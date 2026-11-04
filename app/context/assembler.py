"""统一组装出口：所有给模型的 messages 都从这里出去。

一个请求的输入由五块拼成，每块都有独立预算：

    system（规则+环境快照）  ≤ 15%
    摘要（更早对话压缩）     ┐
    检索（相关卡片）         ├ 25%（"检索"这一档，摘要也算在里面）
    历史（窗口内消息）       ≤ 60%
    本轮用户输入             不设上限（见下）

**降级顺序是固定且可观测的**：检索 → 摘要 → 最旧轮次。理由：
检索和摘要都是"锦上添花"（没有也能答，只是答得粗）；历史轮次里可能有约束，
最后才动。每降一级都写进 `ctx_traces.degraded_json`，所以"这轮为什么没给检索材料"
是可查的，不是玄学。

**唯一不裁的是本轮用户输入**：用户粘一份 2 万字面经，被截断就等于让他白粘。
这种情况会记 `CTX_USER_OVERSIZE` 并如实报总 token——不假装守住了预算。
真正的解法是长材料先落盘再引用 id（TODO，见复盘文档"后续"）。
"""
import datetime
import json

from ..core import config
from ..core.db import get_conn
from . import memory_retriever, summarizer, window
from .token_budget import Budget, count_messages, count_tokens, default_budget, log_ctx

CTX_VERSION = 1
TRUNC_MARK = "…（已按上下文预算截断）"
TOOL_KEEP_CHARS = 600     # 轮内裁剪后每条工具结果保留的字符数


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class CtxResult:
    def __init__(self, messages: list[dict], stats: dict):
        self.messages = messages
        self.stats = stats

    def as_dict(self) -> dict:
        return {"sections": self.stats.get("sections", {}), "total": self.stats.get("total"),
                "budget": self.stats.get("budget"), "dropped": self.stats.get("dropped", 0),
                "anchored": self.stats.get("anchored", 0),
                "summary_used": self.stats.get("summary_used", False),
                "retrieved_n": self.stats.get("retrieved_n", 0),
                "degraded": self.stats.get("degraded", []),
                "fallback": self.stats.get("fallback")}


def _join_system(system: str, summary_block: str, retrieval_block: str) -> str:
    parts = [system.strip()]
    if summary_block:
        parts.append(summary_block)
    if retrieval_block:
        parts.append(retrieval_block)
    return "\n\n".join(p for p in parts if p)


def _drop_oldest_soft(kept: list[dict]) -> list[dict] | None:
    """丢掉最旧的一"软"轮（不含用户约束轮的整轮）。

    返回 None 表示剩下的全是约束轮——这时候"守预算"要让位给"别丢用户说过的规则"。
    """
    turns = window.group_turns(kept)
    for i, t in enumerate(turns):
        if any(window.is_constraint(m) for m in t):
            continue
        if len(turns) - i <= 1:        # 最后一轮不动
            return None
        rest = turns[:i] + turns[i + 1:]
        return [m for tt in rest for m in tt]
    return None


def assemble(session_id: str | None, system: str, user_msg: str, history: list[dict],
             budget: Budget | None = None, recent_turns: int | None = None,
             use_retrieval: bool = True, persist: bool = True) -> CtxResult:
    """装配一次请求。永不抛异常（降级到底也要能发出请求）。"""
    budget = budget or default_budget()
    recent_turns = recent_turns if recent_turns is not None else config.CTX_RECENT_TURNS
    degraded: list[str] = []

    # 1) 历史：先剪掉"已被摘要覆盖"的部分，再走窗口 + 锚定
    #    不剪就会重复计价：同一段内容既在摘要里、又在原文里，花两份钱还互相矛盾。
    history = list(history or [])
    covered_removed = 0
    summary_row = None
    if session_id:
        try:
            summary_row = summarizer.latest_summary(session_id)
        except Exception as e:  # noqa: BLE001  摘要表缺失/读失败绝不能让对话挂掉
            degraded.append(f"summary_unavailable:{type(e).__name__}")
            log_ctx(error=f"summary_read:{type(e).__name__}")
    if summary_row:
        covered = summary_row["covered_to_id"]
        kept_hist = [m for m in history if m.get("id") is None or m["id"] > covered]
        covered_removed = len(history) - len(kept_hist)
        history = kept_hist

    sel = window.select(history, budget.alloc("history"), recent_turns)
    summary_block = ""
    if summary_row and (covered_removed or sel.dropped or len(history) > recent_turns * 2):
        summary_block = summarizer.format_for_prompt(summary_row["summary"], budget.alloc("retrieval"))

    # 3) 检索：按本轮问题取材料
    hits = []
    if use_retrieval and user_msg and len(user_msg) < 4000:
        hits = memory_retriever.retrieve(user_msg, session_id, max_tokens=budget.alloc("retrieval"))
    retrieval_block = memory_retriever.format_for_prompt(hits)

    def build():
        sys_text = _join_system(system, summary_block, retrieval_block)
        # 只给 API 认识的字段：历史里带着 id 是为了"被摘要覆盖"判定，不能透传给模型
        msgs = [{"role": "system", "content": sys_text}] + [
            {"role": m.get("role") or "user", "content": m.get("content") or ""} for m in sel.kept]
        if user_msg:
            msgs.append({"role": "user", "content": user_msg})
        return msgs, sys_text

    messages, sys_text = build()
    total = count_messages(messages)

    # 4) 降级：检索 → 摘要 → 最旧轮次
    if total > budget.usable and retrieval_block:
        degraded.append("drop_retrieval")
        retrieval_block, hits = "", []
        messages, sys_text = build()
        total = count_messages(messages)

    if total > budget.usable and summary_block:
        half = max(80, budget.alloc("retrieval") // 2)
        tightened = summarizer.format_for_prompt(summary_row["summary"], half)
        if count_tokens(tightened) < count_tokens(summary_block):
            degraded.append("shorten_summary")
            summary_block = tightened
        else:
            degraded.append("drop_summary")
            summary_block = ""
        messages, sys_text = build()
        total = count_messages(messages)

    while total > budget.usable and len(sel.kept) > 2:
        kept = _drop_oldest_soft(sel.kept)
        if kept is None:
            degraded.append("keep_constraint")     # 只剩约束轮与最近轮，宁可超也不丢
            break
        degraded.append("drop_oldest_turn")
        sel.kept = kept
        sel.force_dropped += 1
        messages, sys_text = build()
        total = count_messages(messages)

    # 5) 兜底：用户输入本身超预算就不裁，但要如实记账
    fallback = None
    user_tokens = count_tokens(user_msg)
    if total > budget.usable:
        if "keep_constraint" in degraded:
            fallback = "CTX_KEEP_CONSTRAINT"
        else:
            fallback = "CTX_USER_OVERSIZE" if user_tokens > budget.usable * 0.5 else "CTX_OVER_BUDGET"

    sections = {
        "system": count_tokens(system),
        "summary": count_tokens(summary_block),
        "retrieval": count_tokens(retrieval_block),
        "history": count_messages(sel.kept),
        "user": user_tokens,
    }
    stats = {
        "version": CTX_VERSION, "sections": sections, "total": total,
        "budget": budget.usable, "dropped": len(sel.dropped), "anchored": sel.anchored,
        "summary_used": bool(summary_block), "summary_id": summary_row["id"] if summary_row else None,
        "retrieved_n": len(hits), "degraded": degraded, "fallback": fallback,
        "usage_pct": round(100 * total / max(1, budget.usable), 1),
    }
    log_ctx(session=session_id, total=total, budget=budget.usable, pct=stats["usage_pct"],
            hist=len(sel.kept), dropped=len(sel.dropped), anchored=sel.anchored,
            summary=1 if summary_block else 0, retrieved=len(hits),
            degraded=",".join(degraded) or "-", fallback=fallback)

    if persist:
        _persist(session_id, stats)
    return CtxResult(messages, stats)


def enforce_inflight(messages: list[dict], budget: Budget | None = None,
                     keep_last_tool: int = 1) -> dict:
    """轮内裁剪：ReAct 循环里工具结果会一轮轮堆进去，这是最容易爆的地方。

    8 步 × 2600 字的工具结果 ≈ 2 万字符，比历史更危险，而且**越早的工具结果越没用**
    （模型已经从里面取了结论）。

    分三档收紧，"保护最后一条"是偏好不是保证：
      1) 非末尾工具结果 → 裁到 600 字；
      2) 还不够 → 连末尾一起裁到 600 字（模型要读的那条也可能很大）；
      3) 还不够 → 全部裁到 200 字。工具结果是"可再取"的，宁可让模型重新调一次工具。
    """
    budget = budget or default_budget()
    before = count_messages(messages)
    trimmed = 0
    if before <= budget.usable:
        return {"before": before, "after": before, "trimmed": 0, "saved": 0,
                "over": False}

    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protected = set(tool_idx[-keep_last_tool:]) if keep_last_tool else set()

    def trim(targets: list[int], limit: int) -> int:
        n = 0
        for i in targets:
            content = messages[i].get("content") or ""
            if len(content) > limit:
                messages[i]["content"] = content[:limit] + TRUNC_MARK
                n += 1
        return n

    def over() -> bool:
        return count_messages(messages) > budget.usable

    trimmed += trim([i for i in tool_idx if i not in protected], TOOL_KEEP_CHARS)
    if over():
        trimmed += trim(list(tool_idx), TOOL_KEEP_CHARS)
    if over():
        trimmed += trim(list(tool_idx), 200)

    after = count_messages(messages)
    return {"before": before, "after": after, "trimmed": trimmed,
            "saved": before - after, "over": after > budget.usable}


def _persist(session_id: str | None, stats: dict) -> None:
    if not session_id:
        return
    conn = get_conn()
    try:
        seq = (conn.execute("SELECT COUNT(*) AS n FROM ctx_traces WHERE session_id=?",
                            (session_id,)).fetchone()["n"] or 0) + 1
        conn.execute(
            "INSERT INTO ctx_traces (session_id,seq,budget,total_tokens,sections_json,"
            "dropped_msgs,anchored,summary_id,retrieved_n,degraded_json,fallback,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, seq, stats["budget"], stats["total"],
             json.dumps(stats["sections"], ensure_ascii=False), stats["dropped"],
             stats["anchored"], stats.get("summary_id"), stats["retrieved_n"],
             json.dumps(stats["degraded"], ensure_ascii=False), stats["fallback"], _now()))
        conn.commit()
    except Exception as e:  # noqa: BLE001  记账失败不能影响对话
        log_ctx(error=f"ctx_trace_persist:{type(e).__name__}")
    finally:
        conn.close()


def recent_traces(session_id: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM ctx_traces WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sections"] = json.loads(d.pop("sections_json") or "{}")
            d["degraded"] = json.loads(d.pop("degraded_json") or "[]")
            out.append(d)
        return out
    finally:
        conn.close()
