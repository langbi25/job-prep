"""飞轮 API（阶段 A）：作战地图 + 进化报告 + 飞轮指标 + 真实结果回录。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import coverage_service

router = APIRouter(prefix="/api", tags=["flywheel"])


@router.get("/interviews")
def list_interviews(limit: int = 50):
    """面经列表（作战地图入口）。含是否已有作战地图。"""
    from ..core.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT i.id, i.company, i.position, i.direction, i.round, i.result, i.interview_date, "
            "i.created_at, i.question_count, "
            "(SELECT COUNT(*) FROM question_occurrences o WHERE o.interview_id=i.id) AS n_questions, "
            "c.generated_at AS analyzed_at "
            "FROM feed_interviews i LEFT JOIN coverage_reports c ON c.interview_id=i.id "
            "ORDER BY i.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            dict(r) | {"has_coverage": r["analyzed_at"] is not None} for r in rows
        ]
    finally:
        conn.close()


@router.post("/coverage/{interview_id}/analyze")
def analyze(interview_id: str):
    """生成/刷新作战地图：逐题覆盖预测 + 缺口 + 建议动作。"""
    try:
        return coverage_service.analyze_interview(interview_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/coverage/{interview_id}")
def get_coverage(interview_id: str):
    """读取已保存的作战地图。"""
    import json

    from ..core.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM coverage_reports WHERE interview_id=?", (interview_id,)).fetchone()
        if not row:
            raise HTTPException(404, "尚未生成作战地图")
        payload = json.loads(row["payload_json"])
        payload["analyzed_at"] = row["generated_at"]
        return payload
    finally:
        conn.close()


@router.get("/evolution/report")
def evolution_report():
    """进化报告：覆盖全景 + 高频无卡题 + 薄弱卡 + 建议队列。"""
    return coverage_service.evolution_report()


@router.get("/flywheel/metrics")
def flywheel_metrics():
    """飞轮四指标（Dashboard 用）。"""
    return coverage_service.flywheel_metrics()


class OutcomeItem(BaseModel):
    text: str = Field(min_length=2)
    predicted_band: str | None = None
    self_band: str | None = None


class OutcomePayload(BaseModel):
    company: str = Field(min_length=1)
    occurred_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    result: str = Field(pattern=r"^(pass|fail|unknown)$")
    notes: str | None = None
    questions: list[OutcomeItem] = Field(default_factory=list)


@router.post("/outcomes")
def record_outcome(payload: OutcomePayload):
    """回录真实面试结果（校准环）。"""
    return coverage_service.record_outcome(payload.model_dump())


@router.get("/outcomes")
def list_outcomes(limit: int = 20):
    return coverage_service.list_outcomes(limit=limit)
