"""按需检索注入：只给"这一轮真的用得上"的东西，不是把知识库倒进 prompt。

复用已有的双路检索（FTS + 向量 → RRF），不新造轮子：
`rag_service.hybrid_search(scope="cards")` 已经是评测过的基线（hit@5 0.737）。

两条路分开对待，因为它们的可信度不同：
- **知识卡**：全局检索，命中就注入（它是"我学过什么"，跨会话也有用）；
- **历史摘要**：只在当前会话内按关键词匹配。跨会话摘要语义上很诱人，
  但实测噪音大（上一个话题的结论会被当成本话题的前提），先不跨。
"""
import json

from ..core import config
from ..core.db import get_conn
from ..services import rag_service
from . import summarizer
from .token_budget import count_tokens

RRF_K = 60
SNIPPET_CHARS = 220

# 命中太低就不给——宁可少给材料，也不要给一堆只共享个别字的卡片
# （评分场景踩过这个坑：跨主题卡片会系统性压低分数）
MIN_QUERY_GRAMS = 2


def _grams(text: str, n: int = 2) -> set[str]:
    t = "".join(ch for ch in (text or "") if not ch.isspace())
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _summary_hits(query: str, session_id: str | None, top_k: int) -> list[dict]:
    """历史摘要按 2-gram 重合度打分。摘要都很短，不需要上向量。"""
    if not session_id:
        return []
    qg = _grams(query)
    if len(qg) < MIN_QUERY_GRAMS:
        return []
    last = summarizer.latest_summary(session_id)
    if not last:
        return []
    sg = _grams(last["summary"])
    overlap = len(qg & sg)
    if overlap < MIN_QUERY_GRAMS:
        return []
    return [{"kind": "summary", "id": f"sum-{last['id']}", "title": "更早对话的摘要",
             "text": last["summary"][:SNIPPET_CHARS], "score": overlap / max(1, len(qg))}]


def retrieve(query: str, session_id: str | None = None, top_k: int | None = None,
             max_tokens: int | None = None) -> list[dict]:
    """返回按 RRF 融合排序的候选：[{kind, id, title, text, score, legs}]。

    不做"相关性阈值硬过滤"之外的花活：融合已经是两路投票，分数越低越该丢，
    真正决定给多少的是 assembler 的 token 预算。
    """
    top_k = top_k or config.CTX_RETRIEVE_TOPK
    fused: dict[str, dict] = {}

    def add(items: list[dict], leg: str):
        for rank, it in enumerate(items):
            key = f"{it['kind']}:{it['id']}"
            entry = fused.setdefault(key, {**it, "score": 0.0, "legs": []})
            entry["score"] += 1.0 / (RRF_K + rank + 1)
            entry["legs"].append(leg)

    cards = []
    try:
        for h in rag_service.hybrid_search(query, "cards", top_k=max(3, top_k * 2)):
            row = _card_snippet(h["id"])
            if row:
                cards.append(row)
    except Exception:  # noqa: BLE001  检索挂了不该阻断对话
        cards = []
    add(cards, "card")

    add(_summary_hits(query, session_id, top_k), "summary")

    ranked = sorted(fused.values(), key=lambda x: -x["score"])
    if max_tokens:
        kept, used = [], 0
        for it in ranked:
            t = count_tokens(it["text"]) + 8
            if used + t > max_tokens:
                break
            kept.append(it)
            used += t
        ranked = kept
    return ranked[: max(top_k, 1)]


def _card_snippet(card_id: str) -> dict | None:
    conn = get_conn()
    try:
        c = conn.execute(
            "SELECT id, title, topic, points_json FROM kb_cards WHERE id=? AND status='active'",
            (card_id,)).fetchone()
        if not c:
            return None
        points = json.loads(c["points_json"]) if c["points_json"] else []
        text = "\n".join(f"- {p}" for p in points[:3]) or "（要点待补充）"
        return {"kind": "card", "id": c["id"], "title": c["title"], "topic": c["topic"],
                "text": text[:SNIPPET_CHARS]}
    finally:
        conn.close()


def format_for_prompt(hits: list[dict]) -> str:
    if not hits:
        return ""
    blocks = []
    for h in hits:
        label = "知识卡" if h["kind"] == "card" else "历史摘要"
        topic = f"｜{h.get('topic')}" if h.get("topic") else ""
        blocks.append(f"《{h['title']}》（{label}{topic}）\n{h['text']}")
    return "【这一轮检索到的相关材料（按需注入，可能不完整）】\n" + "\n\n".join(blocks)
