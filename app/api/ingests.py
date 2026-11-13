import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..models.schemas import ConfirmPayload, IngestCreate
from ..services import ingest_service
from .sse import emit_safe, hub, sse_response

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


@router.post("/{ingest_id}/confirm/stream")
def confirm_stream(ingest_id: str, payload: ConfirmPayload):
    """确认入库 + 起草卡片，全过程走 SSE。

    为什么不是"确认照旧、起草另开一个流"：确认这一步本身也可能失败（已确认、
    题目全被丢弃），用户需要立刻知道；而"入库成功了但起草失败"是另一种结局。
    两件事在同一个流里按序发事件，前端才分得清是"没入库"还是"入库了但没建卡"。

    事件序：
      confirmed  入库结果（interview_id / questions_kept / answers_saved / 起草排队数）
      draft_start 第 i/n 张开始
      draft_end   第 i/n 张结果（ok=false 带 error，不影响后面继续）
      done       汇总；也可能以 skipped 结束（没有待建卡的题）
      error      入库或起草整体失败
    断线续传：响应头 X-Stream-Id，GET /api/ingests/draft-stream/{id}?after=<seq>
    """
    from ..services import multi_agent

    sid, bus = hub.create("draft")
    emit = emit_safe(bus)

    def work() -> None:
        try:
            res = ingest_service.confirm_ingest(ingest_id, payload.model_dump())
            qids = res.pop("draft_question_ids", [])
            queued, skipped = min(len(qids), DRAFT_CAP), max(0, len(qids) - DRAFT_CAP)
            emit({"type": "confirmed", "result": res, "draft_queued": queued,
                  "draft_skipped": skipped})
            if not qids:
                emit({"type": "done", "inserted": 0, "failed": 0,
                      "skipped": "这批题没有需要起草的（未勾选建卡或已有卡）"})
                return
            # 起草串行（每张约 20s）：并发会把 2C2G 的机器和上游限流一起打满
            multi_agent.draft_cards(qids[:DRAFT_CAP], limit=DRAFT_CAP, low_risk=True,
                                    on_event=emit)
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": f"{type(e).__name__}: {e}"[:300]})
        finally:
            bus.close()

    # 起线程而不是 prompt 式生成器：客户端要能立刻拿到响应头与 X-Stream-Id，
    # 否则"确认"这个动作在主线程里卡 60 秒，续传也无从谈起。
    threading.Thread(target=work, daemon=True).start()
    return sse_response(bus, sid)


@router.get("/draft-stream/{stream_id}")
def resume_draft_stream(stream_id: str, after: int = 0):
    """起草进度续传。after=已收到的最后一个 seq。"""
    bus = hub.get(stream_id)
    if bus is None:
        raise HTTPException(404, "这个起草任务不存在或已过期（缓冲保留 10 分钟）")
    return sse_response(bus, stream_id, after=max(0, int(after or 0)))


@router.post("/{ingest_id}/discard")
def discard(ingest_id: str):
    try:
        return ingest_service.discard_ingest(ingest_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
