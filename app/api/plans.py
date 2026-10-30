"""项目进化环 API：项目档案 + 改造方案 + 实战回灌。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import improvement_service

router = APIRouter(prefix="/api", tags=["plans"])


class ProfilePayload(BaseModel):
    profile: str = ""


class PlanReq(BaseModel):
    question_id: str
    focus: str = ""


class ExperienceReq(BaseModel):
    notes: str = Field(min_length=1)


@router.get("/profile")
def get_profile():
    return improvement_service.get_profile()


@router.put("/profile")
def put_profile(payload: ProfilePayload):
    return improvement_service.save_profile(payload.profile)


@router.post("/plans/draft")
def draft_plan(req: PlanReq):
    """为一道题生成项目改造方案（LLM）。方案正文同时落 docs/plans/*.md。"""
    try:
        return improvement_service.draft_plan(req.question_id, focus=req.focus)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/plans")
def list_plans(status: str | None = None):
    return improvement_service.list_plans(status)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    try:
        return improvement_service.get_plan(plan_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/plans/{plan_id}/adopt")
def adopt_plan(plan_id: str):
    try:
        return improvement_service.adopt_plan(plan_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/plans/{plan_id}/experience")
def draft_experience(plan_id: str, req: ExperienceReq):
    """回灌实施笔记 → 生成实战卡草稿（pending，审批后入库并与原卡互链）。"""
    try:
        return improvement_service.draft_experience(plan_id, req.notes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
