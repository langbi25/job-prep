from fastapi import APIRouter, HTTPException

from ..core.db import get_conn
from ..models.schemas import CardCreate
from ..services import kb_service

router = APIRouter(prefix="/api/cards", tags=["cards"])


@router.post("")
def create_card(payload: CardCreate):
    data = payload.model_dump()
    qid = data.pop("question_id", None)
    card = kb_service.create_card(data)
    if qid:
        try:
            if kb_service.link_question_card(qid, card["id"], match_method="manual"):
                card["linked_question"] = qid
        except Exception as e:  # noqa: BLE001
            card["link_question_error"] = str(e)[:120]
    return card


@router.get("")
def list_cards(domain: str | None = None, topic: str | None = None, q: str | None = None, limit: int = 50):
    return kb_service.list_cards(domain=domain, topic=topic, q=q, limit=limit)


@router.get("/{card_id}")
def get_card(card_id: str):
    card = kb_service.get_card(card_id)
    if not card:
        raise HTTPException(404, "卡片不存在")
    # 分层视图的卡详情：带上「这张卡覆盖了哪些题」——L2 → L3 的向下指针
    conn = get_conn()
    try:
        card["linked_questions"] = [
            dict(r) for r in conn.execute(
                "SELECT l.question_id, l.match_method, q.text, q.freq_count "
                "FROM card_question_links l JOIN question_pool q ON q.id = l.question_id "
                "WHERE l.card_id = ? ORDER BY q.freq_count DESC", (card_id,))
        ]
    finally:
        conn.close()
    return card
