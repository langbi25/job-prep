from fastapi import APIRouter, HTTPException

from ..models.schemas import ConfirmPayload, IngestCreate
from ..services import ingest_service

router = APIRouter(prefix="/api/ingests", tags=["ingests"])


@router.post("")
def create_ingest(payload: IngestCreate):
    """投喂通道 A（paste）/ B（url）/ C（bookmarklet：url+selection）。"""
    try:
        return ingest_service.create_ingest(
            payload.source_type, text=payload.text, url=payload.url, selection=payload.selection
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"抓取失败：{e}")


@router.get("")
def list_ingests(status: str | None = None):
    return ingest_service.list_ingests(status=status)


@router.post("/{ingest_id}/extract")
def extract(ingest_id: str):
    try:
        return ingest_service.run_extract(ingest_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/{ingest_id}")
def get_ingest(ingest_id: str):
    rows = ingest_service.list_ingests()
    for r in rows:
        if r["id"] == ingest_id:
            return r
    raise HTTPException(404, "采集记录不存在")


@router.post("/{ingest_id}/confirm")
def confirm(ingest_id: str, payload: ConfirmPayload):
    try:
        return ingest_service.confirm_ingest(ingest_id, payload.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{ingest_id}/discard")
def discard(ingest_id: str):
    try:
        return ingest_service.discard_ingest(ingest_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
