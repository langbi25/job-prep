"""混合检索（RAG 核心）：FTS 关键词路 + 向量语义路 → RRF 倒数排名融合。

为什么混合：FTS 对"专有名词精确命中"强（如 Tungsten-sort），向量对"换种问法"强
（如"消息不丢"≈"可靠性保证"），两路互补，RRF 无需调参融合排名。
"""
import json
import re

from ..core import embedder
from ..core.db import get_conn

RRF_K = 60

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_+#.\-]{2,}")


def _fts_query(query: str) -> str:
    """把自然语言查询编译成 FTS5 可用的 OR 表达式。

    SQLite 内置 unicode61 分词器把连续中文当成**一个** token，所以整句中文
    （"面试官问我消息丢了怎么办"）无法命中任何文档——中文 FTS 实际失效。
    这里自己做切分：英文/数字词原样保留，中文按 2-gram 展开后 OR。
    注意查询词必须用**前缀**（"数据"*）而不是精确匹配——文档侧同样被 unicode61
    切成一个长 token（"数据倾斜"），精确匹配 bigram 会全部落空。
    bigram 会带来一些噪声，所以查询端统一用 bm25 排序，靠相关性把噪声压下去。
    """
    terms: list[str] = _WORD_RE.findall(query)
    for run in _CJK_RE.findall(query):
        if len(run) <= 2:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    seen, uniq = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return " OR ".join(f'"{t}"*' for t in uniq)


def _card_text(c) -> str:
    return " ".join([c["title"], c["question"] or "", c["points_json"]])


def ensure_all_indexed() -> dict:
    """懒索引：为所有卡片和题目建立/更新向量。"""
    if not embedder.available():
        return {"enabled": False, "cards": 0, "questions": 0}
    conn = get_conn()
    try:
        cards = [(r["id"], _card_text(r)) for r in conn.execute(
            "SELECT * FROM kb_cards WHERE status='active'").fetchall()]
        questions = [(r["id"], r["text"]) for r in conn.execute(
            "SELECT id, text FROM question_pool").fetchall()]
    finally:
        conn.close()
    n1 = embedder.ensure_indexed("card", cards)
    n2 = embedder.ensure_indexed("question", questions)
    return {"enabled": True, "cards": n1, "questions": n2}


def _fts_search(conn, scope: str, query: str, limit: int = 10) -> list[str]:
    fts_q = _fts_query(query)
    if not fts_q:
        return []
    if scope == "cards":
        try:
            rows = conn.execute(
                "SELECT c.id FROM kb_cards_fts f JOIN kb_cards c ON c.rowid=f.rowid "
                "WHERE kb_cards_fts MATCH ? AND c.status='active' "
                "ORDER BY bm25(kb_cards_fts) LIMIT ?",
                (fts_q, limit)).fetchall()
        except Exception:
            rows = []
        return [r["id"] for r in rows]
    try:
        rows = conn.execute(
            "SELECT p.id FROM question_fts f JOIN question_pool p ON p.rowid=f.rowid "
            "WHERE question_fts MATCH ? AND p.merged_into IS NULL "
            "ORDER BY bm25(question_fts) LIMIT ?",
            (fts_q, limit)).fetchall()
    except Exception:
        rows = []
    return [r["id"] for r in rows]


def hybrid_search(query: str, scope: str = "cards", top_k: int = 5) -> list[dict]:
    """返回 [{"id", "score", "legs"}]，legs 标注每条结果命中的检索路（可解释性）。"""
    fused: dict[str, dict] = {}

    def add(ids: list[str], leg: str):
        for rank, eid in enumerate(ids):
            entry = fused.setdefault(eid, {"id": eid, "score": 0.0, "legs": []})
            entry["score"] += 1.0 / (RRF_K + rank + 1)
            if leg not in entry["legs"]:
                entry["legs"].append(leg)

    conn = get_conn()
    try:
        add(_fts_search(conn, scope, query), "fts")
    finally:
        conn.close()

    if embedder.available():
        etype = "card" if scope == "cards" else "question"
        add([eid for eid, _ in embedder.search_similar(query, etype, top_k=10)], "vector")

    ranked = sorted(fused.values(), key=lambda x: -x["score"])[:top_k]
    return ranked


def find_similar_question(text: str, threshold: float = 0.88) -> str | None:
    """题目查重：精确 → FTS → 向量语义（余弦阈值）。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM question_pool WHERE text=?", (text,)).fetchone()
        if row:
            return row["id"]
        try:
            row = conn.execute(
                "SELECT p.id FROM question_fts f JOIN question_pool p ON p.rowid=f.rowid "
                "WHERE question_fts MATCH ? ORDER BY bm25(question_fts) LIMIT 1",
                (_fts_query(text),)).fetchone()
            if row:
                return row["id"]
        except Exception:
            pass
    finally:
        conn.close()
    if embedder.available():
        ensure_all_indexed()
        hits = embedder.search_similar(text, "question", top_k=1)
        if hits and hits[0][1] >= threshold:
            return hits[0][0]
    return None


# ============ 深化：查询改写 + LLM rerank + 三档检索（阶段 6） ============

def rewrite_query(query: str) -> list[str]:
    """multi-query 改写：原始 + 2 条变体。失败则降级为原查询。"""
    try:
        from ..core import llm, prompts
        system, user, _ = prompts.render("query_rewrite", query=query)
        msg = llm._chat_json(system, user)
        import json as _json
        extra = [q for q in _json.loads(msg["content"]).get("queries", []) if q and q != query][:2]
        return [query] + extra
    except Exception:
        return [query]


def _candidate_snippet(scope: str, ids: list[str]) -> dict[str, str]:
    conn = get_conn()
    try:
        out = {}
        for eid in ids:
            if scope == "cards":
                r = conn.execute("SELECT title, question, points_json FROM kb_cards WHERE id=?", (eid,)).fetchone()
                if r:
                    out[eid] = f"{r['title']}｜{r['question'] or ''}｜{r['points_json'][:160]}"
            else:
                r = conn.execute("SELECT text, topic FROM question_pool WHERE id=?", (eid,)).fetchone()
                if r:
                    out[eid] = f"{r['text']}｜{r['topic'] or ''}"
        return out
    finally:
        conn.close()


def llm_rerank(query: str, hits: list[dict], scope: str, top_k: int) -> list[dict]:
    """LLM listwise 精排；失败降级为原顺序（可观测但不可用时不影响主链路）。"""
    if len(hits) <= 1:
        return hits[:top_k]
    try:
        from ..core import llm, prompts
        snippets = _candidate_snippet(scope, [h["id"] for h in hits])
        cand_text = "\n".join(f"- id={h['id']}：{snippets.get(h['id'], '')}" for h in hits)
        system, user, _ = prompts.render("rerank", query=query, candidates=cand_text, top_k=top_k)
        msg = llm._chat_json(system, user)
        import json as _json
        ranking = _json.loads(msg["content"]).get("ranking", [])
        by_id = {h["id"]: h for h in hits}
        ordered = [by_id[i] | {"reranked": True} for i in ranking if i in by_id]
        if not ordered:
            return hits[:top_k]
        rest = [h for h in hits if h["id"] not in ranking]
        return (ordered + rest)[:top_k]
    except Exception:
        return hits[:top_k]


def search(query: str, scope: str = "cards", top_k: int = 5, mode: str = "hybrid") -> list[dict]:
    """三档检索：fts（基线）/ hybrid（FTS+向量 RRF）/ full（+改写+rerank）。"""
    if mode == "fts":
        conn = get_conn()
        try:
            ids = _fts_search(conn, scope, query, limit=top_k)
        finally:
            conn.close()
        return [{"id": i, "score": 1.0 - r * 0.01, "legs": ["fts"]} for r, i in enumerate(ids)]

    if mode == "hybrid":
        return hybrid_search(query, scope=scope, top_k=top_k)

    # full：多查询召回 + 融合 + 精排
    queries = rewrite_query(query)
    fused: dict[str, dict] = {}
    for rank_q, q in enumerate(queries):
        for rank, hit in enumerate(hybrid_search(q, scope=scope, top_k=10)):
            entry = fused.setdefault(hit["id"], {"id": hit["id"], "score": 0.0, "legs": set()})
            entry["score"] += 1.0 / (RRF_K + rank + 1) / (1 + rank_q * 0.5)
            entry["legs"] |= set(hit["legs"])
    merged = sorted(fused.values(), key=lambda x: -x["score"])[:10]
    for m in merged:
        m["legs"] = sorted(m["legs"])
    return llm_rerank(query, merged, scope, top_k)
