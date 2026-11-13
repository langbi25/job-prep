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


@router.get("/alerts")
def alert_history(days: int = 1, limit: int = 50):
    """告警历史：触发记录 + 当前阈值配置 + 送达失败数。

    送达失败率本身是最重要的一列——webhook 断了以后告警会静默失败，
    靠这个数字发现"告警系统失联"。
    """
    from fastapi import HTTPException
    from ..observability import alerts

    if not (1 <= days <= 30):
        raise HTTPException(400, "days 限 1~30")
    return alerts.history(days=days, limit=limit)


@router.post("/alerts/test")
def alert_test():
    """手动发一条测试告警：配置完 webhook 后验收链路，不必等真故障。"""
    from ..observability import alerts

    return alerts.send_test()


@router.get("/eval-intake")
def eval_intake_stats():
    """评测素材池现状：失败轮自动进料了多少、按来源分布、最近 5 条。"""
    from ..services import eval_intake

    return eval_intake.stats()


@router.get("/audit")
def audit_log(days: int = 7, limit: int = 100):
    """审计日志：写操作与被拒请求。数据源是本地 audit_log 表，与外部观测后端无关。"""
    import datetime

    from fastapi import HTTPException
    from ..core.db import get_conn

    if not (1 <= days <= 90):
        raise HTTPException(400, "days 限 1~90")
    if not (1 <= limit <= 500):
        raise HTTPException(400, "limit 限 1~500")
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, actor, action, path, query, ip, status, duration_ms "
        "FROM audit_log WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
    total = conn.execute("SELECT count(*) FROM audit_log WHERE ts >= ?", (since,)).fetchone()[0]
    denied = conn.execute(
        "SELECT count(*) FROM audit_log WHERE ts >= ? AND status = 401", (since,)).fetchone()[0]
    return {"available": True, "total": total, "denied_401": denied,
            "items": [dict(zip(("ts", "actor", "action", "path", "query", "ip", "status", "duration_ms"), r))
                      for r in rows]}
