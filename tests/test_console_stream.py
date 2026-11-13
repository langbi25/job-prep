"""对话流式输出测试。

事件协议的三个不变量，各对应一个测试：
1. 事件序：segment → delta → turn_end(has_tools) → tool → turn_end(无工具收尾)；
   "最终回复一定是无工具那一轮"是前端清缓冲逻辑的依据，协议必须保证它成立。
2. 回调抛异常绝不能打断对话（流式是体验增强，不是关键路径）。
3. SSE 端点把事件逐条转成 data: 行，错误以 error 事件到达而不是 5xx 断流。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import console as console_api
from app.core import db
from app.services import console_service


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """这些用例会真的跑一遍 chat()，链路里要落 console_messages / skill_runs / turn_facts。

    不隔离 DB 就会写进开发库：实测在真实库里留下过一条 session_id='s-test'
    的假回合，把回合质量面板的「回合数」分母污染成 2。
    """
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


def _fake_chat_json_seq(seq):
    """按序返回预制 LLM 轮；记录 on_delta 是否被透传。"""
    calls = iter(seq)
    deltas = []

    def fake(system, user, tools=None, messages=None, temperature=0.1,
             json_mode=True, on_delta=None):
        item = next(calls)
        for d in item.pop("deltas") or []:
            deltas.append(d)
            if on_delta:
                on_delta(d)
        return item["msg"]

    return fake, deltas


def test_event_order_and_turn_end_semantics(monkeypatch):
    """带工具的轮：delta 后必须跟 turn_end(has_tools=true)；收尾轮 has_tools=false。"""
    fake, _ = _fake_chat_json_seq([
        {"deltas": ["我先查一下记忆。"],
         "msg": {"content": "我先查一下记忆。",
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "recall_memory", "arguments": "{}"}}]}},
        {"deltas": [], "msg": {"content": None}},
        {"deltas": ["结论是以下三点："],
         "msg": {"content": "结论是以下三点："}},
    ])
    monkeypatch.setattr(console_service.llm, "_chat_json", fake)
    monkeypatch.setattr(console_service.dispatch, "run_batch", lambda *a, **k: [])
    monkeypatch.setattr(console_service.assembler, "assemble", lambda *a, **k: type(
        "C", (), {"messages": [], "as_dict": staticmethod(lambda: {})})())
    monkeypatch.setattr(console_service, "_history", lambda sid: [])
    monkeypatch.setattr(console_service, "_save", lambda *a, **k: None, raising=False)

    events = []
    console_service.chat("测试", session_id="s-test", on_event=events.append)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "segment"
    assert "turn_end" in kinds and kinds[-1] == "turn_end"
    # 第一轮（带工具）：delta 之后必须紧跟 has_tools=true 的 turn_end
    i = kinds.index("turn_end")
    assert events[i]["has_tools"] is True
    assert kinds[i - 1] == "delta"
    # 收尾轮：has_tools=false
    assert events[-1]["has_tools"] is False


def test_callback_exception_never_breaks_chat(monkeypatch):
    def boom(ev):
        raise RuntimeError("回调炸了")
    fake, _ = _fake_chat_json_seq([
        {"deltas": [], "msg": {"content": "回答"}},
    ])
    monkeypatch.setattr(console_service.llm, "_chat_json", fake)
    monkeypatch.setattr(console_service.assembler, "assemble", lambda *a, **k: type(
        "C", (), {"messages": [], "as_dict": staticmethod(lambda: {})})())
    monkeypatch.setattr(console_service, "_history", lambda sid: [])
    monkeypatch.setattr(console_service, "_save", lambda *a, **k: None, raising=False)

    res = console_service.chat("测试", session_id="s-test", on_event=boom)
    assert res["reply"] == "回答", "回调异常不能打断正在跑的对话"


def _events(body: str) -> list[dict]:
    """解析 SSE 文本为事件列表。每个块形如 "id: 3\\ndata: {...}"，不能再简单按行首判断。"""
    out = []
    for chunk in body.strip().split("\n\n"):
        if "data: " not in chunk:
            continue
        out.append(json.loads(chunk.split("data: ", 1)[1].split("\n")[0]))
    return out


def test_stream_endpoint_sse(monkeypatch):
    def fake_chat(message, session_id=None, mode="auto", skill=None, on_event=None):
        on_event({"type": "delta", "text": "你好"})
        return {"session_id": "s1", "reply": "你好", "trace": [], "ctx": {}}

    monkeypatch.setattr(console_service, "chat", fake_chat)
    monkeypatch.setattr(console_api.summarizer, "summarize", lambda sid: None)
    app = FastAPI()
    app.include_router(console_api.router)
    c = TestClient(app)

    with c.stream("POST", "/api/console/chat/stream",
                  json={"message": "hi", "mode": "auto"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes()).decode("utf-8")
    evs = _events(body)
    assert [e["type"] for e in evs] == ["delta", "done"]
    assert evs[-1]["result"]["reply"] == "你好"


def test_stream_endpoint_error_reaches_client(monkeypatch):
    def boom(*a, **k):
        raise ValueError("消息不能为空")
    monkeypatch.setattr(console_service, "chat", boom)
    app = FastAPI()
    app.include_router(console_api.router)
    c = TestClient(app)

    with c.stream("POST", "/api/console/chat/stream", json={"message": "x"}) as resp:
        body = b"".join(resp.iter_bytes()).decode("utf-8")
    evs = _events(body)
    assert evs[-1]["type"] == "error", "错误要以事件到达，而不是断流"
    assert "消息不能为空" in evs[-1]["message"]


def test_stream_id_exposed_and_events_numbered(monkeypatch):
    def fake_chat(message, session_id=None, mode="auto", skill=None, on_event=None):
        on_event({"type": "delta", "text": "a"})
        on_event({"type": "delta", "text": "b"})
        return {"session_id": "s1", "reply": "ab", "trace": [], "ctx": {}}

    monkeypatch.setattr(console_service, "chat", fake_chat)
    monkeypatch.setattr(console_api.summarizer, "summarize", lambda sid: None)
    app = FastAPI()
    app.include_router(console_api.router)
    c = TestClient(app)

    with c.stream("POST", "/api/console/chat/stream", json={"message": "hi"}) as resp:
        assert resp.headers.get("x-stream-id"), "没有流 id 就无法续传"
        evs = _events(b"".join(resp.iter_bytes()).decode("utf-8"))
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1)), "seq 必须连续递增"


def test_resume_replays_after_breakpoint(monkeypatch):
    """核心场景：客户端只收到 2 条就断了，重连后补齐剩下的，且不重复。"""
    def fake_chat(message, session_id=None, mode="auto", skill=None, on_event=None):
        for t in ("一", "二", "三"):
            on_event({"type": "delta", "text": t})
        return {"session_id": "s1", "reply": "一二三", "trace": [], "ctx": {}}

    monkeypatch.setattr(console_service, "chat", fake_chat)
    monkeypatch.setattr(console_api.summarizer, "summarize", lambda sid: None)
    app = FastAPI()
    app.include_router(console_api.router)
    c = TestClient(app)

    with c.stream("POST", "/api/console/chat/stream", json={"message": "hi"}) as resp:
        sid = resp.headers["x-stream-id"]
        full = _events(b"".join(resp.iter_bytes()).decode("utf-8"))
    assert [e["text"] for e in full if e["type"] == "delta"] == ["一", "二", "三"]

    last_seen = 2   # 只收到前两条 delta
    with c.stream("GET", f"/api/console/chat/stream/{sid}?after={last_seen}") as resp2:
        assert resp2.status_code == 200
        rest = _events(b"".join(resp2.iter_bytes()).decode("utf-8"))
    assert [e["text"] for e in rest if e["type"] == "delta"] == ["三"]
    assert rest[0]["seq"] == 3, "续传必须从断点之后开始，不能重发已收到的"
    assert rest[-1]["type"] == "done", "续传也要能拿到最终结果"


def test_resume_unknown_stream_returns_404():
    app = FastAPI()
    app.include_router(console_api.router)
    c = TestClient(app)
    r = c.get("/api/console/chat/stream/st-doesnotexist?after=0")
    assert r.status_code == 404
    assert "过期" in r.json()["detail"]
