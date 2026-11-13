"""回合事实层：自进化唯一的、诚实的输入。

## 为什么需要这一层

自进化闭环原来是：手写用例 → LLM 自评 → 归因 → 改 prompt。它有一个致命问题——
**输入是想象中的问题，标签是模型自己的判断**。优化出来的 prompt 在评测集上变好，
在真实使用里未必；反过来，真实使用里暴露的毛病（查了三轮没查到、上下文被裁掉
关键材料、答完就被追问）根本不进闭环。

所以这里只做一件事：**一轮问答结束，把这一轮的全部信号快照成一行**，
让"哪类问题在什么条件下容易失败"变成可以 GROUP BY 的事实。

## 四类信号不能混

| 类别 | 字段 | 作用 |
|---|---|---|
| 输入 | retrieval_n / memory_n / ctx_tokens / ctx_budget | 解释"这轮为什么难" |
| 过程 | llm_calls / tool_calls / tool_errors | 解释"它是怎么跑的" |
| 结果 | reply_chars / duration_ms / status | 解释"跑完是什么" |
| 质量 | feedback / followup_secs | **唯一的标签** |

前三类是解释变量，最后一类才是 label。把耗时当"质量"是最常见的错误——
它只能回答"快不快"，回答不了"对不对"。

## 信号强度分级（决定归因时的权重）

1. `feedback = -1`：用户明确说没用。**最强证据**，不需要再推断。
2. `followup_secs <= 90` 且未表态：答完一分半内又问——大概率没解决。
   这是**弱证据**，只能作为辅因，不能单独定案（用户也可能只是接着问下一个问题）。
3. 规则推断（空回复、工具报错、打到轮次上限）：是实现缺陷的证据，不是质量证据。

所以 `failure_rule`（主因）永远优先取强证据，弱证据只在没有别的问题时才当主因。

## 不做什么

- **不存消息正文**。正文在 console_messages，这里只存指标。事实表要能被长期聚合，
  不该跟着内容一起膨胀。
- **不回填历史**。2026-11-12 之前的 82 条消息没有 duration/tool_errors，
  硬凑出来的行会让"耗时分布"变成一半 0 一半真值。宁可让面板显示"自 X 起统计"。
"""
from __future__ import annotations

import datetime
import json
import uuid

from ..core.db import get_conn

# 主因优先级：强证据在前，弱证据（fast_followup）只在没有别的问题时才算数。
# 顺序本身就是判断逻辑，改顺序等于改结论，所以放在模块顶部显式声明。
PRIMARY_ORDER = (
    "user_reject",      # 用户点踩
    "run_failed",       # 这一轮报错/状态 failed
    "blind_answer",     # 技能要求查数据，但一次工具都没调
    "loop_exhausted",   # 打到 LLM 轮次上限还没收敛
    "empty_reply",      # 回复过短，基本等于没答
    "tool_error",       # 工具报错（可能是参数不对，也可能是下游挂了）
    "ctx_squeezed",     # 上下文被裁，材料不全
    "too_slow",         # 超过 90 秒
    "llm_unstable",     # 模型调用失败/降级过（但最后成了）
    "fast_followup",    # 90 秒内被追问（弱信号）
)

RULE_LABELS = {
    "user_reject": "用户点踩",
    "run_failed": "整轮失败",
    "blind_answer": "未查就答",
    "loop_exhausted": "轮次用尽",
    "empty_reply": "回复为空",
    "tool_error": "工具报错",
    "ctx_squeezed": "上下文被裁",
    "too_slow": "响应过慢",
    "llm_unstable": "模型调用重试",
    "fast_followup": "快速追问",
}

# 规则 → 该动哪里。这张表是"归因"和"改进"之间的桥：
# 没有它，归因报告只会说"有 5 条 wrong_tool"，看了也不知道改什么。
# 每条都要指出**具体的改动位置**，而不是"优化一下 prompt"这种废话。
ACTION_HINTS = {
    "user_reject": ("看样本", "用户明确说没用。逐条看回复内容判断是知识缺口还是推理错误；"
                            "确认后进回归集，防止以后再犯。"),
    "run_failed": ("查错误", "先看 error 字段。若是 LLMError/超时，是稳定性问题不是内容问题，"
                            "改 prompt 没用。"),
    "blind_answer": ("改技能正文", "技能声明了必须查数据却没查。在技能 SOP 里写明"
                                "「必须先调用 X，拿到结果才能回答」。"),
    "loop_exhausted": ("缩小范围", "打到轮次上限说明找材料的范围太大。缩小检索 top_k、"
                                 "或给技能加「两次没查到就直说没查到」的停止条件。"),
    "empty_reply": ("查中断", "模型没吐任何字。多半是工具结果撑爆预算或 JSON 解析失败，"
                             "看这一轮的 ctx_budget 与 tool_errors。"),
    "tool_error": ("改工具", "工具报错不是 prompt 问题。看是参数校验没过还是上游挂了，"
                            "参数问题改 description，上游问题加降级。"),
    "ctx_squeezed": ("调预算", "历史被裁掉了。要么提高上下文预算，要么缩短检索材料——"
                              "注意这会让「记不住上文」变成静默故障。"),
    "too_slow": ("拆步骤", "超过 90 秒。看 llm_calls：轮数多是工具在绕圈，单轮慢是模型或网络。"),
    "llm_unstable": ("改稳定性", "模型调用失败过又重试成功。这是链路问题不是内容问题——"
                                "改 prompt 无效，要看 error 是 429 限流、超时还是请求体非法。"),
    "fast_followup": ("补齐结论", "90 秒内被追问，大概率答案没给到结论。"
                                 "检查回复是否只罗列了过程没有最终判断。"),
}

EMPTY_REPLY_CHARS = 0      # 只抓「完全没吐字」。长度不是质量信号：短答案可能是对的
SLOW_MS = 90_000
FAST_FOLLOWUP_SECS = 90


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"tf-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def classify(row: dict, *, max_llm_calls: int | None = None) -> list[str]:
    """给一行回合事实打规则标签（可能命中多条）。

    只做**可解释的规则**，不上模型判断：归因本身要能被复核。
    每条规则的阈值都取"明显异常"而不是"略低于平均"——宁可漏报，
    不可天天报警导致人不再看。
    """
    flags: list[str] = []
    fb = row.get("feedback")
    if fb is not None and int(fb) < 0:
        flags.append("user_reject")
    if row.get("status") == "failed" or row.get("error"):
        flags.append("run_failed")
    if int(row.get("tool_errors") or 0) > 0:
        flags.append("tool_error")
    try:
        degraded = json.loads(row.get("degraded_json") or "[]")
    except (ValueError, TypeError):
        degraded = []
    if int(row.get("dropped_msgs") or 0) > 0 or "drop_oldest_turn" in degraded:
        flags.append("ctx_squeezed")
    if int(row.get("reply_chars") or 0) <= EMPTY_REPLY_CHARS:
        flags.append("empty_reply")
    limit = int(max_llm_calls or 6)
    if int(row.get("llm_calls") or 0) >= limit and int(row.get("reply_chars") or 0) < 200:
        flags.append("loop_exhausted")
    # 盲答：技能声明了必须用的工具，结果一个没调、记忆也没召回——它只能靠猜
    if (int(row.get("need_tools") or 0) > 0 and int(row.get("tool_calls") or 0) == 0
            and int(row.get("memory_n") or 0) == 0):
        flags.append("blind_answer")
    fu = row.get("followup_secs")
    if fb is None and fu is not None and 0 < int(fu) <= FAST_FOLLOWUP_SECS:
        flags.append("fast_followup")
    if int(row.get("duration_ms") or 0) > SLOW_MS:
        flags.append("too_slow")
    # 失败/降级的模型调用：整轮最终成了，但链路抖过。
    # 排在这些之后才当主因——用户可能完全没感知，不该抢占"内容不对"的位置。
    if int(row.get("llm_errors") or 0) > 0:
        flags.append("llm_unstable")
    return flags


def primary_rule(flags: list[str]) -> str | None:
    for rule in PRIMARY_ORDER:
        if rule in flags:
            return rule
    return None


def record(*, session_id: str, turn: int, mode: str = "auto", skill: str | None = None,
           skill_version: int | None = None, segs: int = 1, need_tools: int = 0,
           msg_chars: int = 0, ctx: dict | None = None, llm_calls: int = 0,
           trace: list | None = None, reply: str = "", duration_ms: int = 0,
           status: str = "ok", error: str | None = None, usage: dict | None = None,
           max_llm_calls: int = 6, trace_id: str = "") -> str | None:
    """落一行回合事实。**唯一写入点**——别处不要再往 turn_facts 插数。

    写失败绝不影响对话：事实层是统计，不是业务前置条件。
    但失败了要能看出来，所以返回 None 而不是静默吞掉。

    ## 为什么 llm_calls / tokens / cost 要回台账重算

    原来这三个数由调用方攒（console 累计 llm_calls，pricing.snapshot() 取费用）。
    实测对不上：某一轮轮级记 5 次，同一 (session,turn) 的 llm_usage 有 6 行——
    第 6 行是异步记忆抽取，它花了同一轮的钱但不占主循环配额。两本账各记各的，
    差值没人解释得了，而"这轮几次调用"正是判断轮次用尽的依据。

    现在改成：**调用级台账是唯一事实源，轮级是它的投影**。
    按 (session_id, turn) 聚合 scene LIKE 'console%' 的记录，成功数与失败数分开。
    聚合不到才退回调用方传的值（测试、或 LLM 未真正调用时）。
    """
    ctx = ctx or {}
    usage = usage or {}
    trace = trace or []
    ts = _now()
    tid = _new_id()
    row = {
        "id": tid, "session_id": session_id, "turn": int(turn), "ts": ts,
        "trace_id": trace_id or None, "mode": mode,
        "skill": skill, "skill_version": skill_version, "segs": int(segs),
        "need_tools": int(need_tools),
        "msg_chars": int(msg_chars),
        "retrieval_n": int(ctx.get("retrieved_n") or 0),
        "memory_n": int(ctx.get("memory_n") or 0),
        "ctx_tokens": int(ctx.get("total") or 0),
        "ctx_budget": int(ctx.get("budget") or 0),
        "dropped_msgs": int(ctx.get("dropped") or 0),
        "degraded_json": json.dumps(ctx.get("degraded") or [], ensure_ascii=False),
        "hit_ids": json.dumps(ctx.get("hit_ids") or [], ensure_ascii=False),
        "llm_calls": int(llm_calls),
        "llm_aux": 0,
        "llm_errors": 0,
        "tool_calls": len(trace),
        "tool_errors": sum(1 for t in trace if not t.get("ok", True)),
        "reply_chars": len(reply or ""),
        "duration_ms": int(duration_ms),
        "tokens": int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0)),
        "cost_cny": float(usage.get("cost_cny") or 0),
        "status": status, "error": (error or None),
        "feedback": None, "feedback_note": None, "followup_secs": None,
    }

    conn = None
    try:
        conn = get_conn()
        # 先对齐台账，再算标签——顺序不能反：loop_exhausted 依赖 llm_calls，
        # 用调用方攒的数会算出"打到上限"而台账显示只调了 3 次。
        led = _ledger(conn, session_id, int(turn))
        if led["ok"] or led["err"]:
            row["llm_calls"] = int(led["ok"])
            row["llm_errors"] = int(led["err"])
            row["tokens"] = int(led["tokens"])
            row["cost_cny"] = float(led["cost"])
        row["flags_json"] = json.dumps(classify(row, max_llm_calls=max_llm_calls),
                                       ensure_ascii=False)
        row["failure_rule"] = primary_rule(json.loads(row["flags_json"]))

        # 版本号调用方手里没有（它只有技能对象），在这里补一次。技能没进版本管理时
        # 保持 NULL——和 skill_runs 同一原则，编个 0 会让人以为存在"第 0 版"。
        if skill_version is None and skill:
            v = conn.execute("SELECT MAX(version) FROM skill_versions WHERE skill=?",
                             (skill,)).fetchone()
            skill_version = v[0] if v and v[0] is not None else None
            row["skill_version"] = skill_version
        # 同一轮被重新生成时覆盖：旧行的 feedback 是对旧答案的评价，留着会污染新结果
        conn.execute(
            "INSERT OR REPLACE INTO turn_facts (id,session_id,turn,ts,trace_id,mode,skill,"
            "skill_version,segs,need_tools,msg_chars,retrieval_n,memory_n,ctx_tokens,"
            "ctx_budget,dropped_msgs,degraded_json,hit_ids,llm_calls,llm_aux,llm_errors,"
            "tool_calls,tool_errors,reply_chars,duration_ms,tokens,cost_cny,status,error,"
            "feedback,feedback_note,followup_secs,flags_json,failure_rule) "
            "VALUES (:id,:session_id,:turn,:ts,:trace_id,:mode,:skill,:skill_version,:segs,"
            ":need_tools,:msg_chars,:retrieval_n,:memory_n,:ctx_tokens,:ctx_budget,"
            ":dropped_msgs,:degraded_json,:hit_ids,:llm_calls,:llm_aux,:llm_errors,"
            ":tool_calls,:tool_errors,:reply_chars,:duration_ms,:tokens,:cost_cny,:status,"
            ":error,:feedback,:feedback_note,:followup_secs,:flags_json,:failure_rule)", row)
        _stamp_followup(conn, session_id, int(turn), ts)
        conn.commit()
        return tid
    except Exception:  # noqa: BLE001
        return None
    finally:
        if conn is not None:
            conn.close()


def _ledger(conn, session_id: str, turn: int) -> dict:
    """按调用级台账重算这一轮的模型调用账。

    只认 scene 以 'console' 开头的行——那是主循环（console:auto / console:assist）。
    记忆抽取、标题生成、编排步骤走各自的 scene，属于"同一轮但不占主循环配额"，
    由 bump_aux 单独累加，混进来会让"轮次用尽"的判定失准。
    """
    try:
        r = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),0) AS ok, "
            "COALESCE(SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END),0) AS err, "
            "COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens, "
            "COALESCE(SUM(cost_cny),0) AS cost "
            "FROM llm_usage WHERE session_id=? AND turn=? AND scene LIKE 'console%'",
            (session_id, turn)).fetchone()
        return {"ok": int(r["ok"] or 0), "err": int(r["err"] or 0),
                "tokens": int(r["tokens"] or 0), "cost": float(r["cost"] or 0)}
    except Exception:  # noqa: BLE001  老库没这几列时不能让落账整体失败
        return {"ok": 0, "err": 0, "tokens": 0, "cost": 0.0}


def bump_aux(session_id: str, turn: int, *, tokens: int = 0, cost: float = 0.0,
             calls: int = 1) -> bool:
    """回填"同一轮的旁路调用"（记忆抽取、标题生成…）。

    为什么是回填而不是写入时一起算：旁路是异步的，record 落行时它还没跑完。
    这也是唯一一个"事后改本轮"的字段——它花的是这一轮的钱，但不在主循环里，
    分开记才能回答"一轮对话的真实成本里，有多少是主循环之外的"。
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE turn_facts SET llm_aux=llm_aux+?, tokens=tokens+?, cost_cny=cost_cny+? "
            "WHERE session_id=? AND turn=?",
            (int(calls), int(tokens), float(cost), session_id, int(turn)))
        conn.commit()
        return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def _stamp_followup(conn, session_id: str, turn: int, ts: str) -> None:
    """给上一轮回填"多久之后被追问"。

    为什么由后一轮回填：一轮刚结束时，下一轮还不存在，这个数只能后到。
    这也是唯一一个"后来改前一条"的字段——它描述的是两轮之间的关系，
    不属于任何单独一轮。
    """
    prev = conn.execute(
        "SELECT id, ts FROM turn_facts WHERE session_id=? AND turn<? "
        "ORDER BY turn DESC LIMIT 1", (session_id, turn)).fetchone()
    if not prev:
        return
    try:
        delta = int((datetime.datetime.fromisoformat(ts)
                     - datetime.datetime.fromisoformat(prev["ts"])).total_seconds())
    except (ValueError, TypeError):
        return
    if delta < 0:
        return
    conn.execute("UPDATE turn_facts SET followup_secs=? WHERE id=?", (delta, prev["id"]))
    # 追问信号变了，上一轮的弱标签要跟着重算——否则"快速追问"永远标不上
    row = conn.execute("SELECT * FROM turn_facts WHERE id=?", (prev["id"],)).fetchone()
    if row:
        flags = classify(dict(row))
        conn.execute("UPDATE turn_facts SET flags_json=?, failure_rule=? WHERE id=?",
                     (json.dumps(flags, ensure_ascii=False), primary_rule(flags), prev["id"]))


def set_feedback(turn_id: str, score: int, note: str | None = None) -> dict | None:
    """写显式反馈。点踩是最强证据，写完立刻重算这一轮的归因。

    note 缺省时**不覆盖**已有备注：面板上点一下按钮只会传 score，
    如果按"没传就是空"处理，会把用户之前写的理由抹掉——理由比分数更难补。
    """
    score = 1 if int(score) > 0 else -1
    conn = get_conn()
    try:
        if note is None:
            cur = conn.execute("UPDATE turn_facts SET feedback=? WHERE id=?",
                               (score, turn_id))
        else:
            cur = conn.execute("UPDATE turn_facts SET feedback=?, feedback_note=? WHERE id=?",
                               (score, (note or "")[:300] or None, turn_id))
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM turn_facts WHERE id=?", (turn_id,)).fetchone()
        flags = classify(dict(row))
        conn.execute("UPDATE turn_facts SET flags_json=?, failure_rule=? WHERE id=?",
                     (json.dumps(flags, ensure_ascii=False), primary_rule(flags), turn_id))
        conn.commit()
        return dict(conn.execute("SELECT * FROM turn_facts WHERE id=?", (turn_id,)).fetchone())
    finally:
        conn.close()


def _since(days: int) -> str:
    d = datetime.date.today() - datetime.timedelta(days=max(int(days), 1) - 1)
    return d.isoformat()


def summary(days: int = 14) -> dict:
    """回合质量总览。**唯一口径**——面板和归因都从这里取，别各写一条 SQL。"""
    since = _since(days)
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT COUNT(*) AS turns, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_turns, "
            "SUM(CASE WHEN feedback=1 THEN 1 ELSE 0 END) AS liked, "
            "SUM(CASE WHEN feedback=-1 THEN 1 ELSE 0 END) AS disliked, "
            "SUM(CASE WHEN feedback IS NOT NULL THEN 1 ELSE 0 END) AS rated, "
            "SUM(CASE WHEN followup_secs IS NOT NULL THEN 1 ELSE 0 END) AS has_followup, "
            "SUM(CASE WHEN followup_secs BETWEEN 1 AND ? THEN 1 ELSE 0 END) AS fast_followup, "
            "SUM(tokens) AS tokens, SUM(cost_cny) AS cost_cny, "
            "SUM(CASE WHEN failure_rule IS NOT NULL THEN 1 ELSE 0 END) AS flagged "
            "FROM turn_facts WHERE ts>=?", (FAST_FOLLOWUP_SECS, since)).fetchone()
        d = dict(r)
        turns = int(d.get("turns") or 0)
        # P95 用 Python 算：SQLite 没有 percentile，装扩展不值得
        ms = [int(x[0]) for x in conn.execute(
            "SELECT duration_ms FROM turn_facts WHERE ts>=? ORDER BY duration_ms", (since,))]
        p50 = ms[len(ms) // 2] if ms else 0
        p95 = ms[min(int(len(ms) * 0.95), len(ms) - 1)] if ms else 0

        by_rule = [{"rule": row["failure_rule"],
                    "label": RULE_LABELS.get(row["failure_rule"], row["failure_rule"]),
                    "n": row["n"]}
                   for row in conn.execute(
                       "SELECT failure_rule, COUNT(*) AS n FROM turn_facts "
                       "WHERE ts>=? AND failure_rule IS NOT NULL "
                       "GROUP BY failure_rule ORDER BY n DESC", (since,))]
        by_skill = [dict(row) for row in conn.execute(
            "SELECT COALESCE(skill,'(通用)') AS skill, COUNT(*) AS turns, "
            "SUM(CASE WHEN failure_rule IS NOT NULL THEN 1 ELSE 0 END) AS flagged, "
            "SUM(CASE WHEN feedback=-1 THEN 1 ELSE 0 END) AS disliked, "
            "CAST(AVG(duration_ms) AS INTEGER) AS avg_ms, "
            "CAST(AVG(cost_cny)*10000 AS INTEGER)/10000.0 AS avg_cost "
            "FROM turn_facts WHERE ts>=? GROUP BY skill ORDER BY turns DESC", (since,))]
        by_day = [dict(row) for row in conn.execute(
            "SELECT substr(ts,1,10) AS day, COUNT(*) AS turns, "
            "SUM(CASE WHEN failure_rule IS NOT NULL THEN 1 ELSE 0 END) AS flagged, "
            "SUM(CASE WHEN feedback=-1 THEN 1 ELSE 0 END) AS disliked "
            "FROM turn_facts WHERE ts>=? GROUP BY day ORDER BY day", (since,))]
        first = conn.execute("SELECT MIN(ts) FROM turn_facts").fetchone()[0]
    finally:
        conn.close()

    def rate(n, d_):
        return round((n or 0) / d_, 4) if d_ else None

    return {"window_days": int(days), "since": since, "first_ts": first,
            "turns": turns,
            "ok_turns": int(d.get("ok_turns") or 0),
            "ok_rate": rate(d.get("ok_turns"), turns),
            "flagged": int(d.get("flagged") or 0),
            "flag_rate": rate(d.get("flagged"), turns),
            "rated": int(d.get("rated") or 0),
            "liked": int(d.get("liked") or 0),
            "disliked": int(d.get("disliked") or 0),
            "reject_rate": rate(d.get("disliked"), int(d.get("rated") or 0)),
            "has_followup": int(d.get("has_followup") or 0),
            "fast_followup": int(d.get("fast_followup") or 0),
            "followup_rate": rate(d.get("fast_followup"), int(d.get("has_followup") or 0)),
            "tokens": int(d.get("tokens") or 0),
            "cost_cny": round(float(d.get("cost_cny") or 0), 4),
            "p50_ms": p50, "p95_ms": p95,
            "by_rule": by_rule, "by_skill": by_skill, "by_day": by_day}


def params(max_llm_calls: int = 6) -> dict:
    """归因阈值。面板上直接显示，避免"为什么这条被标成失败"变成玄学。"""
    return {"empty_reply_chars": EMPTY_REPLY_CHARS, "slow_ms": SLOW_MS,
            "fast_followup_secs": FAST_FOLLOWUP_SECS, "max_llm_calls": int(max_llm_calls)}


def clusters(days: int = 14, *, per_rule: int = 3) -> dict:
    """失败簇：按规则聚合，每簇带几个真实样本。

    这是自进化的**取样入口**——优化 prompt 时，材料不再是手写用例，
    而是"上周真实发生过的、被点踩的 3 轮对话"。
    """
    since = _since(days)
    conn = get_conn()
    try:
        rules = list(conn.execute(
            "SELECT failure_rule, COUNT(*) AS n FROM turn_facts "
            "WHERE ts>=? AND failure_rule IS NOT NULL "
            "GROUP BY failure_rule ORDER BY n DESC", (since,)))
        out = []
        for r in rules:
            samples = [dict(x) for x in conn.execute(
                # trace_id 一起带出来：样本旁边要能直接跳到那一轮的调用树。
                # 没有这个字段时，"看样本"只能看到一行统计，看不到现场长什么样。
                "SELECT id, ts, session_id, turn, trace_id, skill, reply_chars, duration_ms, "
                "tool_calls, tool_errors, llm_calls, feedback, feedback_note, flags_json "
                "FROM turn_facts WHERE failure_rule=? AND ts>=? "
                "ORDER BY ts DESC LIMIT ?", (r["failure_rule"], since, per_rule))]
            out.append({"rule": r["failure_rule"],
                        "label": RULE_LABELS.get(r["failure_rule"], r["failure_rule"]),
                        "n": r["n"], "samples": samples})
        return {"since": since, "clusters": out,
                "total_flagged": sum(int(r["n"]) for r in rules)}
    finally:
        conn.close()


def improvement(days: int = 14, *, per_skill: int = 3) -> dict:
    """按技能汇总"哪里该改"，并给出改哪个文件。

    这是自进化的**决策出口**：不是"失败率 12%"，而是
    「gap_to_cards 上周 5 次里有 3 次一次工具都没调 → 改技能正文明写必须先查」。
    没有这层转换，归因报告只能得出"情况不太好"。
    """
    since = _since(days)
    conn = get_conn()
    try:
        skills = list(conn.execute(
            "SELECT COALESCE(skill,'(通用)') AS skill, COUNT(*) AS turns, "
            "SUM(CASE WHEN failure_rule IS NOT NULL THEN 1 ELSE 0 END) AS flagged, "
            "SUM(CASE WHEN feedback=-1 THEN 1 ELSE 0 END) AS disliked "
            "FROM turn_facts WHERE ts>=? GROUP BY skill "
            "HAVING flagged>0 ORDER BY flagged DESC", (since,)))
        out = []
        for s in skills:
            rules = []
            for r in conn.execute(
                    "SELECT failure_rule AS rule, COUNT(*) AS n FROM turn_facts "
                    "WHERE ts>=? AND COALESCE(skill,'(通用)')=? AND failure_rule IS NOT NULL "
                    "GROUP BY failure_rule ORDER BY n DESC", (since, s["skill"])):
                target, advice = ACTION_HINTS.get(r["rule"], ("待判断", ""))
                rules.append({"rule": r["rule"],
                              "label": RULE_LABELS.get(r["rule"], r["rule"]),
                              "n": r["n"], "target": target, "advice": advice})
            samples = [dict(x) for x in conn.execute(
                "SELECT id, ts, turn, session_id, failure_rule, reply_chars, llm_calls,"
                " tool_calls, tool_errors, duration_ms, feedback, feedback_note "
                "FROM turn_facts WHERE ts>=? AND COALESCE(skill,'(通用)')=? "
                "AND failure_rule IS NOT NULL ORDER BY ts DESC LIMIT ?",
                (since, s["skill"], per_skill))]
            out.append({"skill": s["skill"], "turns": s["turns"],
                        "flagged": s["flagged"], "disliked": s["disliked"],
                        "rules": rules, "samples": samples})
        return {"since": since, "skills": out}
    finally:
        conn.close()


def regression_candidates(days: int = 14, *, limit: int = 50) -> list[dict]:
    """把真实出问题的回合整理成回归用例**草稿**。

    为什么不自动写成 evals/agent_tasks.jsonl：那里的 `must_mention`（必须提到什么）
    只有人能定。让模型自己生成"正确答案该包含什么"，就等于用同一张嘴判自己对不对。
    所以这里只交出可靠的部分——用户原话、当时调了什么、错在哪，剩下的留空等人补。
    """
    since = _since(days)
    conn = get_conn()
    try:
        rows = list(conn.execute(
            "SELECT id, session_id, turn, ts, skill, failure_rule, flags_json,"
            " tool_calls, tool_errors, llm_calls, duration_ms, feedback, feedback_note "
            "FROM turn_facts WHERE ts>=? AND failure_rule IS NOT NULL "
            "ORDER BY CASE WHEN feedback<0 THEN 0 ELSE 1 END, ts DESC LIMIT ?",
            (since, int(limit))))
        out = []
        for r in rows:
            q = conn.execute(
                "SELECT content FROM console_messages WHERE session_id=? AND role='user' "
                "ORDER BY rowid LIMIT 1 OFFSET ?", (r["session_id"], int(r["turn"]) - 1)).fetchone()
            try:
                flags = json.loads(r["flags_json"] or "[]")
            except (ValueError, TypeError):
                flags = []
            out.append({
                "turn_id": r["id"], "ts": r["ts"], "skill": r["skill"],
                "failure_rule": r["failure_rule"],
                "label": RULE_LABELS.get(r["failure_rule"], r["failure_rule"]),
                "flags": flags, "query": (q["content"] if q else None),
                "tool_calls": r["tool_calls"], "tool_errors": r["tool_errors"],
                "llm_calls": r["llm_calls"], "duration_ms": r["duration_ms"],
                "feedback": r["feedback"], "feedback_note": r["feedback_note"],
                # 留给人的两栏：答案该包含什么、最少调几个工具。
                # min_tools 不能填"当时调了几次"——那正是可能出错的地方
                # （实测一轮问复习却调了 6 次建卡工具）。它和 must_mention 同理，
                # 是判断标准，不是观测值，只能人定。
                "draft_case": {"task": (q["content"] if q else ""),
                               "min_tools": None,
                               "must_mention": []},
            })
        return out
    finally:
        conn.close()


def recent(days: int = 14, *, limit: int = 30, only_flagged: bool = False) -> list[dict]:
    since = _since(days)
    conn = get_conn()
    try:
        sql = ("SELECT * FROM turn_facts WHERE ts>=?"
               + (" AND failure_rule IS NOT NULL" if only_flagged else "")
               + " ORDER BY ts DESC LIMIT ?")
        return [dict(r) for r in conn.execute(sql, (since, int(limit)))]
    finally:
        conn.close()


def coverage(days: int = 14) -> dict:
    """**落账覆盖率自检**：真实发生过多少轮对话，其中多少轮留下了事实。

    为什么这是最重要的一张表而不是最不重要：所有"失败率 12%""点踩率 8%"都建立在
    "分母是对的"之上。分母是 turn_facts 的行数，而真实发生的轮次在
    console_messages（用户消息数）。两者一差，面板上所有比例都是假的——
    而且假得**看不出来**：漏掉的那几轮恰恰可能是失败得最惨、根本没走到落账的。

    所以这个函数不产生结论，只负责把"结论的可信边界"报出来。

    ## 分母怎么取（这是唯一容易算错的地方）

    按时间截断是错的：一轮对话里用户消息先落库、事实后落库，用"建表时刻"当起点
    会把每一轮的自己都排除掉；按日期截又会把建表当天早些时候的历史对话算成漏账
    （实测 29 轮里 27 轮是建表前的，直接显示 6.9% 覆盖率，纯属虚报）。

    所以按**会话**判定：一个会话只要出现过事实行，就说明它已经在记账范围内，
    它的全部用户消息都进分母；从没出现事实行的会话不进分母，单独作为
    `sessions_without_facts` 列出来，并按建表时刻分成"升级前"和"升级后"——
    后者才是真的漏。

    ## 另附三个字段完整率（"细不细"的直接证据）

    - no_cost：cost_cny=0 的行（台账没对上，费用不可信）
    - no_trace：trace_id 空（串不起调用链）
    - no_hits：retrieval_n>0 但 hit_ids 空（老行没有明细，不能拿来分析检索）
    """
    since = _since(days)
    conn = get_conn()
    try:
        fact = {r["session_id"]: int(r["n"]) for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM turn_facts GROUP BY session_id")}
        # 只统计"已进入记账范围的会话"的轮次。用户的第 n 条消息 = 第 n 轮，
        # 与 _chat_inner 的 turn 算法一致（"一句继续也是新的一轮"）。
        seen: dict[str, int] = {}
        orphans: list[dict] = []
        for r in conn.execute(
                "SELECT session_id, COUNT(*) AS n, MAX(created_at) AS last "
                "FROM console_messages WHERE role='user' GROUP BY session_id"):
            sid = r["session_id"]
            if not sid:
                continue
            if sid in fact:
                seen[sid] = int(r["n"])
            else:
                orphans.append({"session_id": sid, "turns": int(r["n"]),
                                "last_seen": (r["last"] or "")[:19]})

        wall = conn.execute("SELECT MIN(ts) FROM turn_facts").fetchone()[0]
        before_wall = [o for o in orphans if wall and o["last_seen"] < wall]
        after_wall = [o for o in orphans if not (wall and o["last_seen"] < wall)]

        missing = []
        for sid, n in seen.items():
            got = fact.get(sid, 0)
            if got < n:
                missing.append({"session_id": sid, "turns": n, "facts": got,
                                "lost": n - got})
        missing.sort(key=lambda x: -x["lost"])

        agg = conn.execute(
            "SELECT COUNT(*) AS rows, "
            "SUM(CASE WHEN cost_cny=0 THEN 1 ELSE 0 END) AS no_cost, "
            "SUM(CASE WHEN trace_id IS NULL OR trace_id='' THEN 1 ELSE 0 END) AS no_trace, "
            "SUM(CASE WHEN retrieval_n>0 AND hit_ids='[]' THEN 1 ELSE 0 END) AS no_hits "
            "FROM turn_facts WHERE ts>=?", (since,)).fetchone()
        total_seen = sum(seen.values())
        total_fact = sum(fact.values())
        rows = int(agg["rows"] or 0)
    finally:
        conn.close()

    def rate(n, d):
        return round((n or 0) / d, 4) if d else None

    return {
        "window_days": int(days), "since": since,
        # 记账起点：面板要显式说明"从哪一刻开始算"，否则历史对话会被读成故障
        "counted_since": wall,
        "turns_seen": total_seen,          # 记账范围内的真实轮次
        "turns_recorded": total_fact,      # 落了事实的
        "coverage": rate(total_fact, total_seen),
        "missing": missing[:10],
        "sessions_seen": len(seen), "sessions_missing": len(missing),
        # 从没出现过事实行的会话。升级前的是历史，升级后的才是真漏——
        # 混成一个数就没法判断"该不该担心"。
        "sessions_before_wall": len(before_wall),
        "sessions_after_wall": len(after_wall),
        "leaked_sessions": after_wall[:5],
        # 字段完整率（分母是本窗口的行数，不是真实轮次）
        "no_cost": int(agg["no_cost"] or 0),
        "no_trace": int(agg["no_trace"] or 0),
        "no_hits": int(agg["no_hits"] or 0),
        "cost_complete": rate(rows - int(agg["no_cost"] or 0), rows),
        "trace_complete": rate(rows - int(agg["no_trace"] or 0), rows),
    }


def detail(turn_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM turn_facts WHERE id=?", (turn_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["flags"] = json.loads(out.get("flags_json") or "[]")
        except (ValueError, TypeError):
            out["flags"] = []
        return out
    finally:
        conn.close()


def by_turn(session_id: str, turn: int) -> dict | None:
    """按会话+轮次取一行。前端知道消息但不知道 turn_id 时用这个。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM turn_facts WHERE session_id=? AND turn=?",
                           (session_id, int(turn))).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
