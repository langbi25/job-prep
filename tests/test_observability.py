"""可观测中心（中文面板）测试：全部用假 Phoenix GraphQL 数据，不依赖真实 Phoenix。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import observability
from app.services import obs_service


def _fake_gql(responses):
    """按调用顺序返回预制 GraphQL 响应。"""
    calls = iter(responses)

    def fake(query):
        return next(calls)

    return fake


PROJECTS = {"projects": {"edges": [{"node": {"name": "default"}}]}}
OVERVIEW_NODE = {"projects": {"edges": [{"node": {
    "name": "default", "traceCount": 64, "tokenCountTotal": 12345,
    "tokenCountPrompt": 10000, "tokenCountCompletion": 2345,
    "p50": 120.5, "p95": 3800.0,
}}]}}


def test_zh_name_mapping():
    assert obs_service.zh_name("agent.console") == "Agent 对话"
    assert obs_service.zh_name("llm.chat") == "LLM 生成回答"
    assert obs_service.zh_name("tool.search_kb") == "工具·search_kb"
    assert obs_service.zh_name("retriever.hybrid") == "知识检索"
    assert obs_service.zh_name("chain.assemble") == "回答组装"
    assert obs_service.zh_name("fastapi.endpoint") == "请求处理"
    # HTTP 路由本就可读，原样保留
    assert obs_service.zh_name("GET /api/cards") == "GET /api/cards"


def test_overview():
    obs_service._gql = _fake_gql([OVERVIEW_NODE])
    r = obs_service.overview()
    assert r["available"] is True
    assert r["trace_count"] == 64
    assert r["tokens_total"] == 12345
    assert r["latency_p50_ms"] == 120.5


def test_list_traces_mapping():
    spans_node = {"projects": {"edges": [{"node": {"name": "default", "spans": {"edges": [
        {"node": {"name": "agent.console", "spanKind": "AGENT", "statusCode": "OK",
                  "startTime": "2026-11-11T16:00:00+00:00", "latencyMs": 4100.0,
                  "context": {"traceId": "t1", "spanId": "s1"},
                  "tokenCountTotal": 900, "numChildSpans": 3,
                  "trace": {"numSpans": 6, "statusCode": "OK"}}},
        {"node": {"name": "GET /health", "spanKind": "unknown", "statusCode": "ERROR",
                  "startTime": "2026-11-11T15:00:00+00:00", "latencyMs": 5.0,
                  "context": {"traceId": "t2", "spanId": "s2"},
                  "tokenCountTotal": 0, "numChildSpans": 2,
                  "trace": {"numSpans": 4, "statusCode": "ERROR"}}},]}}}]}}
    obs_service._gql = _fake_gql([spans_node])
    r = obs_service.list_traces(10)
    assert r["available"] is True
    a, b = r["items"]
    assert a["name_zh"] == "Agent 对话" and a["kind_zh"] == "智能体"
    assert a["span_count"] == 6 and a["tokens"] == 900 and a["error"] is False
    assert b["name_zh"] == "GET /health" and b["error"] is True


def test_trace_detail_tree_and_attrs():
    tr = {"projects": {"edges": [{"node": {"name": "default", "trace": {
        "traceId": "t1", "startTime": "2026-11-11T16:00:00+00:00",
        "latencyMs": 4000.0, "numSpans": 3,
        "spans": {"edges": [
            {"node": {"name": "agent.console", "spanKind": "AGENT", "statusCode": "OK",
                      "statusMessage": "", "parentId": None,
                      "startTime": "2026-11-11T16:00:00+00:00",
                      "endTime": "2026-11-11T16:00:04+00:00", "latencyMs": 4000.0,
                      "context": {"spanId": "root"}, "tokenCountTotal": 0,
                      "tokenCountPrompt": 0, "tokenCountCompletion": 0,
                      "attributes": {"openinference.span.kind": "AGENT"}}},
            {"node": {"name": "llm.chat", "spanKind": "LLM", "statusCode": "OK",
                      "statusMessage": "", "parentId": "root",
                      "startTime": "2026-11-11T16:00:00.5+00:00",
                      "endTime": "2026-11-11T16:00:03.5+00:00", "latencyMs": 3000.0,
                      "context": {"spanId": "llm"}, "tokenCountTotal": 900,
                      "tokenCountPrompt": 700, "tokenCountCompletion": 200,
                      "attributes": {"llm": {"model_name": "gpt-x", "scene": "study_plan:bigdata",
                                             "temperature": 0.1},
                                     "input": {"value": "帮我做个复习计划", "mime_type": "text"},
                                     "output": {"value": "好的，计划如下…", "mime_type": "text"}}}},
            {"node": {"name": "tool.search_kb", "spanKind": "TOOL", "statusCode": "ERROR",
                      "statusMessage": "boom", "parentId": "root",
                      "startTime": "2026-11-11T16:00:03.6+00:00",
                      "endTime": "2026-11-11T16:00:03.7+00:00", "latencyMs": 100.0,
                      "context": {"spanId": "tool"}, "tokenCountTotal": 0,
                      "tokenCountPrompt": 0, "tokenCountCompletion": 0,
                      "attributes": {"tool": {"name": "search_kb", "source": "console"},
                                     "input": {"value": "实时数仓架构"}}}},]}}}}]}}
    obs_service._gql = _fake_gql([tr])
    r = obs_service.trace_detail("t1")
    assert r["available"] is True and r["num_spans"] == 3
    root = r["tree"][0]
    assert root["name_zh"] == "Agent 对话" and len(root["children"]) == 2
    llm, tool = root["children"]
    assert llm["offset_ms"] == 500.0 and llm["attrs"]["model"] == "gpt-x"
    assert llm["attrs"]["scene"] == "study_plan:bigdata"
    assert "计划" in llm["attrs"]["output"]
    assert tool["error"] is True and tool["error_message"] == "boom"
    assert tool["name_zh"] == "工具·search_kb"


def test_api_degrades_when_phoenix_down():
    """Phoenix 挂掉时返回 available=False 而不是 500。"""
    def boom(query):
        raise obs_service.PhoenixUnavailable("Phoenix 连接失败：refused")

    obs_service._gql = boom
    app = FastAPI()
    app.include_router(observability.router)
    c = TestClient(app)
    for path in ("/api/obs/overview", "/api/obs/traces", "/api/obs/traces/t1"):
        r = c.get(path)
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False and "Phoenix" in body["reason"]
