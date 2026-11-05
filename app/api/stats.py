from fastapi import APIRouter, HTTPException

from ..core.db import get_conn

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
def stats():
    conn = get_conn()
    try:
        cards_by_domain = {
            r["domain"]: r["n"]
            for r in conn.execute("SELECT domain, COUNT(*) AS n FROM kb_cards WHERE status='active' GROUP BY domain")
        }
        ivs_by_direction = {
            r["direction"]: r["n"]
            for r in conn.execute("SELECT direction, COUNT(*) AS n FROM feed_interviews GROUP BY direction")
        }
        top_questions = [
            dict(r)
            for r in conn.execute(
                "SELECT id, text, qtype, topic, freq_count FROM question_pool "
                "WHERE merged_into IS NULL ORDER BY freq_count DESC, last_seen_at DESC LIMIT 10"
            )
        ]
        pending = conn.execute("SELECT COUNT(*) AS n FROM ingests WHERE status IN ('pending','extracted')").fetchone()["n"]
        return {
            "cards_by_domain": cards_by_domain,
            "interviews_by_direction": ivs_by_direction,
            "top_questions": top_questions,
            "pending_ingests": pending,
        }
    finally:
        conn.close()


@router.get("/radar")
def radar():
    """能力雷达（阶段 5）：各领域主题掌握度 + 模拟面试趋势。"""
    conn = get_conn()
    try:
        topics = {}
        for r in conn.execute(
            "SELECT domain, topic, AVG(mastery) AS m, COUNT(*) AS n FROM kb_cards "
            "WHERE status='active' AND review_count>0 GROUP BY domain, topic"
        ):
            topics.setdefault(r["domain"], []).append(
                {"topic": r["topic"], "mastery": round(r["m"], 2), "cards": r["n"]})
        all_cards = conn.execute(
            "SELECT domain, AVG(mastery) AS m, COUNT(*) AS n FROM kb_cards "
            "WHERE status='active' GROUP BY domain").fetchall()
        trend = []
        for r in conn.execute(
            "SELECT started_at, overall_json FROM mock_sessions "
            "WHERE finished_at IS NOT NULL ORDER BY started_at"
        ):
            if r["overall_json"]:
                import json
                o = json.loads(r["overall_json"])
                trend.append({"date": r["started_at"][:10], "avg": o.get("overall_avg")})
        return {
            "by_domain": {r["domain"]: {"avg": round(r["m"], 2), "cards": r["n"]} for r in all_cards},
            "topics": topics,
            "mock_trend": trend,
        }
    finally:
        conn.close()


@router.get("/questions")
def list_questions(q: str | None = None, qtype: str | None = None, topic: str | None = None,
                   limit: int = 200, with_coverage: bool = False):
    """题目池列表。with_coverage=True 时附带"是否有知识卡覆盖"（逐题语义匹配，约 1ms/题）。"""
    import json

    conn = get_conn()
    try:
        if q:
            from ..services.rag_service import search_questions

            rows = [dict(r) for r in search_questions(q, limit=limit)]
            if qtype:
                rows = [r for r in rows if r["qtype"] == qtype]
            if topic:
                rows = [r for r in rows if r["topic"] == topic]
        else:
            sql = ("SELECT * FROM question_pool p WHERE p.merged_into IS NULL")
            args: list = []
            if qtype:
                sql += " AND p.qtype=?"
                args.append(qtype)
            if topic:
                sql += " AND p.topic=?"
                args.append(topic)
            sql += " ORDER BY p.freq_count DESC, p.last_seen_at DESC LIMIT ?"
            args.append(limit)
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        occ = {r["question_id"]: r["n"] for r in conn.execute(
            "SELECT question_id, COUNT(*) AS n FROM question_occurrences GROUP BY question_id")}
        out = [r | {"aliases": json.loads(r["aliases_json"]), "interview_count": occ.get(r["id"], 0)}
               for r in rows]
        if with_coverage:
            from ..services import coverage_service

            for it in out:
                card = coverage_service._match_card(conn, it["text"], it["topic"], it["id"])
                it["card"] = ({"id": card["id"], "title": card["title"], "mastery": card["mastery"],
                               "similarity": card["similarity"]} if card else None)
        return out
    except Exception:
        raise HTTPException(400, "查询失败")
    finally:
        conn.close()


@router.get("/questions/{qid}")
def question_detail(qid: str):
    """题目详情（分层视图用）：题面 + 别名 + 关联卡（含匹配方式）+ 面经出处。

    L3 → L2 的向上指针来自 card_question_links；json 列只做兜底（legacy）。
    面经出处走 question_occurrences —— 「这道题在哪几家面经里出现过」是频次的证据链。
    """
    import json

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM question_pool WHERE id=?", (qid,)).fetchone()
        if not row:
            raise HTTPException(404, "题目不存在")
        d = dict(row)
        d["aliases"] = json.loads(d["aliases_json"] or "[]")
        links, seen = [], set()
        for r in conn.execute(
                "SELECT l.card_id, l.match_method, k.title, k.status, k.mastery "
                "FROM card_question_links l JOIN kb_cards k ON k.id = l.card_id "
                "WHERE l.question_id = ?", (qid,)):
            links.append(dict(r))
            seen.add(r["card_id"])
        for cid in json.loads(d.get("related_card_ids_json") or "[]"):
            if cid in seen:
                continue
            k = conn.execute(
                "SELECT id, title, status, mastery FROM kb_cards WHERE id=?", (cid,)).fetchone()
            if k:
                links.append({"card_id": k["id"], "match_method": "legacy",
                              "title": k["title"], "status": k["status"], "mastery": k["mastery"]})
        d["linked_cards"] = links
        d["occurrences"] = [
            dict(r) for r in conn.execute(
                "SELECT o.raw_text, o.ask_order, i.company, i.position, i.round, "
                "i.interview_date, i.direction, i.result, i.id AS interview_id "
                "FROM question_occurrences o JOIN feed_interviews i ON i.id = o.interview_id "
                "WHERE o.question_id = ? ORDER BY i.interview_date DESC", (qid,))
        ]
        return d
    finally:
        conn.close()


@router.post("/questions/{qid}/merge")
def merge_question(qid: str, into: str):
    """把 qid 归并进 into：出现记录转移、频次累加、qid 标记 merged_into。"""
    import json

    conn = get_conn()
    try:
        src = conn.execute("SELECT * FROM question_pool WHERE id=?", (qid,)).fetchone()
        dst = conn.execute("SELECT * FROM question_pool WHERE id=?", (into,)).fetchone()
        if not src or not dst:
            raise HTTPException(404, "题目不存在")
        if src["merged_into"]:
            raise HTTPException(400, "源题目已被归并过")
        conn.execute("UPDATE question_occurrences SET question_id=? WHERE question_id=?", (into, qid))
        aliases = json.loads(dst["aliases_json"]) + [src["text"]] + json.loads(src["aliases_json"])
        conn.execute(
            "UPDATE question_pool SET freq_count=freq_count+?, aliases_json=? WHERE id=?",
            (src["freq_count"], json.dumps(list(dict.fromkeys(aliases)), ensure_ascii=False), into),
        )
        conn.execute("UPDATE question_pool SET merged_into=? WHERE id=?", (into, qid))
        conn.commit()
        return {"merged": qid, "into": into}
    finally:
        conn.close()
