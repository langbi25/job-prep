"""记忆管理：自动抽取 + 显式写入 + 上下文注入。

抽取为什么要异步：
一轮对话本身已经 3~4 次 LLM 往返（10~20 秒），再串一次抽取会让用户多等 1 秒，
而这件事**用户不需要看见**。异步的代价是"这一轮说的事，下一轮才生效"——
对记忆来说完全可以接受，因为记忆本来就是给下一轮用的。

抽取为什么要克制：
模型很容易把"我同学在字节"总结成"用户想进字节"，把一次性的工具结果当成事实。
所以 prompt 里给了明确的"不记"清单，且置信度默认 0.6（低于显式写入的 0.9），
配合 store 的"低置信不覆盖高置信"，脏记忆不会顶掉用户亲口说的事实。
"""
import json
import threading

from ..core import llm, pricing, prompts
from ..observability import tracing as tr
from . import recall as recall_mod
from . import store

EXTRACT_MIN_CHARS = 6          # 太短的消息（"好""继续"）没有抽取价值
EXTRACT_CONF = 0.6             # 自动抽取的默认置信度（显式 remember 用 0.9）
EXPLICIT_CONF = 0.9
# 模型没给 scope 时的兜底。按 kind 分，不搞"一律 session"：
# 一律 session 的后果是**换一个会话就等于失忆**——实测 8 条记忆全是 session，
# 新会话问"我为什么总挂"什么都不记得。偏好和稳定事实本来就该跨会话。
DEFAULT_SCOPE = {"preference": "global", "fact": "global",
                 "task_state": "session", "episode": "session"}


def remember(kind: str, key: str, value: str, *, scope: str = "session",
             session_id: str | None = None, confidence: float | None = None,
             ttl_days: int | None = None, source: str = "tool",
             ref: dict | None = None) -> dict:
    """显式写入（工具调用）。scope=global 表示跨会话长期有效。"""
    res = store.set_memory(
        kind, key, value, scope=scope, scope_id=session_id if scope == "session" else None,
        confidence=EXPLICIT_CONF if confidence is None else confidence,
        source=source, session_id=session_id, ttl_days=ttl_days, ref=ref)
    store.sweep()
    return res


def remember_many(items: list[dict], *, session_id: str | None = None,
                  source: str = "extract") -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        key = (it.get("key") or "").strip()
        value = (it.get("value") or "").strip()
        if not key or not value:
            continue
        kind = it.get("kind") if it.get("kind") in store.KINDS else "fact"
        scope = it.get("scope") if it.get("scope") in ("global", "session") else DEFAULT_SCOPE.get(kind, "session")
        out.append(store.set_memory(
            kind, key, value,
            scope=scope, scope_id=session_id if scope == "session" else None,
            confidence=float(it.get("confidence") or EXTRACT_CONF),
            source=source, session_id=session_id,
            ref={"turn": it.get("turn")}, ttl_days=it.get("ttl_days")))
    if out:
        store.sweep()
    return out


def extract(message: str, reply: str, *, session_id: str, turn: int = 0,
            trace_id: str = "") -> list[dict]:
    """从一轮对话里抽取值得长期记住的信息。失败静默返回 []（记忆不能影响主流程）。"""
    msg = (message or "").strip()
    if len(msg) < EXTRACT_MIN_CHARS:
        return []
    existing = store.list_memory(scope="global", limit=30)
    if session_id:
        existing += store.list_memory(scope="session", scope_id=session_id, limit=20)
    known = "\n".join(f"- [{e['kind']}] {e['key']}：{e['value'][:80]}" for e in existing) or "（空）"

    try:
        system, user, _version = prompts.render(
            "memory_extract", message=msg[:3000], reply=(reply or "")[:1500], known=known)
        with tr.span("memory.extract", tr.KIND_CHAIN, {
                "memory.session_id": session_id, "memory.turn": turn,
                "memory.parent_trace": trace_id or "-"}) as s:
            data = llm.chat_parsed(system, user)
            items = data.get("memories") or []
            if s is not None:
                s.set_attribute("memory.extracted", len(items))
    except Exception:  # noqa: BLE001  抽取是增强，不是前置条件
        return []

    clean = []
    for it in items[:6]:
        if not isinstance(it, dict):
            continue
        value = (it.get("value") or "").strip()
        key = (it.get("key") or "").strip()
        if len(value) < 2 or not key or len(key) > store.MAX_KEY_CHARS:
            continue
        clean.append({**it, "turn": turn})
    return remember_many(clean, session_id=session_id, source="extract")


def extract_async(message: str, reply: str, *, session_id: str, turn: int = 0,
                  trace_id: str = "") -> None:
    """后台线程抽取。daemon=True：进程退出不等它，避免拖住关停。"""
    # 在**父线程**抓一份链路上下文，交给子线程继承。
    # 只传 trace_id 不够：trace 相同但 parent 为空，memory.extract 会变成孤儿，
    # 瀑布图上它飘在根旁边，看不出"这是那一轮派生的旁路"。实测抓到过这种。
    ctx = tr.capture()

    def _run():
        try:
            tr.adopt(ctx)
            # 线程不继承 contextvar：不显式设置的话，这次 LLM 调用的费用在
            # 成本台账里会落到 scene=unknown、没有会话归属，按场景/会话聚合就缺一角。
            tr.set_scene("memory_extract")
            tr.set_session(session_id or "", turn)
            # trace_id 也要显式继承：否则这次调用在台账里没有身份，
            # 无法归到触发它的那一轮（它是"同一轮的旁路"，不是独立事件）。
            tr.set_trace(trace_id)
            # 用收集器量出这次抽取自己花了多少，回填到那一轮：
            # 主循环的账由 turn_facts 从台账聚合，旁路只能事后加。
            with pricing.collect() as rows:
                extract(message, reply, session_id=session_id, turn=turn, trace_id=trace_id)
            agg = pricing.accumulate(rows)
            if agg["calls"]:
                from ..services import turn_facts
                turn_facts.bump_aux(session_id, turn, calls=agg["calls"],
                                    tokens=agg["total_tokens"], cost=agg["cost_cny"])
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, name=f"mem-extract-{turn}", daemon=True).start()


def recall(query: str, *, session_id: str | None = None, kinds: list[str] | None = None,
           top_k: int = 6, max_tokens: int | None = None) -> list[dict]:
    return recall_mod.recall(query, session_id=session_id, kinds=kinds,
                             top_k=top_k, max_tokens=max_tokens)


def prompt_block(query: str, *, session_id: str | None = None, max_tokens: int | None = None,
                 top_k: int = 6) -> tuple[str, int]:
    """给 assembler 用：返回 (注入文本, 命中条数)。"""
    hits = recall(query, session_id=session_id, top_k=top_k, max_tokens=max_tokens)
    return recall_mod.format_for_prompt(hits), len(hits)


def dump(session_id: str | None = None) -> dict:
    """面板用：当前记忆全貌。

    不按 session 过滤：面板要看的是"系统一共记住了什么"，
    只列 global 会出现"统计说 5 条、列表是空"的错位（会话域的记忆恰恰是最多的）。
    """
    memories = store.list_memory(limit=300)
    if session_id:
        # 指定会话时，让该会话的记忆排前面，方便对照"这轮用到了什么"
        memories.sort(key=lambda m: 0 if m.get("scope_id") == session_id else 1)
    return {"stats": store.stats(), "memories": memories,
            "vector": recall_mod.vector_status()}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
