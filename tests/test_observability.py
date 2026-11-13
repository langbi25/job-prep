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
    "name": "default", "totalCount": 382, "agentCount": 15,
    "tokenCountTotal": 12345, "tokenCountPrompt": 10000, "tokenCountCompletion": 2345,
    "p50": 2102.7, "p95": 3800.0,
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
    queries = []

    def fake(query):
        queries.append(query)
        return OVERVIEW_NODE

    obs_service._gql = fake
    r = obs_service.overview("agent")
    assert r["available"] is True
    assert r["trace_count"] == 15            # agent 口径
    assert r["trace_count_all"] == 382
    assert "span_kind == 'AGENT'" in queries[-1]  # 查询里带了 Agent 过滤
    r2 = obs_service.overview("all")
    assert r2["trace_count"] == 382          # all 口径


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
    r = obs_service.list_traces(10, "agent")
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


def test_api_degrades_when_phoenix_down(monkeypatch):
    """Phoenix 挂掉时返回 available=False 而不是 500。

    显式固定 backend：.env 里 OBS_BACKEND 可能已切到 langfuse，
    这个用例只验证 phoenix 分支的降级语义。
    """
    monkeypatch.setenv("OBS_BACKEND", "phoenix")
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


def test_trace_detail_includes_events():
    """span events（首 token / 工具动作）要进详情接口。"""
    tr = {"projects": {"edges": [{"node": {"name": "default", "trace": {
        "traceId": "t9", "startTime": "2026-11-12T00:00:00+00:00",
        "latencyMs": 1000.0, "numSpans": 1,
        "spans": {"edges": [
            {"node": {"name": "llm.chat", "spanKind": "LLM", "statusCode": "OK",
                      "statusMessage": "", "parentId": None,
                      "startTime": "2026-11-12T00:00:00+00:00",
                      "endTime": "2026-11-12T00:00:01+00:00", "latencyMs": 1000.0,
                      "context": {"spanId": "l1"}, "tokenCountTotal": 300,
                      "tokenCountPrompt": 200, "tokenCountCompletion": 100,
                      "attributes": {"llm": {"model_name": "deepseek-chat"}},
                      "events": [
                          {"name": "first_token",
                           "timestamp": "2026-11-12T00:00:00.400+00:00",
                           "attributes": {"ttft_ms": 400.0}},
                          {"name": "gen_ai.usage",
                           "timestamp": "2026-11-12T00:00:01+00:00",
                           "attributes": {"prompt_tokens": 200, "completion_tokens": 100}},
                      ]}},
        ]}}}}]}}

    obs_service._gql = _fake_gql([tr])
    r = obs_service.trace_detail("t9")
    llm = r["tree"][0]
    assert [e["name"] for e in llm["events"]] == ["first_token", "gen_ai.usage"]
    ft = llm["events"][0]
    assert ft["offset_ms"] == 400.0 and ft["attrs"]["ttft_ms"] == 400.0
    assert llm["attrs"]["model"] == "deepseek-chat"


def test_sessions_and_turns():
    """会话列表 + 轮次下钻：session.id 聚合、turn 取打点侧记录值。

    假数据用辅助函数构造——深层嵌套 dict 字面量的闭合括号靠手数必错（本文件前科累累）。
    """
    def proj(payload):
        return {"projects": {"edges": [{"node": {"name": "default", **payload}}]}}

    def session_node(session_id, traces=None, **extra):
        return {"node": {"sessionId": session_id, "startTime": "2026-11-12T00:00:00+00:00",
                         "endTime": "2026-11-12T00:10:00+00:00", **extra,
                         **({"traces": {"edges": [{"node": t} for t in traces]}} if traces else {})}}

    def trace_node(trace_id, start, latency, spans, root="agent.console"):
        return {"traceId": trace_id, "startTime": start, "latencyMs": latency,
                "numSpans": 3, "errorCount": 0,
                "rootSpan": {"name": root, "latencyMs": latency},
                "spans": {"edges": [{"node": {"attributes": spans}}]}}

    obs_service._gql = _fake_gql([proj({"sessions": {"edges": [session_node(
        "cs-1", numTraces=2, numTracesWithError=0, firstInput={"value": "帮我做计划"},
        **{"tokenUsage": {"total": 5000}})]}})])
    r = obs_service.list_sessions(10)
    s = r["items"][0]
    assert s["session_id"] == "cs-1" and s["turns"] == 2 and s["tokens"] == 5000

    t1 = trace_node("t1", "2026-11-12T00:05:00+00:00", 2000.0,
                    "{'session': {'id': 'cs-1', 'turn': 2}}")
    t2 = trace_node("t2", "2026-11-12T00:00:00+00:00", 1500.0, "{}")
    obs_service._gql = _fake_gql([proj({"sessions": {"edges": [session_node("cs-1", traces=[t1, t2])]}})])
    d = obs_service.session_detail("cs-1")
    assert d["available"] is True and len(d["turns"]) == 2
    # 按时间排序后：t2 是第 1 轮（无 turn 属性 → 用序号），t1 打点记录了 turn=2
    assert d["turns"][0]["trace_id"] == "t2" and d["turns"][0]["turn"] == 1
    assert d["turns"][1]["trace_id"] == "t1" and d["turns"][1]["turn"] == 2
    assert d["turns"][1]["name_zh"] == "Agent 对话"
