from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import mock_service

router = APIRouter(prefix="/api/mock", tags=["mock"])


class MockStart(BaseModel):
    direction: str = "bigdata"
    persona: str = ""
    jd_text: str = ""
    question_count: int = Field(default=8, ge=1, le=15)
    company: str = ""       # 阶段 5 冲刺模式：该公司面经出现过的题优先


class MockAnswer(BaseModel):
    index: int
    answer_text: str = ""


@router.post("/sessions")
def start_session(payload: MockStart):
    try:
        return mock_service.compose_session(
            payload.direction, persona=payload.persona,
            jd_text=payload.jd_text, question_count=payload.question_count,
            company=payload.company)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/sessions")
def list_sessions():
    return mock_service.list_sessions()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    s = mock_service.get_session(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    return s


@router.post("/sessions/{session_id}/answer")
def answer(session_id: str, payload: MockAnswer):
    try:
        return mock_service.submit_answer(session_id, payload.index, payload.answer_text)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{session_id}/finish")
def finish(session_id: str):
    try:
        return mock_service.finish_session(session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{session_id}/writeback")
def redo_writeback(session_id: str):
    """手动补跑阶段 4 回写（如自动回写失败时使用），幂等。"""
    import json as _json

    s = mock_service.get_session(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    records = [
        {"qid": q["qid"], "text": q["text"], "topic": q.get("topic", ""),
         "scores": (a or {}).get("scores"), "feedback": (a or {}).get("feedback")}
        for q, a in zip(s["questions"], s["answers"])
    ]
    from ..services import review_service

    return review_service.writeback_after_session(session_id, records, s["direction"])
