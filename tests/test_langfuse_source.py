"""Langfuse 数据源适配层测试（假数据，不依赖真实实例）。

重点验证三件容易错的事：
1. usage 结构在不同 Langfuse 版本里不一样（input/output vs promptTokens/completionTokens）
2. latency 单位是秒，面板要毫秒
3. v4 没有会话实体——会话要靠 sessionId 聚合，轮次要按时间排序后重新编号
"""
import os

from app.services import langfuse_service as lf


def obs(oid, trace, parent=None, name="agent.console", otype="AGENT",
        start="2026-11-12T00:00:00Z", latency=1.0, session="cs-1",
        usage=None, level="DEFAULT", input_=None):
    """构造一条 v2 observation 假数据。"""
    return {
        "id": oid, "traceId": trace, "parentObservationId": parent,
        "name": name, "type": otype, "startTime": start,
        "latency": latency, "sessionId": session, "level": level,
        "usage": usage or {}, "input": input_,
    }


def test_usage_tokens_supports_both_shapes():
    assert lf._usage_tokens({"usage": {"input": 120, "output": 30}}) == (120, 30)
    assert lf._usage_tokens({"usage": {"promptTokens": 7, "completionTokens": 3}}) == (7, 3)
    assert lf._usage_tokens({"usage": None}) == (0, 0)
    assert lf._usage_tokens({}) == (0, 0)


def test_latency_seconds_to_ms():
    """Langfuse 的 latency 是秒（1.5 → 1500ms），别直接当毫秒用。"""
    assert lf._latency_ms({"latency": 1.5}) == 1500.0
    assert lf._latency_ms({"latency": 0.42}) == 420.0
    # 缺 latency 时用 end-start 兜底
    assert lf._latency_ms({"startTime": "2026-11-12T00:00:00Z",
                           "endTime": "2026-11-12T00:00:02Z"}) == 2000.0


def test_kind_mapping():
    assert lf._kind({"type": "GENERATION"}) == "LLM"
    assert lf._kind({"type": "AGENT"}) == "AGENT"
    assert lf._kind({"type": "RETRIEVER"}) == "RETRIEVER"
    # 类型不认识时按名字前缀兜底
    assert lf._kind({"type": "weird", "name": "tool.search_kb"}) == "TOOL"


def test_list_traces_maps_and_filters():
    data = [
        obs("a1", "t-agent", name="agent.console", start="2026-11-12T00:00:02Z"),
        obs("a2", "t-agent", parent="a1", name="llm.chat", otype="GENERATION",
            start="2026-11-12T00:00:02.5Z", usage={"input": 100, "output": 20}),
        obs("h1", "t-http", name="GET /health", otype="SPAN", start="2026-11-12T00:00:01Z"),
    ]
    lf._fetch_observations = lambda **kw: data
    lf._get = lambda *a, **k: {}  # 不让它真发请求

    agent_only = lf.list_traces(10, "agent")
    assert [r["trace_id"] for r in agent_only["items"]] == ["t-agent"]
    row = agent_only["items"][0]
    assert row["name_zh"] == "Agent 对话"
    assert row["kind_zh"] == "智能体"
    assert row["latency_ms"] == 1000.0
    assert row["span_count"] == 2
    assert row["tokens"] == 120  # 根上没有 usage，从子树汇总
    assert row["session_id"] == "cs-1"

    both = lf.list_traces(10, "all")
    assert {r["trace_id"] for r in both["items"]} == {"t-agent", "t-http"}


def test_trace_detail_builds_tree():
    data = [
        obs("r", "t1", name="agent.console", start="2026-11-12T00:00:00Z", latency=2.0),
        obs("c1", "t1", parent="r", name="llm.chat", otype="GENERATION",
            start="2026-11-12T00:00:00.5Z", latency=1.0, usage={"input": 50, "output": 10}),
        obs("c2", "t1", parent="r", name="tool.search_kb", otype="TOOL",
            start="2026-11-12T00:00:01.6Z", latency=0.3),
    ]
    lf._fetch_observations = lambda **kw: data
    d = lf.trace_detail("t1")
    assert d["num_spans"] == 3 and len(d["tree"]) == 1
    root = d["tree"][0]
    assert root["name_zh"] == "Agent 对话"
    assert [c["name_zh"] for c in root["children"]] == ["LLM 生成回答", "工具·search_kb"]
    assert root["children"][0]["offset_ms"] == 500.0
    assert root["children"][0]["prompt_tokens"] == 50
    assert root["children"][1]["kind_zh"] == "工具调用"


def test_session_turns_renumbered_by_time():
    """v4 没有会话实体，轮次得按 startTime 排序后重新编号。"""
    data = [
        obs("x1", "t-late", name="agent.console", start="2026-11-12T00:05:00Z"),
        obs("x2", "t-early", name="agent.console", start="2026-11-12T00:00:00Z"),
    ]
    lf._fetch_observations = lambda **kw: data

    r = lf.list_sessions(5)
    s = r["items"][0]
    assert s["session_id"] == "cs-1" and s["turns"] == 2

    d = lf.session_detail("cs-1")
    assert [(t["turn"], t["trace_id"]) for t in d["turns"]] == [(1, "t-early"), (2, "t-late")]
    assert d["turns"][0]["name_zh"] == "Agent 对话"


def test_api_dispatch_switches_backend(monkeypatch):
    """OBS_BACKEND=langfuse 时 API 层要切到 Langfuse 实现。"""
    from app.api import observability

    monkeypatch.setenv("OBS_BACKEND", "langfuse")
    monkeypatch.setattr(lf, "list_traces", lambda limit=50, scope="agent": {"backend": "langfuse", "items": []})
    r = observability.list_traces(10, "agent")
    assert r["backend"] == "langfuse"

    monkeypatch.setenv("OBS_BACKEND", "phoenix")
    assert observability.backend_name() == "phoenix"


def test_api_degrades_when_langfuse_down(monkeypatch):
    from app.api import observability

    monkeypatch.setenv("OBS_BACKEND", "langfuse")

    def boom(*a, **k):
        raise lf.LangfuseUnavailable("连接被拒绝")

    monkeypatch.setattr(lf, "list_traces", boom)
    r = observability.list_traces(10, "agent")
    assert r["available"] is False and "连接被拒绝" in r["reason"]
    assert r["backend"] == "langfuse"
