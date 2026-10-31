from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..models.schemas import ConfirmPayload, IngestCreate
from ..services import ingest_service

router = APIRouter(prefix="/api/ingests", tags=["ingests"])

# 一次确认最多自动起草几张卡。上限存在的理由不是省钱，是 2C2G 单机：
# 每张卡一次 LLM 调用约 20s，20 道题全建卡会让确认接口（以及后面的复习队列）卡死。
DRAFT_CAP = 3


def _draft_cards_bg(question_ids: list[str]) -> None:
    """确认后异步起草卡片。失败只打日志，不回滚入库——面经已经进池了，卡可以稍后补。"""
    from ..services import multi_agent
    try:
        multi_agent.draft_cards(question_ids[:DRAFT_CAP], limit=DRAFT_CAP, low_risk=True)
    except Exception as e:  # noqa: BLE001
        print(f"[draft_cards] 后台起草失败：{e}")


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
    try:
        return ingest_service.ingest_detail(ingest_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/{ingest_id}/confirm")
def confirm(ingest_id: str, payload: ConfirmPayload, bg: BackgroundTasks):
    try:
        res = ingest_service.confirm_ingest(ingest_id, payload.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 建卡放后台：确认接口必须秒回，卡片草稿进审批中心后再由人逐条过/批量过
    qids = res.pop("draft_question_ids", [])
    if qids:
        bg.add_task(_draft_cards_bg, qids)
    res["draft_queued"] = min(len(qids), DRAFT_CAP)
    res["draft_skipped"] = max(0, len(qids) - DRAFT_CAP)
    return res


@router.post("/{ingest_id}/discard")
def discard(ingest_id: str):
    try:
        return ingest_service.discard_ingest(ingest_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
