"""旧轮次摘要压缩：把窗口外的对话压成有界的三段式要点。

三个刻意的选择：

1. **落库而不是放内存**：摘要是"下一轮要注入的内容"，进程重启不能丢；
   `covered_to_id` 记录压到哪一条，避免重复压同一段（重复压 = 花钱把摘要再摘要一遍）。
2. **异步执行、下一轮生效**：摘要要走一次 LLM（1~3 秒），放在当轮同步链路里
   等于让用户等一件他看不见的事。代价是"刚被挤出去的那几轮"要等一轮才进摘要，
   这期间靠 `window` 的锚定规则兜住关键信息。
3. **失败就保持原样**：摘要失败只记 `CTX_SUMMARY_FAILED`，旧摘要继续用，
   下一轮再试。绝不因为压缩失败就让对话挂掉。
"""
import datetime

from ..core import llm
from ..core.db import get_conn
from .token_budget import count_tokens

MIN_BATCH = 4            # 少于这么多条不值得压（一次 LLM 调用换不回多少 token）
KEEP_RECENT_MSGS = 12    # 与 CTX_RECENT_TURNS*2 对齐，最近的不压
MAX_SUMMARY_CHARS = 1200


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def latest_summary(session_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM conversation_summary WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _pending(session_id: str, keep_recent: int) -> list[dict]:
    """还没被压过、且已经掉出"最近保留区"的消息。"""
    last = latest_summary(session_id)
    after = last["covered_to_id"] if last else 0
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, role, content FROM console_messages "
            "WHERE session_id=? AND id>? ORDER BY id", (session_id, after)).fetchall()]
    finally:
        conn.close()
    if len(rows) <= keep_recent:
        return []
    return rows[:-keep_recent]


def _transcript(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        who = "用户" if r["role"] == "user" else "助手"
        lines.append(f"{who}：{(r['content'] or '').strip()[:600]}")
    return "\n".join(lines)


def summarize(session_id: str, keep_recent: int = KEEP_RECENT_MSGS, force: bool = False) -> dict:
    """压一次。返回 {ok, summary_id?, covered?, error?}；不抛异常（异步任务里抛了就没人管）。"""
    rows = _pending(session_id, keep_recent)
    if len(rows) < MIN_BATCH and not force:
        return {"ok": True, "skipped": True, "pending": len(rows)}
    if not rows:
        return {"ok": True, "skipped": True, "pending": 0}

    prev = latest_summary(session_id)
    src_chars = sum(len(r["content"] or "") for r in rows)
    try:
        text = llm.summarize_context(prev["summary"] if prev else "", _transcript(rows))
    except Exception as e:  # noqa: BLE001  LLM 挂了不能影响对话
        return {"ok": False, "error": f"CTX_SUMMARY_FAILED: {type(e).__name__}: {e}",
                "session_id": session_id, "pending": len(rows)}

    text = (text or "").strip()[:MAX_SUMMARY_CHARS]
    if len(text) < 10:
        return {"ok": False, "error": "CTX_SUMMARY_FAILED: 模型返回空摘要",
                "session_id": session_id, "pending": len(rows)}

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO conversation_summary "
            "(session_id,covered_from_id,covered_to_id,n_messages,src_chars,summary,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, rows[0]["id"], rows[-1]["id"], len(rows), src_chars, text, _now()))
        conn.commit()
        sid = cur.lastrowid
    finally:
        conn.close()

    return {"ok": True, "summary_id": sid, "covered": [rows[0]["id"], rows[-1]["id"]],
            "n_messages": len(rows), "src_chars": src_chars,
            "summary_tokens": count_tokens(text),
            "compression": round(src_chars / max(1, len(text)), 1)}


def format_for_prompt(summary: str, max_tokens: int | None = None) -> str:
    """把摘要包成注入 system 的一段；超预算就按行截断（保留前面的事实/进度）。"""
    head = "【更早对话的摘要（原文已移出窗口，新信息以当前轮为准）】"
    if not summary:
        return ""
    text = summary.strip()
    if max_tokens:
        lines, used, kept = text.splitlines(), 0, []
        for ln in lines:
            t = count_tokens(ln)
            if used + t > max_tokens:
                kept.append("…（摘要已按预算截断）")
                break
            kept.append(ln)
            used += t
        text = "\n".join(kept)
    return f"{head}\n{text}"


def stats(session_id: str | None = None) -> dict:
    conn = get_conn()
    try:
        where, args = ("WHERE session_id=?", (session_id,)) if session_id else ("", ())
        row = conn.execute(
            f"SELECT COUNT(*) AS n, SUM(src_chars) AS src, SUM(LENGTH(summary)) AS out "
            f"FROM conversation_summary {where}", args).fetchone()
        return {"summaries": row["n"] or 0, "src_chars": row["src"] or 0, "out_chars": row["out"] or 0,
                "ratio": round((row["src"] or 0) / max(1, row["out"] or 1), 1)}
    finally:
        conn.close()
