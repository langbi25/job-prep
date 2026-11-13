"""确认入库 → 后台起草卡片 的流式进度测试。

这段过程原本完全不可见：确认接口秒回一句「约 1 分钟后去审批中心看」，
之后是成功、失败、建了几张，用户全靠猜。四个不变量：
1. 入库结果必须先到（confirmed），起草进度在后——否则分不清"没入库"和"入库了没建卡"；
2. 单张失败不能让后面的卡不再起草（draft_end.ok=false 后继续）；
3. 没有待建卡的题时以 skipped 结束，而不是静默空流；
4. 断线可续传，且不重发已收到的。
"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ingests as ingests_api
from app.services import ingest_service, multi_agent


def _events(body: str) -> list[dict]:
    out = []
    for chunk in body.split("\n\n"):
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
                break
    return out


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(ingests_api.router)
    return app


def _confirm_ok(qids: list[str]):
    def fake(ingest_id, payload):
        return {"interview_id": "iv-test", "questions_kept": len(qids),
                "answers_saved": 2, "draft_question_ids": qids}
    return fake


def test_confirmed_then_progress_then_done(monkeypatch):
    """确认 → 逐卡进度 → 汇总，且 confirmed 必须是第一条。"""
    monkeypatch.setattr(ingest_service, "confirm_ingest", _confirm_ok(["q1", "q2"]))

    def fake_draft(question_ids=None, limit=3, only_uncarded=True, low_risk=False,
                   topic=None, on_event=None):
        for i, qid in enumerate(["q1", "q2"], 1):
            on_event({"type": "draft_start", "i": i, "n": 2, "question_id": qid,
                      "question": f"题 {qid}"})
            on_event({"type": "draft_end", "i": i, "n": 2, "ok": True,
                      "question_id": qid, "action_id": f"act-{i}", "title": f"卡 {i}",
                      "points": 4})
        on_event({"type": "done", "inserted": 2, "failed": 0,
                  "actions": ["act-1", "act-2"]})
        return {"drafted": [], "failed": [], "inserted": 2}

    monkeypatch.setattr(multi_agent, "draft_cards", fake_draft)

    with TestClient(_app()).stream(
            "POST", "/api/ingests/ing-1/confirm/stream",
            json={"questions": [{"text": "题 q1", "keep": True}]}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        evs = _events(b"".join(resp.iter_bytes()).decode("utf-8"))

    kinds = [e["type"] for e in evs]
    assert kinds[0] == "confirmed"
    assert evs[0]["draft_queued"] == 2
    assert evs[0]["result"]["interview_id"] == "iv-test"
    assert kinds == ["confirmed", "draft_start", "draft_end",
                     "draft_start", "draft_end", "done"]
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))


def test_single_card_failure_does_not_stop_the_rest(monkeypatch):
    """第 1 张失败，第 2 张必须照常起草——批量里一张坏掉拖垮全部最伤。"""
    monkeypatch.setattr(ingest_service, "confirm_ingest", _confirm_ok(["q1", "q2"]))

    def fake_draft(question_ids=None, limit=3, only_uncarded=True, low_risk=False,
                   topic=None, on_event=None):
        on_event({"type": "draft_start", "i": 1, "n": 2, "question_id": "q1"})
        on_event({"type": "draft_end", "i": 1, "n": 2, "ok": False,
                  "question_id": "q1", "error": "要点不足（1 条）"})
        on_event({"type": "draft_start", "i": 2, "n": 2, "question_id": "q2"})
        on_event({"type": "draft_end", "i": 2, "n": 2, "ok": True,
                  "question_id": "q2", "action_id": "act-2", "title": "卡 2"})
        on_event({"type": "done", "inserted": 1, "failed": 1, "actions": ["act-2"]})
        return {"drafted": [], "failed": [{"question_id": "q1"}], "inserted": 1}

    monkeypatch.setattr(multi_agent, "draft_cards", fake_draft)

    with TestClient(_app()).stream(
            "POST", "/api/ingests/ing-1/confirm/stream", json={"questions": []}) as resp:
        evs = _events(b"".join(resp.iter_bytes()).decode("utf-8"))

    ends = [e for e in evs if e["type"] == "draft_end"]
    assert [e["ok"] for e in ends] == [False, True]
    assert evs[-1]["inserted"] == 1 and evs[-1]["failed"] == 1


def test_no_draft_queue_ends_with_skipped(monkeypatch):
    """没勾建卡：不能静默结束，要明确说"这批不需要起草"。"""
    monkeypatch.setattr(ingest_service, "confirm_ingest", _confirm_ok([]))

    with TestClient(_app()).stream(
            "POST", "/api/ingests/ing-1/confirm/stream", json={"questions": []}) as resp:
        evs = _events(b"".join(resp.iter_bytes()).decode("utf-8"))

    assert [e["type"] for e in evs] == ["confirmed", "done"]
    assert evs[-1].get("skipped")


def test_confirm_failure_reaches_client_as_error_event(monkeypatch):
    """入库本身失败（如重复确认）要发 error 事件，而不是 500 断流。"""
    def boom(ingest_id, payload):
        raise ValueError("该面经已确认")
    monkeypatch.setattr(ingest_service, "confirm_ingest", boom)

    with TestClient(_app()).stream(
            "POST", "/api/ingests/ing-1/confirm/stream", json={"questions": []}) as resp:
        assert resp.status_code == 200
        evs = _events(b"".join(resp.iter_bytes()).decode("utf-8"))

    assert evs[-1]["type"] == "error"
    assert "已确认" in evs[-1]["message"]


def test_resume_replays_after_breakpoint(monkeypatch):
    monkeypatch.setattr(ingest_service, "confirm_ingest", _confirm_ok(["q1"]))

    def fake_draft(question_ids=None, limit=3, only_uncarded=True, low_risk=False,
                   topic=None, on_event=None):
        on_event({"type": "draft_start", "i": 1, "n": 1, "question_id": "q1"})
        on_event({"type": "draft_end", "i": 1, "n": 1, "ok": True, "question_id": "q1"})
        on_event({"type": "done", "inserted": 1, "failed": 0})
        return {"drafted": [], "failed": [], "inserted": 1}

    monkeypatch.setattr(multi_agent, "draft_cards", fake_draft)
    c = TestClient(_app())

    with c.stream("POST", "/api/ingests/ing-1/confirm/stream",
                  json={"questions": []}) as resp:
        sid = resp.headers["x-stream-id"]
        assert sid
        full = _events(b"".join(resp.iter_bytes()).decode("utf-8"))

    with c.stream("GET", f"/api/ingests/draft-stream/{sid}?after=2") as resp2:
        assert resp2.status_code == 200
        rest = _events(b"".join(resp2.iter_bytes()).decode("utf-8"))

    assert rest[0]["seq"] == 3, "续传从断点之后开始"
    assert rest[-1]["type"] == "done"
    assert [e["seq"] for e in full] == list(range(1, len(full) + 1))


def test_resume_unknown_stream_returns_404():
    r = TestClient(_app()).get("/api/ingests/draft-stream/draft-doesnotexist")
    assert r.status_code == 404
    assert "过期" in r.json()["detail"]
