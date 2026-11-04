from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import config
from ..services import console_service

router = APIRouter(prefix="/api/console", tags=["console"])


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    mode: str = "auto"          # auto / assist


@router.post("/chat")
def chat(req: ChatRequest):
    """一轮对话：Agent 自己决定调哪些工具，写完汇报。写权限由 mode 决定。"""
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，对话控制台不可用（见 .env）")
    try:
        return console_service.chat(req.message, req.session_id, req.mode)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/history")
def history(session_id: str):
    return {"session_id": session_id, "messages": console_service.history(session_id)}


@router.get("/sessions")
def sessions(limit: int = 20):
    return console_service.sessions(limit)
