"""可观测中心 API：中文面板的只读代理（Phoenix → /api/obs/*）。

Phoenix 不可用时不抛 500，而是返回 available=False + 原因，前端据此展示降级提示。
"""
from fastapi import APIRouter

from ..services import obs_service

router = APIRouter(prefix="/api/obs", tags=["observability"])


@router.get("/overview")
def overview(scope: str = "agent"):
    try:
        return obs_service.overview(scope)
    except obs_service.PhoenixUnavailable as e:
        return {"available": False, "reason": str(e), "phoenix_url": obs_service.PHOENIX_URL}


@router.get("/traces")
def list_traces(limit: int = 50, scope: str = "agent"):
    try:
        return obs_service.list_traces(limit, scope)
    except obs_service.PhoenixUnavailable as e:
        return {"available": False, "reason": str(e), "phoenix_url": obs_service.PHOENIX_URL}


@router.get("/sessions")
def list_sessions(limit: int = 20):
    try:
        return obs_service.list_sessions(limit)
    except obs_service.PhoenixUnavailable as e:
        return {"available": False, "reason": str(e), "phoenix_url": obs_service.PHOENIX_URL}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str):
    try:
        return obs_service.session_detail(session_id)
    except obs_service.PhoenixUnavailable as e:
        return {"available": False, "reason": str(e), "phoenix_url": obs_service.PHOENIX_URL}


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str):
    try:
        return obs_service.trace_detail(trace_id)
    except obs_service.PhoenixUnavailable as e:
        return {"available": False, "reason": str(e), "phoenix_url": obs_service.PHOENIX_URL}
