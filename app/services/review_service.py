"""阶段 4：数据闭环服务。

- 模拟面试结束后自动回写：弱项（均分<4）记入错题本、按均分增减相关卡片的掌握度、
  缺口主题自动建知识卡片（source=mock），让下一场组卷和复习计划自动瞄准薄弱点。
- SRS 复习调度：到期卡片推送、快问快答自测（pass +0.5 / fail -0.5）、按掌握度定下次间隔。
- 每日简报：到期数、薄弱主题、未解决错题、待处理采集。

掌握度规则（设计方案 4.3）：模拟面试答好(均分>=7) +1，答错/答浅(均分<4) -1；
自测通过 +0.5，自测失败 -0.5，上限 5 下限 0。
"""
import datetime
import json

from ..core import config
from ..core.db import get_conn
from . import kb_service


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.date.today().isoformat()


SRS_INTERVALS = [1, 2, 4, 7, 15, 30]  # 按掌握度档位的天数


def _apply_mastery(conn, card_id: str, delta: float, reason: str, ref_type: str, ref_id: str) -> float:
    card = conn.execute("SELECT mastery FROM kb_cards WHERE id=?", (card_id,)).fetchone()
    if not card:
        return 0.0
    new_m = round(min(5.0, max(0.0, card["mastery"] + delta)), 1)
    conn.execute(
        "UPDATE kb_cards SET mastery=?, last_review_at=?, updated_at=? WHERE id=?",
        (new_m, today(), _now(), card_id),
    )
    conn.execute(
        "INSERT INTO mastery_events (card_id,delta,reason,ref_type,ref_id,created_at) VALUES (?,?,?,?,?,?)",
        (card_id, new_m - card["mastery"], reason, ref_type, ref_id, _now()),
    )
    return new_m - card["mastery"]


def _cause_type(scores: dict) -> str:
    if scores["correctness"] < 4:
        return "知识盲区"
    if scores["structure"] < 4:
        return "表达不清"
    return "理解偏差"


def writeback_after_session(session_id: str, records: list[dict], direction: str) -> dict:
    """模拟面试结束后的自动回写。幂等：错题按 (session_id, raw_text) 唯一。

    分三阶段执行，避免 SQLite 嵌套连接锁：
    A) 单连接写错题 + 掌握度调整，提交；
    B) 通过 kb_service 建缺口卡片（自管连接）；
    C) 新连接为新建卡片补掌握度事件。
    """
    conn = get_conn()
    mistakes_created, cards_updated, to_create = [], [], []
    try:
        for r in records:
            if not r.get("scores"):
                continue
            s = r["scores"]
            avg = sum(s.values()) / 4

            # 1) 弱项 -> 错题本
            if avg < 4:
                cur = conn.execute(
                    "SELECT id FROM mistakes WHERE mock_session_id=? AND raw_text=?", (session_id, r["text"])
                ).fetchone()
                if not cur:
                    conn.execute(
                        "INSERT INTO mistakes (question_id,card_id,mock_session_id,raw_text,cause_type,feedback,resolved,created_at) "
                        "VALUES (?,?,?,?,?,?,0,?)",
                        (r.get("qid"), None, session_id, r["text"], _cause_type(s), r.get("feedback") or "", _now()),
                    )
                    mistakes_created.append(r["text"])

            # 2) 掌握度回写（按 topic 匹配卡片）
            delta = 1 if avg >= 7 else (-1 if avg < 4 else 0)
            cards = conn.execute(
                "SELECT id FROM kb_cards WHERE topic=? AND status='active'", (r.get("topic") or "",)
            ).fetchall()
            for c in cards:
                if delta != 0:
                    _apply_mastery(conn, c["id"], delta, f"mock_{('good' if delta > 0 else 'bad')}", "mock_session", session_id)
                    cards_updated.append({"card": c["id"], "delta": delta})

            # 3) 缺口主题且答得差 -> 记录待建卡片
            if avg < 4 and not cards:
                to_create.append(r)
        conn.commit()
    finally:
        conn.close()

    # 阶段 B：建缺口卡片
    cards_created = []
    for r in to_create:
        card = kb_service.create_card({
            "title": r["text"][:40],
            "domain": direction if direction in config.DOMAINS else "cs",
            "topic": r.get("topic") or "通用", "question": r["text"],
            "points": [], "pitfalls": [], "tags": ["模拟面试弱项"],
            "source_type": "mock",
        })
        cards_created.append(card["id"])
        # 阶段 C：为新建卡片补掌握度事件（初值 1：知道有这个坑但没答上来）
        conn = get_conn()
        try:
            _apply_mastery(conn, card["id"], 1, "mock_gap_created", "mock_session", session_id)
            conn.commit()
        finally:
            conn.close()

    return {"mistakes_created": len(mistakes_created), "cards_updated": cards_updated,
            "cards_created": cards_created}


# ============ SRS 复习 ============

def due_cards(limit: int = 20) -> list[dict]:
    """今日到期：srs_due 为空（新卡）或 <= 今天的卡片。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, domain, topic, title, question, mastery, srs_due, review_count FROM kb_cards "
            "WHERE status='active' AND (srs_due IS NULL OR srs_due<=?) "
            "ORDER BY (srs_due IS NULL) DESC, srs_due LIMIT ?",
            (today(), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def review_card(card_id: str, result: str) -> dict:
    """快问快答自测：pass +0.5 / fail -0.5，并按新掌握度定下次到期日。"""
    conn = get_conn()
    try:
        card = conn.execute("SELECT * FROM kb_cards WHERE id=?", (card_id,)).fetchone()
        if not card:
            raise ValueError("卡片不存在")
        delta = 0.5 if result == "pass" else -0.5
        _apply_mastery(conn, card_id, delta, f"srs_{result}", "srs", today())
        conn.execute("UPDATE kb_cards SET review_count=review_count+1 WHERE id=?", (card_id,))

        new_m = conn.execute("SELECT mastery FROM kb_cards WHERE id=?", (card_id,)).fetchone()["mastery"]
        interval = SRS_INTERVALS[min(int(new_m), len(SRS_INTERVALS) - 1)]
        if result == "fail":
            interval = 1
        next_due = (datetime.date.today() + datetime.timedelta(days=interval)).isoformat()
        conn.execute(
            "INSERT INTO srs_reviews (card_id,reviewed_at,result,next_due) VALUES (?,?,?,?)",
            (card_id, _now(), result, next_due),
        )
        conn.execute("UPDATE kb_cards SET srs_due=? WHERE id=?", (next_due, card_id))
        conn.commit()
        return {"card_id": card_id, "mastery": new_m, "next_due": next_due, "interval_days": interval}
    finally:
        conn.close()


def weak_topics(direction: str | None = None, limit: int = 5) -> list[dict]:
    """薄弱主题：被模拟面试打低分的卡片 + 近 14 天错题主题。"""
    conn = get_conn()
    try:
        since = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
        rows = conn.execute(
            "SELECT m.raw_text AS text, m.cause_type, m.raw_text, q.topic FROM mistakes m "
            "LEFT JOIN question_pool q ON q.id=m.question_id WHERE m.resolved=0 AND m.created_at>=? "
            "ORDER BY m.created_at DESC LIMIT ?", (since, limit),
        ).fetchall()
        weak_cards = conn.execute(
            "SELECT id, topic, title, mastery FROM kb_cards "
            "WHERE status='active' AND review_count>0 AND mastery<=2 "
            + ("AND domain=? " if direction else "") + "ORDER BY mastery LIMIT ?",
            ((direction,) if direction else tuple()) + (limit,),
        ).fetchall()
        return {"recent_mistakes": [dict(r) for r in rows], "weak_cards": [dict(r) for r in weak_cards]}
    finally:
        conn.close()


def daily_brief() -> dict:
    due = due_cards(limit=100)
    conn = get_conn()
    try:
        open_mistakes = conn.execute("SELECT COUNT(*) AS n FROM mistakes WHERE resolved=0").fetchone()["n"]
        pending = conn.execute("SELECT COUNT(*) AS n FROM ingests WHERE status IN ('pending','extracted')").fetchone()["n"]
        recent = conn.execute(
            "SELECT overall_json FROM mock_sessions WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 3"
        ).fetchall()
    finally:
        conn.close()
    mock_avgs = []
    for r in recent:
        if r["overall_json"]:
            mock_avgs.append(json.loads(r["overall_json"]).get("overall_avg"))
    return {
        "date": today(),
        "due_cards": len(due),
        "due_preview": due[:5],
        "open_mistakes": open_mistakes,
        "pending_ingests": pending,
        "recent_mock_avgs": mock_avgs,
        "weak": weak_topics(),
    }
