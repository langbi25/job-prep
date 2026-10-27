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
def list_questions(q: str | None = None, qtype: str | None = None, topic: str | None = None, limit: int = 50):
    conn = get_conn()
    try:
        sql = "SELECT id, text, aliases_json, qtype, topic, freq_count, last_seen_at FROM question_pool WHERE merged_into IS NULL"
        args: list = []
        if qtype:
            sql += " AND qtype=?"; args.append(qtype)
        if topic:
            sql += " AND topic=?"; args.append(topic)
        if q:
            sql = ("SELECT p.id, p.text, p.aliases_json, p.qtype, p.topic, p.freq_count, p.last_seen_at "
                   "FROM question_fts f JOIN question_pool p ON p.rowid=f.rowid WHERE question_fts MATCH ? AND p.merged_into IS NULL")
            args = [q]
        sql += " ORDER BY freq_count DESC LIMIT ?"; args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) | {"aliases": __import__("json").loads(r["aliases_json"])} for r in rows]
    except Exception:
        raise HTTPException(400, "查询失败")
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
