"""上下文预算压力验证：把 12k 预算真打满，看降级链怎么走。

与 smoke_long_conversation.py 的分工：
  - 那个验证"长对话下摘要链路成立"（20 轮短回复，预算只用到 25%）；
  - 这个验证"预算不够时降级链怎么动"（少轮次 + 长回答，逼到 100% 以上）。

四段：
  A. 真实长回答（LLM，10 轮，每轮要求 2500 字以上）——观察自然发生的降级
  B. 预算阶梯扫描（不花 LLM 钱，用 A 的真实历史）——把每一级的临界点定位出来
  C. 超长单轮输入（LLM，1 轮，约 1.2 万字）——验证"本轮用户输入永不裁"是否成立
  D. 轮内裁剪（纯合成，不花 LLM 钱）——ReAct 里 8 条工具结果堆积时的三档收紧

为什么 B 不能省：真实链路只会告诉你"这一轮降到了第几级"，
告诉不了"预算降到多少才会触发 shorten_summary"。要定位临界点必须扫。

跑法（容器内）：
    docker exec job-prep-api-1 python scripts/stress_context_budget.py
成本：约 ¥0.3（长回答输出多）。
"""
import json
import sys
import time

sys.path.insert(0, "/app")

from app.context import assembler, summarizer        # noqa: E402
from app.context.token_budget import Budget, count_messages, count_tokens  # noqa: E402
from app.core.db import get_conn                     # noqa: E402
from app.services import console_service             # noqa: E402

# 注意别在每轮都写"不要…"：那会让每一轮都变成约束轮，压力测试就变成
# 专门测"保护上界"了。真实用户只在开头立规矩，这条差异会改变降级路径。
LONG = ("不少于 3000 字，分小节写，每个小节要有具体做法而不是泛泛而谈。"
        "直接讲解即可，不必查我的知识库。")

JOB_PASTE = (
    "下面是我摘的一段岗位描述，读完再回答：\n\n"
    "【岗位职责】负责实时数据平台核心链路的设计与开发；负责 Flink 作业的性能优化"
    "与稳定性治理，包括反压、数据倾斜、状态膨胀、checkpoint 超时等问题；负责实时数仓"
    "分层建模与指标口径统一；负责数据质量监控体系建设，包括血缘、SLA、异常告警；"
    "参与数据链路从 0 到 1 的搭建，与算法团队协作完成特征工程；负责线上问题的定位"
    "与复盘，输出可复用的排查手册。\n"
    "【任职要求】5 年以上大数据开发经验；精通 Java/Scala，熟悉 JVM 调优；精通 Flink "
    "的运行时架构、状态管理、checkpoint/savepoint 机制与两阶段提交；熟悉 Kafka 的"
    "副本同步、ISR、事务消息与幂等生产；熟悉 Hive/Spark 的分区分桶、小文件治理与"
    "执行计划优化；熟悉 MySQL 索引与慢查询治理；有大规模数据治理与成本优化经验；"
    "有较强的抽象能力，能把重复问题沉淀成平台能力。\n"
    "【加分项】有实时数仓建设经验；有 Flink 源码阅读经验；有开源贡献；有团队管理经验。\n\n"
    "请回答三点：1) 这个岗位最看重的三项能力；2) 我现有经历里最匹配的两段；"
    "3) 我最该补的一个短板。每点都要给出依据，不要泛泛而谈。"
)

FACE_PASTE = (
    "这是我昨天面完记的面经，你看下我答得怎么样：\n\n"
    "一面：1) 自我介绍；2) 项目里 Flink 作业的并发度怎么定的，为什么；"
    "3) checkpoint 间隔设多少，超时怎么办，遇到过 barrier 对齐慢吗；"
    "4) 反压怎么定位，我说了看 backpressure 面板和火焰图，面试官追问"
    "「如果火焰图显示是下游 write 慢，你怎么确认是存储侧还是网络侧」——我答得不好；"
    "5) 数据倾斜，我说了加盐两阶段聚合，追问「加盐后结果怎么合并，会不会重复」；"
    "6) Kafka 消息丢失的三个环节；7) 事务消息和幂等的关系；"
    "8) 一道算法：合并 K 个有序链表，我写了优先队列，面试官问有没有更省内存的做法。\n"
    "二面：1) 让我设计一个日活 100 万的实时 UV 统计，要求分钟级延迟；"
    "我说了 Flink + Redis HyperLogLog，面试官说不能用 Redis，问我怎么办——"
    "我当时卡住了，后来说用 Flink 状态存 bitmap，但没讲清楚状态大小怎么估；"
    "2) 让我讲讲 JVM 调优，我说了 G1 的参数，被追问「你怎么知道该调哪个参数」，"
    "我答得比较空；3) 问我们线上有没有做过容量规划，我说没有系统做过。\n"
    "HR 面：问离职原因、期望薪资、能不能接受值班。\n\n"
    "请逐条点评：哪几问答得可以、哪几问是硬伤、每个硬伤该怎么补。"
)

TOPICS = [
    f"记住：我的机器只有 2C2G，方案里不要出现 Redis 集群、Kafka 集群这类重组件。"
    f"按这个前提，系统讲讲单机上能落地的实时链路该怎么搭。{LONG}",
    f"系统讲讲 Flink 的状态后端、checkpoint 流程、barrier 对齐，以及 Exactly-Once 靠什么成立。{LONG}",
    f"讲讲 Flink 的反压是怎么产生的、怎么定位、怎么解，每步给出可操作的手段。{LONG}",
    f"讲讲 Kafka 的副本同步、ISR 收缩、事务消息与幂等生产者的关系。{LONG}",
    f"讲讲数据倾斜的成因和主流解法，每种解法说清代价和适用边界。{LONG}",
    f"讲讲 JVM 内存结构、常见 GC、以及堆只有 1~1.2G 时该怎么调。{LONG}",
    JOB_PASTE,
    FACE_PASTE,
    f"基于前面聊的内容，判断我最薄弱的三个主题，并给出补强顺序和理由。{LONG}",
    f"讲讲实时数仓的分层建模、指标口径统一，以及小文件治理的具体做法。{LONG}",
    "最后回顾：我的机器配置、你一直遵守的约束、这两周的重点，以及这个岗位最看重什么？",
]

OVERSIZE_PROMPT = (
    "下面是我整理的一份岗位描述和我自己的项目复盘，材料比较长，"
    "请读完并给出：1) 岗位最看重的三项能力；2) 我现有经历里最匹配的两段；"
    "3) 最该补的一个短板。回答控制在 600 字内。\n\n"
)


def _max_ctx_id(sid: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id),0) m FROM ctx_traces WHERE session_id=?",
                           (sid,)).fetchone()
        return int(row["m"] or 0)
    finally:
        conn.close()


def _last_trace(sid: str) -> dict:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT total_tokens,budget,sections_json,dropped_msgs,anchored,summary_id,"
            "retrieved_n,memory_n,degraded_json,fallback FROM ctx_traces "
            "WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if not r:
            return {}
        d = dict(r)
        d["sections"] = json.loads(d.pop("sections_json") or "{}")
        d["degraded"] = json.loads(d.pop("degraded_json") or "[]")
        return d
    finally:
        conn.close()


def _history(sid: str, limit: int = 40) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,role,content FROM console_messages WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"]}
                for r in reversed(rows)]
    finally:
        conn.close()


# ---------------------------------------------------------------- A: 真实长回答
def part_a() -> tuple[str, list[dict]]:
    print("\n########## A. 真实长回答（10 轮） ##########", flush=True)
    sid, log = None, []
    for i, msg in enumerate(TOPICS, 1):
        t0 = time.perf_counter()
        res = console_service.chat(msg, sid, mode="assist")
        sid = res["session_id"]
        summarizer.summarize(sid)          # 与 API 的 BackgroundTasks 同函数
        tr = _last_trace(sid)
        rec = {
            "turn": i,
            "sec": round(time.perf_counter() - t0, 1),
            "reply_chars": len(res.get("reply") or ""),
            "total": tr.get("total_tokens"),
            "budget": tr.get("budget"),
            "pct": round(100 * (tr.get("total_tokens") or 0)
                         / max(1, tr.get("budget") or 1), 1),
            "sections": tr.get("sections"),
            "dropped": tr.get("dropped_msgs"),
            "anchored": tr.get("anchored"),
            "summary": tr.get("summary_id"),
            "retrieved": tr.get("retrieved_n"),
            "memory": tr.get("memory_n"),
            "degraded": tr.get("degraded"),
            "fallback": tr.get("fallback"),
        }
        log.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    return sid, log


# ---------------------------------------------------------- B: 预算阶梯扫描
def part_b(sid: str) -> None:
    print("\n########## B. 预算阶梯扫描（无 LLM） ##########", flush=True)
    hist = _history(sid)
    print(f"历史消息 {len(hist)} 条 / {sum(len(m['content'] or '') for m in hist)} 字", flush=True)
    sys_prompt = "你是求职准备助手，按用户约束给建议。"
    q = "再讲讲数据倾斜怎么处理"
    rows = []
    for cap in (12000, 10000, 9000, 8000, 7000, 6000, 5000, 4200, 3500, 2800, 2000):
        r = assembler.assemble(sid, sys_prompt, q, hist,
                               budget=Budget(soft_cap=cap), persist=False)
        st = r.stats
        row = {"cap": cap, "total": st["total"], "pct": st["usage_pct"],
               "degraded": st["degraded"], "fallback": st["fallback"],
               "sections": st["sections"], "kept_msgs": len(r.messages) - 2}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    print("\n-- 各级首次出现 --", flush=True)
    for lvl in ("drop_retrieval", "drop_memory", "shorten_summary", "drop_summary",
                "drop_oldest_turn", "keep_constraint"):
        hit = [r["cap"] for r in rows if lvl in r["degraded"]]
        if hit:
            print(f"{lvl:18s} 首次出现于 cap={hit[0]}（共 {len(hit)} 档触发）", flush=True)
        else:
            print(f"{lvl:18s} 本阶梯未触发", flush=True)

    # 对照组：每轮都带约束词（"不要说教"这种句式极常见）。
    # 这一组专门验证"保护有上界"——没有上界时一轮都丢不掉。
    print("\n-- 对照：每轮都含约束词 --", flush=True)
    cn, mid = [], 1
    for i in range(1, 9):
        cn.append({"id": mid, "role": "user", "content": f"第{i}轮：不要用 Redis，直接讲"})
        mid += 1
        cn.append({"id": mid, "role": "assistant",
                   "content": f"第{i}轮回复。" + "这是一段较长的技术说明。" * 110})
        mid += 1
    for cap in (12000, 9000, 7000, 5000, 3500):
        r = assembler.assemble(None, sys_prompt, q, cn,
                               budget=Budget(soft_cap=cap), persist=False)
        print(json.dumps({"cap": cap, "total": r.stats["total"],
                          "pct": r.stats["usage_pct"],
                          "turns_kept": (len(r.messages) - 2) // 2,
                          "degraded": r.stats["degraded"],
                          "fallback": r.stats["fallback"]}, ensure_ascii=False), flush=True)


# ------------------------------------------------------- C: 超长单轮输入
def part_c() -> None:
    print("\n########## C. 超长单轮输入（约 1.2 万字） ##########", flush=True)
    filler = (
        "岗位职责：负责数据平台核心链路的开发与维护，参与实时计算任务的性能优化，"
        "推动数据质量体系建设，与算法团队协作完成特征工程与指标口径统一。"
        "任职要求：熟悉 Java 与 Scala，熟悉 Flink/Kafka/Hive，有大规模数据治理经验，"
        "能独立定位线上性能问题。项目复盘：我在上一家公司负责实时数仓，"
        "把日均 3 亿条日志的链路从 40 分钟延迟压到 8 分钟，主要做了三件事："
        "拆分算子链路、给热点 key 加盐、把维表 join 改成 broadcast。"
    )
    body = OVERSIZE_PROMPT + filler * 62     # ≈ 1.2 万字
    print(f"输入长度 {len(body)} 字 / 估算 {count_tokens(body)} tokens", flush=True)
    res = console_service.chat(body, None, mode="assist")
    sid = res["session_id"]
    tr = _last_trace(sid)
    conn = get_conn()
    try:
        saved = conn.execute("SELECT LENGTH(content) n FROM console_messages "
                             "WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
                             (sid,)).fetchone()["n"]
    finally:
        conn.close()
    print(json.dumps({
        "total": tr.get("total_tokens"), "budget": tr.get("budget"),
        "pct": round(100 * (tr.get("total_tokens") or 0) / max(1, tr.get("budget") or 1), 1),
        "degraded": tr.get("degraded"), "fallback": tr.get("fallback"),
        "user_chars_in": len(body), "user_chars_saved": saved,
        "user_truncated": saved != len(body),
        "reply_chars": len(res.get("reply") or ""),
    }, ensure_ascii=False), flush=True)

    # 单条消息上限 20000 字，扣掉系统提示后单靠一条消息很难压过 12k 预算
    # （中文约 0.58 token/字）。所以再用缩小预算的装配器直接验证一次性标记：
    # 用户材料本身超预算一半以上时，应报 CTX_USER_OVERSIZE，且**一个字都不裁**。
    r = assembler.assemble(None, "系统提示", body, [], budget=Budget(soft_cap=6000),
                           persist=False)
    st = r.stats
    print(json.dumps({
        "probe": "assemble@6000", "total": st["total"], "budget": st["budget"],
        "user_tokens": st["sections"]["user"],
        "fallback": st["fallback"], "degraded": st["degraded"],
        "content_intact": r.messages[-1]["content"] == body,
    }, ensure_ascii=False), flush=True)


# ------------------------------------------------------------ D: 轮内裁剪
def part_d() -> None:
    print("\n########## D. 轮内裁剪（合成，无 LLM） ##########", flush=True)
    big = "工具返回的一大段原始数据，" * 400          # 每条约 4800 字
    msgs = [{"role": "system", "content": "系统提示"},
            {"role": "user", "content": "查一下最近的数据"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "list_cards", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "content": big})
    before = count_messages(msgs)
    out = assembler.enforce_inflight(msgs, Budget(soft_cap=12000))
    print(json.dumps({"before_tokens": before, **out,
                      "budget": 12000}, ensure_ascii=False), flush=True)
    lens = [len(m["content"]) for m in msgs if m["role"] == "tool"]
    print(f"8 条工具结果裁剪后长度: {lens}", flush=True)


def main() -> int:
    sid, log = part_a()
    levels = []
    for rec in log:
        for d in (rec["degraded"] or []):
            if d not in levels:
                levels.append(d)
    print(f"\n真实链路出现过的降级级别（按首次出现）: {levels or '无'}", flush=True)

    part_b(sid)
    part_c()
    part_d()
    print(f"\nA 段会话: {sid}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
