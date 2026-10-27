from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import review_service

router = APIRouter(prefix="/api", tags=["review"])


class ReviewPayload(BaseModel):
    card_id: str
    result: str  # pass / fail


@router.get("/srs/due")
def srs_due(limit: int = 20):
    return review_service.due_cards(limit=limit)


@router.post("/srs/review")
def srs_review(payload: ReviewPayload):
    if payload.result not in ("pass", "fail"):
        raise HTTPException(400, "result 只能是 pass / fail")
    try:
        return review_service.review_card(payload.card_id, payload.result)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/mistakes")
def list_mistakes(only_open: bool = True, limit: int = 50):
    from ..core.db import get_conn

    conn = get_conn()
    try:
        sql = ("SELECT m.*, q.topic, q.freq_count FROM mistakes m "
               "LEFT JOIN question_pool q ON q.id=m.question_id")
        if only_open:
            sql += " WHERE m.resolved=0"
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/mistakes/{mistake_id}/resolve")
def resolve_mistake(mistake_id: str):
    from ..core.db import get_conn

    conn = get_conn()
    try:
        cur = conn.execute("UPDATE mistakes SET resolved=1 WHERE id=?", (int(mistake_id.replace("m-", "")) if mistake_id.startswith("m-") else int(mistake_id),))
        if cur.rowcount == 0:
            raise HTTPException(404, "错题不存在")
        conn.commit()
        return {"resolved": mistake_id}
    finally:
        conn.close()


@router.get("/daily")
def daily():
    return review_service.daily_brief()
