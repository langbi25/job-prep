from fastapi import APIRouter
from pydantic import BaseModel

from ..services import harness

router = APIRouter(prefix="/api/agent", tags=["agent"])


class PlanRequest(BaseModel):
    direction: str = "bigdata"


@router.post("/plan")
def study_plan(payload: PlanRequest):
    """学习教练 Agent：自主调工具查数据 → 产出学习计划，全程 trace 落库。"""
    return harness.run_study_plan_agent(payload.direction)


@router.get("/runs")
def list_runs(limit: int = 10):
    from ..core.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, kind, status, created_at, steps_json FROM runs "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"steps": __import__("json").loads(r["steps_json"])} for r in rows]
    finally:
        conn.close()
