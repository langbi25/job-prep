"""上下文组装器（Context Engineering）。

原则：给 LLM 的每一分上下文都要"相关、有界、可溯源"。
评分场景的上下文 = 同主题知识卡片要点（他学过什么）+ 题目池参考答案 +
该主题历史错题（他曾栽在哪），按字符预算截断，防止上下文膨胀。

踩过的坑（v2 修）：原先会把"语义最近"的卡片无条件注入，导致问 LLM 幻觉时
把 Kafka/Redis 卡片塞进【参考要点】，面试官据此判"回答与参考要点完全无关"并压分——
知识库没覆盖的主题反而被系统性扣分。跨主题的相似卡片现在是**噪音**而非证据，
所以语义补充只在同主题内生效，且必须有足够相似度。
"""
import json

from ..core import embedder
from ..core.db import get_conn
from .rag_service import hybrid_search

MAX_CHARS = 1500
SIM_MIN = 0.55          # 语义补充的余弦下限，低于此值宁可不给材料
MAX_CARDS = 2
SECTION_TITLES = {"cards": "知识卡片", "answer_points": "题目池参考答案", "mistakes": "该主题历史错题"}


def _fmt_card(c) -> str:
    points = json.loads(c["points_json"]) if isinstance(c["points_json"], str) else []
    body = "\n".join(f"- {p}" for p in points[:4]) or "（要点待补充）"
    return f"《{c['title']}》\n{body}"


def _semantic_card_ids(question_text: str, topic: str | None) -> list[str]:
    """语义补充召回：同主题 + 高相似度才算候选，否则视为噪音丢弃。"""
    if not embedder.available():
        return []
    try:
        hits = embedder.search_similar(question_text, "card", top_k=3)
    except Exception:
        return []
    conn = get_conn()
    try:
        out = []
        for cid, score in hits:
            if score < SIM_MIN:
                break
            row = conn.execute("SELECT topic FROM kb_cards WHERE id=?", (cid,)).fetchone()
            if not row:
                continue
            if topic and row["topic"] != topic:
                continue  # 跨主题不注入：相关性无法保证，只会误导评分
            out.append(cid)
            if len(out) >= MAX_CARDS:
                break
        return out
    finally:
        conn.close()


def build_eval_context(topic: str | None, question_text: str) -> str:
    sections: list[tuple[str, str]] = []
    card_ids: set[str] = set()

    def add(kind: str, text: str):
        sections.append((kind, text))

    conn = get_conn()
    try:
        # 1) 同主题卡片（他学过什么）——最可靠的材料
        if topic:
            for c in conn.execute(
                "SELECT * FROM kb_cards WHERE topic=? AND status='active' LIMIT ?",
                (topic, MAX_CARDS)).fetchall():
                add("cards", _fmt_card(c))
                card_ids.add(c["id"])

        # 2) 语义补充（换种问法也能对上），受同主题 + 相似度双约束
        for cid in _semantic_card_ids(question_text, topic):
            if cid in card_ids:
                continue
            c = conn.execute("SELECT * FROM kb_cards WHERE id=?", (cid,)).fetchone()
            if c:
                add("cards", _fmt_card(c))
                card_ids.add(cid)

        # 3) 题目池参考答案要点
        q = conn.execute(
            "SELECT answer_points_json FROM question_pool WHERE text=?", (question_text,)
        ).fetchone()
        if q:
            pts = json.loads(q["answer_points_json"]) if q["answer_points_json"] else []
            if pts:
                add("answer_points", "\n".join(f"- {p}" for p in pts[:4]))

        # 4) 该主题历史错题（他曾栽在哪）
        if topic:
            for m in conn.execute(
                "SELECT m.raw_text, m.cause_type FROM mistakes m "
                "LEFT JOIN question_pool q ON q.id=m.question_id "
                "WHERE q.topic=? AND m.resolved=0 ORDER BY m.created_at DESC LIMIT 2",
                (topic,),
            ).fetchall():
                add("mistakes", f"[{m['cause_type']}] {m['raw_text'][:60]}")
    finally:
        conn.close()

    # 去重 + 按预算组装
    seen, lines, used = set(), [], 0
    for kind, text in sections:
        key = text[:50]
        if key in seen:
            continue
        seen.add(key)
        chunk = f"【{SECTION_TITLES[kind]}】\n{text}"
        if used + len(chunk) > MAX_CHARS:
            break
        lines.append(chunk)
        used += len(chunk)
    if not lines:
        return "（知识库中暂无该主题资料，按通用标准评分，不因缺少知识库材料而扣分）"
    return "\n\n".join(lines)
