"""可观测中心 API：中文面板的只读代理。

数据源可切换（OBS_BACKEND=phoenix|langfuse）：
- phoenix：读 Phoenix GraphQL（默认，单机 pip 起，SQLite 存储）
- langfuse：读 Langfuse v2 observations（需 docker compose 自托管）

两个实现对外签名一致（overview/list_traces/trace_detail/list_sessions/session_detail），
所以这一层只做分派，不做任何数据加工。后端不可用时不抛 500，
而是返回 available=False + 原因，前端据此展示降级提示。
"""
import os

from fastapi import APIRouter

from ..services import langfuse_service, obs_service

router = APIRouter(prefix="/api/obs", tags=["observability"])

_ERRORS = (obs_service.PhoenixUnavailable, langfuse_service.LangfuseUnavailable)


def backend_name() -> str:
    return (os.environ.get("OBS_BACKEND") or "phoenix").strip().lower()


def _svc():
    return langfuse_service if backend_name() == "langfuse" else obs_service


def _fallback(e: Exception) -> dict:
    svc = _svc()
    return {
        "available": False,
        "backend": backend_name(),
        "reason": str(e),
        "phoenix_url": getattr(svc, "PHOENIX_URL", None) or getattr(svc, "LANGFUSE_URL", ""),
    }


@router.get("/backend")
def backend_info():
    """面板用来显示当前数据源（以及原生界面跳转地址）。"""
    name = backend_name()
    if name == "langfuse":
        return {"backend": name, "url": langfuse_service.LANGFUSE_URL,
                "reachable": langfuse_service.available()}
    return {"backend": name, "url": obs_service.PHOENIX_URL, "reachable": True}


@router.get("/overview")
def overview(scope: str = "agent"):
    try:
        return _svc().overview(scope)
    except _ERRORS as e:
        return _fallback(e)


@router.get("/traces")
def list_traces(limit: int = 50, scope: str = "agent"):
    try:
        return _svc().list_traces(limit, scope)
    except _ERRORS as e:
        return _fallback(e)


@router.get("/sessions")
def list_sessions(limit: int = 20):
    try:
        return _svc().list_sessions(limit)
    except _ERRORS as e:
        return _fallback(e)


@router.get("/sessions/{session_id}")
def session_detail(session_id: str):
    try:
        return _svc().session_detail(session_id)
    except _ERRORS as e:
        return _fallback(e)


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str):
    try:
        return _svc().trace_detail(trace_id)
    except _ERRORS as e:
        return _fallback(e)
