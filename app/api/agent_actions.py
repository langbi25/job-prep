from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import multi_agent

router = APIRouter(prefix="/api/agent", tags=["agent"])


class CardDraftReq(BaseModel):
    question_ids: list[str] = Field(default_factory=list)   # 空 = 自动挑高频无卡题
    limit: int = 3
    only_uncarded: bool = True
    # 题目池里有原文答案时优先走"答案规范化"（低风险），默认开启
    low_risk: bool = True


@router.post("/cards/draft")
def draft_cards(req: CardDraftReq):
    """用 LLM 为题目起草知识卡，产出待审批动作（不直接写库）。"""
    try:
        return multi_agent.draft_cards(req.question_ids, limit=max(1, min(req.limit, 10)),
                                       only_uncarded=req.only_uncarded, low_risk=req.low_risk)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class CardFillReq(BaseModel):
    card_ids: list[str] = Field(default_factory=list)   # 空 = 自动挑要点为空的卡
    limit: int = 3


@router.post("/cards/fill")
def fill_cards(req: CardFillReq):
    """为空壳卡（有卡无要点）起草正文，产出待审批 fill_card 动作。"""
    try:
        return multi_agent.draft_card_content(req.card_ids, limit=max(1, min(req.limit, 10)))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/remediation/{session_id}/draft")
def draft(session_id: str):
    """教练 Agent 起草补救动作（不落库，等人审）。"""
    try:
        return multi_agent.draft_remediation(session_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/actions")
def actions(status: str | None = "pending"):
    return multi_agent.list_actions(status)


@router.post("/actions/{action_id}/approve")
def approve(action_id: str):
    try:
        return multi_agent.decide(action_id, approve=True)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/actions/{action_id}/reject")
def reject(action_id: str):
    try:
        return multi_agent.decide(action_id, approve=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
