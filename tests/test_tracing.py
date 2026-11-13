"""Trace 采集层测试：不需要后端，用 InMemorySpanExporter 直接断言 span 树。

覆盖四件事：
1. 未配置 endpoint 时**照样有本地 span**（enabled()=False，但 span() 返回真实对象
   并落 spans 表）——这是"有没有起观测后端"不该决定"有没有数据"的那条线；
2. 启用后 span 树形状正确：AGENT 根 → LLM/TOOL 子 span，父子关系成立；
3. 属性符合 OpenInference/GenAI 语义约定（openinference.span.kind、gen_ai.usage.*）；
4. 横切场景标签（set_scene）与长文本截断生效。
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.pop("TRACING_ENDPOINT", None)  # .env 里配了后端也别影响本测试

import pytest

from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import db
from app.observability import tracing as tr


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """所有用例都跑在临时库上。

    span 现在默认持久化，不隔离的话每跑一次测试就往开发库里灌一批
    `tool.get_daily_brief` 假 trace——真实库的调用树里混进 pytest 的东西，
    比没有数据更糟：它看起来是真的。
    """
    db.close_thread_conns()
    tr.close_span_conn()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()
    tr.close_span_conn()


def _spans(trace_id: str) -> list[dict]:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT span_id,parent_id,name,kind,duration_ms,status,attrs_json,events_json "
            "FROM spans WHERE trace_id=? ORDER BY seq", (trace_id,))]
    finally:
        conn.close()


def test_disabled_by_default():
    """不起后端时的行为：没有 OTel，但**本地调用树完整**。

    这里曾经断言 `span()` 返回 None（"业务不感知"）。那个设计的代价是：
    工具输出长度、检索命中数、首 token 延迟、token 用量——只有装了 Phoenix
    才记得到，本地一条都没有。观测的价值一半在事后，事后往往拿不出一个后端。
    """
    tr._reset_for_test()
    os.environ.pop("TRACING_ENDPOINT", None)
    assert tr.enabled() is False
    with tr.agent_span("agent.x") as tid:
        assert len(tid) == 32 and int(tid, 16) > 0
        # 关键：OTel 关着，链路内也要能拿到同一个 trace_id。
        assert tr.current_trace_id() == tid
        with tr.span("llm.chat", tr.KIND_LLM, {"llm.scene": "console:assist"}) as s:
            assert s is not None, "OTel 关着也必须是真实对象"
            s.set_attribute("gen_ai.usage.prompt_tokens", 12)
            tr.add_event(s, "first_token", ttft_ms=88)
    assert tr.current_trace_id() == "", "span 退出后必须复位，不能把 id 泄漏到下一轮"

    rows = _spans(tid)
    assert [r["name"] for r in rows] == ["agent.x", "llm.chat"]
    assert rows[1]["parent_id"] == rows[0]["span_id"], "父子关系不依赖 OTel 上下文"
    assert rows[1]["kind"] == tr.KIND_LLM
    assert '"gen_ai.usage.prompt_tokens": 12' in rows[1]["attrs_json"]
    assert '"ttft_ms": 88' in rows[1]["events_json"]
    print("ok 无后端时本地调用树仍然完整")


def test_span_tree_and_semantics():
    tr._reset_for_test()
    exp = InMemorySpanExporter()
    assert tr.configure(endpoint="http://localhost:9999", exporter=exp,
                        service_name="job-prep-test") is True
    assert tr.enabled() is True
    assert tr.configure(endpoint="http://other") is True  # 幂等：不叠加 processor

    with tr.agent_span("agent.console", {"agent.mode": "auto"}) as trace_id:
        tr.set_scene("console:auto")
        with tr.span("llm.chat", tr.KIND_LLM, {
                tr.ATTR_MODEL: "deepseek-chat", "llm.scene": tr.current_scene(),
                "input.value": "x" * 3000}) as s:
            s.set_attribute(tr.ATTR_PROMPT_TOKENS, 120)
            s.set_attribute(tr.ATTR_COMPLETION_TOKENS, 45)
        with tr.span("tool.search_kb", tr.KIND_TOOL, {"tool.name": "search_kb"}) as s:
            s.set_attribute("output.value", '{"hits": 3}')

    spans = exp.get_finished_spans()
    assert len(spans) == 3, f"应导出 3 个 span，实际 {len(spans)}"
    root = next(s for s in spans if s.name == "agent.console")
    llm = next(s for s in spans if s.name == "llm.chat")
    tool = next(s for s in spans if s.name == "tool.search_kb")

    # 根 span：AGENT kind + trace_id 落库键与 OTel trace 一致
    assert root.attributes[tr.OI_KIND] == tr.KIND_AGENT
    assert format(root.context.trace_id, "032x") == trace_id
    assert root.parent is None

    # 父子关系：llm/tool 挂在 agent 下（OTel 用 contextvars 传播，业务没穿一个参数）
    assert llm.parent.span_id == root.context.span_id
    assert tool.parent.span_id == root.context.span_id
    assert llm.attributes[tr.OI_KIND] == tr.KIND_LLM
    assert tool.attributes[tr.OI_KIND] == tr.KIND_TOOL

    # GenAI 语义属性 + 场景标签 + 长文本截断
    assert llm.attributes[tr.ATTR_MODEL] == "deepseek-chat"
    assert llm.attributes[tr.ATTR_PROMPT_TOKENS] == 120
    assert llm.attributes["llm.scene"] == "console:auto"
    assert len(llm.attributes["input.value"]) <= tr.MAX_ATTR_CHARS + 20

    # 异常也会被记录，且原样上抛（业务照常处理）
    try:
        with tr.span("tool.bad", tr.KIND_TOOL, {}):
            raise ValueError("boom")
    except ValueError:
        pass
    bad = next(s for s in exp.get_finished_spans() if s.name == "tool.bad")
    assert bad.status.is_status_error if hasattr(bad.status, "is_status_error") else \
        bad.status.status_code.name == "ERROR"
    print("ok span 树形状 + OpenInference/GenAI 语义 + 截断 + 异常记录")


def test_reconfigure_after_reset():
    """重置后可换 exporter：评测/测试可以各自隔离采集。"""
    tr._reset_for_test()
    exp = InMemorySpanExporter()
    tr.configure(endpoint="http://localhost:9999", exporter=exp, service_name="t2")
    with tr.span("llm.chat", tr.KIND_LLM, {}):
        pass
    assert len(exp.get_finished_spans()) == 1
    print("ok reset 后可重新配置")


def test_global_provider_stays_unconfigured():
    """回归：configure() 不得注册全局 TracerProvider。

    FastAPI 0.162+ 的 ASGI telemetry 以「全局 provider 已配置」为激活条件，
    一旦注册，每次 /health 会多出 4 条 span（实测 12h 内 99.6% 的 span 是这种噪音）。
    这里用「全局 tracer 拿到的 span 是 NonRecording」来锁住这条约束——
    要比 isinstance(provider, ProxyTracerProvider) 更贴近实际后果。
    """
    from opentelemetry.trace import NonRecordingSpan

    tr._reset_for_test()
    exp = InMemorySpanExporter()
    tr.configure(endpoint="http://localhost:9999", exporter=exp, service_name="guard")
    with trace.get_tracer("probe").start_as_current_span("probe") as s:
        assert isinstance(s, NonRecordingSpan), "全局 provider 被注册了，FastAPI 自动埋点会复活"
    print("ok 全局 provider 保持未配置（FastAPI 自动埋点关闭）")


def test_session_turn_and_events():
    """session/轮次注入 + span events：多轮对话可观测的客观侧。"""
    tr._reset_for_test()
    exp = InMemorySpanExporter()
    tr.configure(endpoint="http://localhost:9999", exporter=exp, service_name="t3")

    tr.set_session("cs-20261112-000001", 3)
    with tr.agent_span("agent.console", {"agent.mode": "auto"}):
        with tr.span("llm.chat", tr.KIND_LLM, {}) as s:
            tr.add_event(s, "first_token", ttft_ms=321.5)
            tr.record_tool_action(s, {"inserted": ["q1", "q2"], "skipped": 1})
        with tr.span("tool.search_kb", tr.KIND_TOOL, {}) as s:
            tr.record_tool_action(s, {"path": "docs/x.md"})  # 文件动作
        with tr.span("tool.readonly", tr.KIND_TOOL, {}) as s:
            tr.record_tool_action(s, {"ok": True})           # 纯读：不应产生动作
    spans = {s.name: s for s in exp.get_finished_spans()}

    root, llm, tool = spans["agent.console"], spans["llm.chat"], spans["tool.search_kb"]
    # 全部 span 自动带 session.id / session.turn（子 span 继承，不用穿参）
    for s in (root, llm, tool):
        assert s.attributes[tr.ATTR_SESSION_ID] == "cs-20261112-000001"
        assert s.attributes[tr.ATTR_SESSION_TURN] == 3
    # 首 token 事件
    ev_names = [e.name for e in llm.events]
    assert "first_token" in ev_names
    ft = next(e for e in llm.events if e.name == "first_token")
    assert ft.attributes["ttft_ms"] == 321.5
    # 动作打点：写动作有事件，纯读没有
    assert "tool.action" in ev_names
    act = next(e for e in llm.events if e.name == "tool.action")
    assert act.attributes["写入 inserted"] == "2"
    assert llm.attributes["tool.action"].startswith("写入 inserted")
    assert "tool.action" in [e.name for e in tool.events]
    assert "tool.action" not in [e.name for e in spans["tool.readonly"].events]
    # 会话上下文可清（新一轮换会话时不残留）
    tr.set_session("", 0)
    with tr.span("llm.chat", tr.KIND_LLM, {}) as s2:
        pass
    s2 = [x for x in exp.get_finished_spans() if x.name == "llm.chat"][-1]
    assert tr.ATTR_SESSION_ID not in s2.attributes
    print("ok session/轮次注入 + 首token/动作事件")


if __name__ == "__main__":
    test_disabled_by_default()
    test_span_tree_and_semantics()
    test_reconfigure_after_reset()
    test_session_turn_and_events()
    print("4/4 通过")
