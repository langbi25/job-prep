from fastapi import APIRouter, HTTPException

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
            if kb_service.link_question_card(qid, card["id"]):
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
    return card
