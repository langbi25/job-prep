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
    """懒索引：为所有卡片和题目建立/更新向量，并清掉已失效实体的残留向量。

    为什么必须清理：embeddings 表是独立索引，不随 kb_cards.status 变化。
    卡片被归档/合并后如果向量还在，hybrid 检索的向量路会继续把它捞出来
    （FTS 路有 status 过滤，向量路没有），表现为"明明归档了却还在推荐"。
    """
    if not embedder.available():
        return {"enabled": False, "cards": 0, "questions": 0}
    conn = get_conn()
    try:
        cards = [(r["id"], _card_text(r)) for r in conn.execute(
            "SELECT * FROM kb_cards WHERE status='active'").fetchall()]
        questions = [(r["id"], r["text"]) for r in conn.execute(
            "SELECT id, text FROM question_pool").fetchall()]
        # 清理残留：归档卡 / 已删除的题目
        stale = conn.execute(
            "SELECT entity_type, entity_id FROM embeddings").fetchall()
        valid = {"card": {c[0] for c in cards}, "question": {q[0] for q in questions}}
        removed = 0
        for row in stale:
            if row["entity_id"] not in valid.get(row["entity_type"], set()):
                conn.execute("DELETE FROM embeddings WHERE entity_type=? AND entity_id=?",
                             (row["entity_type"], row["entity_id"]))
                removed += 1
        conn.commit()
        # 规模防护：numpy 暴力余弦在几千条时 <10ms，过万后每次查询都全量扫描，
        # 延迟和内存都会线性恶化。这里只告警不阻塞——换 Chroma/pgvector 的
        # 换手点在 embedder.search_similar，接口不变。
        total = len(cards) + len(questions)
        if total > 8000:
            print(f"[warn] 向量实体数 {total} 已超暴力扫描舒适区（<8000），"
                  "建议迁移到 Chroma/pgvector：embedder.search_similar 是预留的换手点")
    finally:
        conn.close()
    n1 = embedder.ensure_indexed("card", cards)
    n2 = embedder.ensure_indexed("question", questions)
    return {"enabled": True, "cards": n1, "questions": n2, "stale_removed": removed}


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


def _norm_text(s: str) -> str:
    """归一化：去标点空白、转小写，只留中英文数字，用于判断"是否同一道题"。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "").lower()


def _same_question(a: str, b: str, min_ratio: float = 0.9) -> bool:
    """字面相似度判定（dedupe 用，比向量可信）。

    不能靠 FTS 判定：unicode61 把整段中文当作一个 token，2-gram 前缀只能命中
    "词首"，句子中间的片段一条都召回不了。所以候选召回改为 3-gram 重合度预筛。
    """
    import difflib

    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= min_ratio


def _grams(s: str, n: int = 3) -> set[str]:
    t = _norm_text(s)
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _text_candidates(conn, text: str, limit: int = 5) -> list:
    """按 3-gram 重合度从题目池里召回字面候选（不依赖 FTS）。"""
    rows = conn.execute(
        "SELECT id, text FROM question_pool WHERE merged_into IS NULL LIMIT 5000").fetchall()
    g = _grams(text)
    if not g:
        return []
    scored = []
    for r in rows:
        gr = _grams(r["text"])
        if not gr:
            continue
        overlap = len(g & gr) / min(len(g), len(gr))
        if overlap >= 0.3:
            scored.append((overlap, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def search_questions(q: str, limit: int = 200) -> list:
    """题目子串搜索（中文友好）。多个词按"命中词数"排序，而不是 AND 全部命中。

    不用 question_fts：中文分词器缺位导致句中出现的关键词无法命中，
    万级以内 LIKE 全表扫描是毫秒级，正确性优先。
    """
    toks = [t for t in re.split(r"\s+", (q or "").strip()) if t]
    if not toks:
        return []
    where = " OR ".join(["(text LIKE ? OR aliases_json LIKE ?)"] * len(toks))
    args: list = []
    for t in toks:
        args += [f"%{t}%", f"%{t}%"]
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM question_pool WHERE merged_into IS NULL AND (" + where + ") LIMIT 3000",
            args).fetchall()
        # IDF 加权：像 "Agent" 这种几乎每道题都有的词不该主导排序
        hays = {(r["text"] or "") + " " + (r["aliases_json"] or ""): r for r in rows}
        import math

        df = {t: sum(1 for h in hays if t in h) for t in toks}
        weight = {t: 1.0 / (1.0 + math.log(max(1, df[t]))) for t in toks}
        scored = []
        for hay, r in hays.items():
            hit = sum(1 for t in toks if t in hay)
            if not hit:
                continue
            score = sum(weight[t] for t in toks if t in hay)
            scored.append((score, hit, r["freq_count"], dict(r)))
        scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        return [x[3] for x in scored[:limit]]
    finally:
        conn.close()


def find_similar_question(text: str, threshold: float = 0.95, ensure_index: bool = False) -> str | None:
    """题目查重：精确 → 3-gram 候选 + 字面裁决 → 向量语义（高阈值）。

    ensure_index 默认 False：调用方可能正持有写事务（如 confirm_ingest），
    此时重建索引会开第二个连接去写 embeddings，直接 SQLITE_BUSY 死锁。
    建索引用启动时的 ensure_all_indexed() 或显式传 True 的只读调用方。
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM question_pool WHERE text=?", (text,)).fetchone()
        if row:
            return row["id"]
        for c in _text_candidates(conn, text):
            if _same_question(text, c["text"]):
                return c["id"]
    finally:
        conn.close()
    # 换一种问法的同一题交给向量，但阈值必须高（bge-small 同领域长句余弦普遍 0.85+）
    if embedder.available():
        if ensure_index:
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
