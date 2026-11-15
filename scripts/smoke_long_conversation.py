"""长对话 + 记忆闭环的真实链路验证（不是 mock）。

跑法（容器内）：
    docker exec job-prep-api-1 python scripts/smoke_long_conversation.py

它做的事：
  1. 用 console_service.chat() 跑 20 轮真实对话——与 API 同一条代码路径，
     唯一区别是摘要不靠 BackgroundTasks，而是在每轮后同步调一次（同函数、同参数）；
  2. 每轮记录：上下文装配账本（total/degraded/summary_used/anchored）、
     本轮新增的 ctx_traces 行数、summary 生成结果；
  3. 结束时输出：摘要表全貌、summary_used 首次出现的轮次、降级序列、
     以及第 1 轮约束是否被锚定到最后（P0 保护是否真的生效）。

为什么需要它：单元测试只能证明"代码逻辑对"，证明不了"真实 LLM 输出
会不会触发某条分支"。库里 conversation_summary 长期为 0，说明这条链路
从未在真实对话长度下跑过——这个脚本就是补这一格。

成本：20 轮 × 每轮 2~4 次 LLM 调用，deepseek-chat 下约 ¥0.1~0.3。
"""
import json
import sys
import time

sys.path.insert(0, "/app")

from app.context import summarizer          # noqa: E402
from app.core.db import get_conn           # noqa: E402
from app.services import console_service   # noqa: E402

MESSAGES = [
    "记住：我的服务器只有 2C2G，方案里不要出现 Redis、Kafka 集群这类重组件。",
    "先说说我现在的复习重点应该怎么排。",
    "帮我看下 q-a1b2c3d4 这张卡还在不在？",
    "解释一下 Flink 的 checkpoint 和 savepoint 区别，一句话。",
    "Kafka 分区数是不是越多越好？",
    "我刚才说的机器配置，对我的方案选型有什么影响？",
    "数据倾斜一般怎么处理？",
    "Hive 分区分桶的区别，简短说。",
    "继续，说说小文件问题。",
    "我刚才问过 Flink 什么？",
    "Java 线程池核心参数有哪些？",
    "那拒绝策略怎么选？",
    "MySQL 索引失效的常见场景。",
    "刚才的 2C2G 约束，你推荐方案时有没有一直遵守？",
    "Redis 在 2C2G 上还能用吗？",
    "总结一下我这几轮都问了什么主题。",
    "其中哪个主题我最薄弱？",
    "给我一道 Flink 的面试题。",
    "这题的标准答案要点是什么？",
    "回顾一下：我的机器配置和复习重点分别是什么？",
]


def _max_ctx_id(sid: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM ctx_traces WHERE session_id=?",
                           (sid,)).fetchone()
        return int(row["m"] or 0)
    finally:
        conn.close()


def _new_ctx_rows(sid: str, after_id: int) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,seq,total_tokens,budget,dropped_msgs,anchored,summary_id,"
            "memory_n,retrieved_n,degraded_json,fallback FROM ctx_traces "
            "WHERE session_id=? AND id>? ORDER BY id", (sid, after_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["degraded"] = json.loads(d.pop("degraded_json") or "[]")
            out.append(d)
        return out
    finally:
        conn.close()


def main() -> int:
    sid = None
    print(f"=== 开始 {len(MESSAGES)} 轮真实对话 ===", flush=True)
    for i, msg in enumerate(MESSAGES, 1):
        after = _max_ctx_id(sid) if sid else 0
        t0 = time.perf_counter()
        try:
            res = console_service.chat(msg, sid, mode="assist")
        except Exception as e:  # noqa: BLE001  单轮失败不该让整场实验白跑
            print(f"[turn {i:02d}] CHAT_FAILED {type(e).__name__}: {e}", flush=True)
            return 2
        sid = res["session_id"]
        dt = time.perf_counter() - t0

        # 与 API 的 BackgroundTasks 同函数同参数：真实的环境下它在响应结束后跑
        sm = summarizer.summarize(sid)
        rows = _new_ctx_rows(sid, after)
        last = rows[-1] if rows else {}
        usage = (res.get("ctx") or {})

        print(json.dumps({
            "turn": i,
            "sec": round(dt, 1),
            "reply_chars": len(res.get("reply") or ""),
            "ctx_rows": len(rows),
            "total": last.get("total_tokens"),
            "budget": last.get("budget"),
            "anchored": last.get("anchored"),
            "dropped": last.get("dropped_msgs"),
            "summary_used": bool(last.get("summary_id")),
            "summary_id": last.get("summary_id"),
            "memory_n": last.get("memory_n"),
            "retrieved_n": last.get("retrieved_n"),
            "degraded": last.get("degraded"),
            "fallback": last.get("fallback"),
            "summarize": {k: v for k, v in sm.items() if k in
                          ("ok", "skipped", "pending", "summary_id", "covered",
                           "n_messages", "compression", "error")},
            "ctx_acc": {k: usage.get(k) for k in
                        ("total", "degraded", "summary_used", "memory_n", "retrieved_n")},
        }, ensure_ascii=False), flush=True)

    print("\n=== 摘要表 ===", flush=True)
    conn = get_conn()
    try:
        for r in conn.execute(
                "SELECT id,covered_from_id,covered_to_id,n_messages,src_chars,"
                "LENGTH(summary) AS out_len,created_at FROM conversation_summary "
                "WHERE session_id=? ORDER BY id", (sid,)).fetchall():
            print(dict(r), flush=True)
        msgs = conn.execute("SELECT COUNT(*) n FROM console_messages WHERE session_id=?",
                            (sid,)).fetchone()["n"]
        used = conn.execute(
            "SELECT COUNT(*) n FROM ctx_traces WHERE session_id=? AND summary_id IS NOT NULL",
            (sid,)).fetchone()["n"]
        total_ctx = conn.execute("SELECT COUNT(*) n FROM ctx_traces WHERE session_id=?",
                                 (sid,)).fetchone()["n"]
        mem = conn.execute("SELECT COUNT(*) n FROM agent_memory WHERE session_id=? "
                           "AND superseded_by IS NULL", (sid,)).fetchone()["n"]
    finally:
        conn.close()

    print(f"\n会话: {sid}")
    print(f"消息数: {msgs} | ctx 装配次数: {total_ctx} | 注入过摘要的轮次: {used}")
    print(f"本会话记忆条数: {mem}")
    print("PASS" if used > 0 else "FAIL: 摘要从未被注入", flush=True)
    return 0 if used > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
