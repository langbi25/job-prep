"""Agent Harness：工具注册表 + Agent 循环 + 轨迹落库。

设计：
- 工具即普通 Python 函数，注册表声明 name/description/JSON schema，直接透传 DeepSeek function calling。
- 循环上限 max_iters 防失控；每一步（工具调用/入参/结果摘要）记入 trace；
- 整个 run 落 runs 表（kind/status/steps/result），可回放、可评测——这是"可观测"的地基。
当前接入场景：学习教练 Agent（读真实数据 → 产出学习计划）。
"""
import datetime
import json
import random
import re
import traceback

from ..core import config, embedder, llm, prompts
from ..core.db import get_conn
from . import review_service


TOOLS = [
    {"type": "function", "function": {
        "name": "get_daily_brief",
        "description": "获取候选人当前学习概况：到期卡片数、未解决错题数、近期模拟面试均分、薄弱卡片",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_due_cards",
        "description": "列出今日到期的复习卡片（含主题和掌握度）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回条数"}}}}},
    {"type": "function", "function": {
        "name": "list_open_mistakes",
        "description": "列出未解决的错题（含错因和主题）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "get_weak_topics",
        "description": "获取薄弱主题（低掌握度卡片 + 近 14 天错题主题），可按方向过滤",
        "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["backend", "agent", "bigdata"]}}}}},
    {"type": "function", "function": {
        "name": "get_top_questions",
        "description": "获取面经题池中的高频题目（按出现次数排序）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "search_kb",
        "description": "在知识库里做混合检索，返回相关卡片（标题/主题/掌握度/要点原文）。"
                       "需要回忆'我到底记过什么'时用它，不要凭记忆编内容。没命中说明是知识缺口。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索语句，可用题目原文或关键词"},
            "topic": {"type": "string", "description": "限定主题（可选），如 Flink / Agent 记忆"},
            "top_k": {"type": "integer", "description": "返回卡片数，默认 3，最多 5"}},
            "required": ["query"]}}},
]


def _list_open_mistakes(args: dict) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.raw_text, m.cause_type, m.created_at, q.topic FROM mistakes m "
            "LEFT JOIN question_pool q ON q.id=m.question_id WHERE m.resolved=0 "
            "ORDER BY m.created_at DESC LIMIT ?", (int(args.get("limit", 10)),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _top_questions(args: dict) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT text, qtype, topic, freq_count FROM question_pool "
            "WHERE merged_into IS NULL ORDER BY freq_count DESC LIMIT ?",
            (int(args.get("limit", 5)),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _search_kb(args: dict) -> dict:
    """知识库混合检索工具：把"卡片"变成 Agent 能读的东西。

    和其它四个工具的区别：那四个返回**清单**（有哪些卡/哪些题），
    这个返回**内容**（卡里写了什么要点）。没有它，Agent 只能知道"我有一张卡"，
    不能知道"这张卡讲了什么"，也就没法自己判断该复习还是该建卡。
    返回带 card_id 可溯源，points 截断到 120 字/条防上下文膨胀。
    """
    from . import rag_service  # 局部导入：rag_service 不依赖 harness，避免模块级循环

    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空"}
    try:
        top_k = int(args.get("top_k", 3) or 3)
    except (TypeError, ValueError):
        top_k = 3
    top_k = max(1, min(5, top_k))
    topic = str(args.get("topic") or "").strip()

    # 带主题过滤时多召一些再筛，否则筛完可能不够 top_k
    hits = rag_service.search(query, scope="cards", top_k=top_k * 3 if topic else top_k, mode="hybrid")

    # 置信度：不能靠 cosine 硬过滤。实测"换个问法"的合法命中只有 0.48~0.69，
    # 而无卡题也能到 0.50~0.60——两段重叠，卡阈值必然误杀。
    # 所以只把相似度透给模型，让它看着要点自己判断"这是答案还是只是同主题"。
    sims: dict[str, float] = {}
    if embedder.available():
        try:
            sims = {cid: float(s) for cid, s in embedder.search_similar(query, "card", top_k=20)}
        except Exception:  # noqa: BLE001  向量挂了不影响工具可用
            sims = {}

    conn = get_conn()
    try:
        rows = {}
        for h in hits:
            r = conn.execute(
                "SELECT id,title,topic,mastery,points_json,related_json FROM kb_cards "
                "WHERE id=? AND status='active'", (h["id"],)).fetchone()
            if r:
                rows[r["id"]] = dict(r)
    finally:
        conn.close()

    out = []
    for h in hits:
        r = rows.get(h["id"])
        if not r:
            continue  # 归档卡：向量可能残留，这里按 active 兜底过滤
        if topic and topic not in (r["topic"] or ""):
            continue
        points = json.loads(r["points_json"] or "[]")
        item = {
            "card_id": r["id"], "title": r["title"], "topic": r["topic"],
            "mastery": r["mastery"], "score": round(float(h.get("score", 0)), 4),
            "similarity": round(sims.get(r["id"], 0.0), 3),
            "via": h.get("legs") or [],
            "points": [str(p)[:120] for p in points[:4]],
            "n_points": len(points),
            "related": json.loads(r["related_json"] or "[]"),
        }
        if not points:
            # 空壳卡：有卡但没内容，对 Agent 等于"查到了但读不到"，
            # 明确标出来，否则模型会以为已掌握（更危险的是它开始自己编）
            item["warning"] = "该卡无要点（空壳卡），只能说明有这个主题，不能当答案用"
        out.append(item)
        if len(out) >= top_k:
            break

    max_sim = max([h["similarity"] for h in out], default=0.0)
    if not out:
        confidence, note = "none", "知识库无相关卡片 —— 这是知识缺口，应提议建卡，不要凭记忆编造"
    elif max_sim >= 0.75:
        confidence, note = "high", ""
    elif max_sim >= 0.60:
        confidence, note = "mid", "中等相关：可能只是换了问法，也可能只是同主题，需对照要点判断"
    else:
        confidence, note = "low", "无高置信度卡片：要点若与本问题对不上，按知识缺口处理（提议建卡）"
    hollow = [h["card_id"] for h in out if not h["points"]]
    if hollow:
        note = (note + "；" if note else "") + f"其中 {','.join(hollow)} 无要点，不能当答案用"
    return {"query": query, "hits": out, "max_similarity": round(max_sim, 3),
            "confidence": confidence, "note": note}


_IMPL = {
    "get_daily_brief": lambda a: review_service.daily_brief(),
    "list_due_cards": lambda a: review_service.due_cards(limit=int(a.get("limit", 10))),
    "list_open_mistakes": _list_open_mistakes,
    "get_weak_topics": lambda a: review_service.weak_topics(a.get("direction")),
    "get_top_questions": _top_questions,
    "search_kb": _search_kb,
}


def _new_run_id() -> str:
    return f"run-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(1000, 9999)}"


_MINUTES_RE = re.compile(r"(\d+)\s*(?:分钟|min|mins|minute)")


def _as_minutes(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    m = _MINUTES_RE.search(str(v or ""))
    return int(m.group(1)) if m else 0


def _as_text(v) -> str:
    """把模型可能返回的任意结构压成一段文字（dict/list 直接切片会炸）。"""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False)


DAILY_BUDGET_MIN = 60


def _enforce_budget(plan: dict, budget: int = DAILY_BUDGET_MIN) -> dict:
    """硬性预算由代码兜底，而不是指望 prompt。

    prompt 里写了"单日不超过 60 分钟"，实测仍会给出 65 分钟——LLM 对数字约束的服从度
    是概率性的。凡是"超了就是错的"这类硬约束，都应该在 harness 层做成确定性逻辑：
    按优先级累加，装不下的低优先条目砍掉，并在结果里显式标记被砍了什么（可观测）。
    """
    kept, total, dropped = [], 0, []
    for it in plan["plan"]:
        if total + it["minutes"] <= budget:
            kept.append(it)
            total += it["minutes"]
        else:
            dropped.append(it["action"][:60])
    plan["plan"] = kept
    plan["total_minutes"] = total
    if dropped:
        plan["trimmed"] = True
        plan["dropped"] = dropped
    return plan


def _normalize_plan(data: dict) -> dict:
    """把最终答案规整成**唯一**的对外结构，对外只暴露这一种形状。

    模型不总是守格式，实测至少有三种走样：
    a) plan 是 dict 分桶（{"priority_0_today": ["...", "..."]}）
    b) plan 是字符串数组（["复习 xx", ...]）
    c) 字段名不同（task/desc 代替 action；why 代替 reason）
    如果让下游（UI / 评测 / 落库）各自兼容，每加一个消费方就多一处隐形耦合。
    所以在 harness 边界做一次归一，下游只认归一后的结构。
    """
    raw = data.get("plan")
    items: list[dict] = []

    def push(text: str, priority: int, reason: str = "", minutes: int = 0):
        text = _as_text(text).strip()
        if text:
            items.append({"priority": priority, "action": text,
                          "reason": _as_text(reason).strip(), "minutes": minutes})

    if isinstance(raw, list):
        for idx, it in enumerate(raw, 1):
            if isinstance(it, dict):
                push(it.get("action") or it.get("task") or it.get("title") or "",
                     int(it.get("priority") or idx),
                     it.get("reason") or it.get("why") or it.get("basis") or "",
                     _as_minutes(it.get("minutes") or it.get("time") or 0))
            else:
                push(it, idx)
    elif isinstance(raw, dict):
        # 分桶结构：桶名里的数字/关键词决定优先级
        for bucket, val in raw.items():
            bl = str(bucket).lower()
            if "0" in bl or "today" in bl or "今天" in bl:
                pr = 1
            elif "1" in bl or "week" in bl or "本周" in bl:
                pr = 2
            else:
                pr = 3
            for it in (val if isinstance(val, list) else [val]):
                if isinstance(it, dict):
                    push(it.get("action") or it.get("task") or it.get("title") or "",
                         int(it.get("priority") or pr),
                         it.get("reason") or it.get("why") or "", _as_minutes(it.get("minutes") or 0))
                else:
                    push(it, pr, minutes=_as_minutes(it))
    elif isinstance(raw, str):
        push(raw, 1)

    items.sort(key=lambda x: x["priority"])
    if not items:
        raise ValueError(f"最终输出的 plan 为空或无法解析: {_as_text(raw)[:200]}")

    findings = data.get("key_findings") or data.get("findings") or []
    if not isinstance(findings, list):
        findings = [_as_text(findings)]

    return {"summary": _as_text(data.get("summary")),
            "key_findings": [_as_text(f) for f in findings],
            "plan": items,
            "total_minutes": sum(i["minutes"] for i in items)}



def _save_run(run_id: str, kind: str, status: str, steps: list, result: dict | None):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id,kind,status,steps_json,result_json,created_at) VALUES (?,?,?,?,?,?)",
            (run_id, kind, status, json.dumps(steps, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False) if result else None, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def run_study_plan_agent(direction: str, max_iters: int = 6) -> dict:
    """学习教练 Agent：自主调用工具查数据 → 产出学习计划。全程留痕。"""
    run_id = _new_run_id()
    steps: list = []
    # prompt 从注册表渲染，不再硬编码——否则 prompt 文件里的 schema 约定形同虚设
    system, user, prompt_ver = prompts.render("study_plan", direction=direction)
    messages = [
        {"role": "system", "content": system + "\n优先调用 get_daily_brief 掌握全局，再按需深入具体工具。"
                                             "需要回忆「我到底记过什么」时调用 search_kb（传题目原文或关键词）；"
                                             "search_kb 返回空 = 知识缺口，按建卡处理，不要凭记忆编内容。"},
        {"role": "user", "content": user},
    ]
    try:
        for i in range(max_iters):
            msg = llm._chat_json(None, None, tools=TOOLS, messages=messages)
            if msg.get("tool_calls"):
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": msg["tool_calls"]})
                for tc in msg["tool_calls"]:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                        result = _IMPL[name](args)
                        result_str = json.dumps(result, ensure_ascii=False)[:3000]
                    except Exception as e:  # noqa: BLE001
                        args, result_str = {}, f"工具执行失败: {e}"
                    steps.append({"iter": i, "tool": name, "args": args,
                                  "result_chars": len(result_str), "result_preview": result_str[:200]})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
                continue

            # 最终回答：tools 模式无法同时强制 json_object，解析失败则降级重试一次
            try:
                data = llm.extract_json(msg["content"])
            except (json.JSONDecodeError, ValueError):
                retry_msgs = messages + [
                    {"role": "assistant", "content": msg.get("content") or ""},
                    {"role": "user", "content": "上面的输出不是合法 JSON。请重新输出，只输出符合格式要求的 JSON 本身，不要任何解释或代码块围栏。"},
                ]
                msg2 = llm._chat_json(None, None, messages=retry_msgs)
                data = llm.extract_json(msg2["content"])
            if "plan" not in data:
                raise ValueError(f"最终输出缺少 plan 字段: {_as_text(msg.get('content'))[:200]}")

            plan = _normalize_plan(data)
            plan["prompt_version"] = prompt_ver

            # 预算超了：先给模型一次自我修正机会（agentic 反馈），再不行才由代码裁剪。
            # 顺序很重要——直接裁剪会悄悄丢掉模型认为重要的条目，先让它自己取舍更符合意图。
            if plan["total_minutes"] > DAILY_BUDGET_MIN and i < max_iters - 1:
                steps.append({"iter": i, "tool": "budget_violation",
                              "total_minutes": plan["total_minutes"], "budget": DAILY_BUDGET_MIN})
                messages.append({"role": "assistant", "content": _as_text(msg.get("content"))})
                messages.append({"role": "user", "content": (
                    f"你的计划总时长 {plan['total_minutes']} 分钟，超过单日 {DAILY_BUDGET_MIN} 分钟上限。"
                    "请自行取舍：保留优先级最高、收益最大的条目，压缩或合并次要条目，"
                    "使总时长不超过上限，然后重新输出完整 JSON。")})
                continue

            plan = _enforce_budget(plan)
            steps.append({"iter": i, "tool": "final_answer",
                          "summary": plan["summary"][:100],
                          "items": len(plan["plan"]), "total_minutes": plan["total_minutes"],
                          "trimmed": plan.get("trimmed", False)})
            _save_run(run_id, "study_plan", "done", steps, plan)
            return {"run_id": run_id, "status": "done", "plan": plan, "trace": steps}

        _save_run(run_id, "study_plan", "max_iters", steps, None)
        return {"run_id": run_id, "status": "max_iters", "error": "超过迭代上限仍未产出计划", "trace": steps}
    except Exception as e:  # noqa: BLE001
        # 只存 str(e) 时，像 "slice(None, 100, None)" 这种报错根本定位不到行号，
        # 所以把末帧位置一起落库，排查时不必复现。
        tb = traceback.format_exc().strip().splitlines()
        _save_run(run_id, "study_plan", "error", steps,
                  {"error": str(e), "where": tb[-3:] if len(tb) >= 3 else tb})
        return {"run_id": run_id, "status": "error", "error": str(e), "trace": steps}
