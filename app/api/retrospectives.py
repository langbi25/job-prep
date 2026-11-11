"""复盘中心 API（P6）：文档导入 → 考点提炼 → 建题 → 建卡（审批）。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import retrospective_service as retro

router = APIRouter(prefix="/api/retro", tags=["retro"])


class IngestReq(BaseModel):
    title: str = ""
    content: str = ""
    source_path: str = ""      # 可选：来自 docs/ 下文件时记录出处


class IngestDocReq(BaseModel):
    path: str                  # 相对项目根，如 docs/复盘-自进化闭环.md


class QuestionsReq(BaseModel):
    per_point: int = Field(1, ge=1, le=3)


class CardsReq(BaseModel):
    limit: int = Field(3, ge=1, le=5)


@router.get("/docs")
def list_docs():
    return {"items": retro.list_docs()}


@router.get("/list")
def list_retros():
    return {"items": retro.list_retrospectives()}


@router.post("/ingest")
def ingest(req: IngestReq):
    try:
        return retro.ingest(req.content, title=req.title,
                            source_path=req.source_path or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/ingest-doc")
def ingest_doc(req: IngestDocReq):
    """从项目 docs/ 导入（前端选文件用）；服务端读文件，路径限项目根内。"""
    try:
        return retro.ingest_from_doc(req.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{rid}")
def get_retro(rid: str):
    try:
        return retro.get_retrospective(rid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{rid}/extract")
def extract(rid: str):
    try:
        return retro.extract_points(rid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{rid}/questions")
def build_questions(rid: str, req: QuestionsReq):
    try:
        return retro.build_questions(rid, per_point=req.per_point)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{rid}/cards")
def build_cards(rid: str, req: CardsReq):
    try:
        return retro.build_cards(rid, limit=req.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
