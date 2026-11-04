from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ..core import config
from ..context import assembler, summarizer
from ..services import console_service

router = APIRouter(prefix="/api/console", tags=["console"])


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    mode: str = "auto"          # auto / assist


@router.post("/chat")
def chat(req: ChatRequest, background: BackgroundTasks):
    """一轮对话：Agent 自己决定调哪些工具，写完汇报。写权限由 mode 决定。"""
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，对话控制台不可用（见 .env）")
    try:
        res = console_service.chat(req.message, req.session_id, req.mode)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # 摘要压缩放后台：它要走一次 LLM，让用户为一件看不见的事多等 2 秒不值得。
    # 拷贝失败不影响本轮结果，下一轮还会再试（covered_to_id 保证不重复压）。
    background.add_task(summarizer.summarize, res["session_id"])
    return res


@router.get("/history")
def history(session_id: str):
    return {"session_id": session_id, "messages": console_service.history(session_id)}


@router.get("/sessions")
def sessions(limit: int = 20):
    return console_service.sessions(limit)


@router.get("/ctx/{session_id}")
def ctx_traces(session_id: str, limit: int = 20):
    """上下文账本：每轮组装的 token 分布、降级记录。"""
    return {"session_id": session_id,
            "traces": assembler.recent_traces(session_id, limit),
            "summary": summarizer.stats(session_id)}
