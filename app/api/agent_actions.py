from fastapi import APIRouter, HTTPException

from ..services import multi_agent

router = APIRouter(prefix="/api/agent", tags=["agent"])


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
