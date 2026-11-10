"""Trace 采集层测试：不需要后端，用 InMemorySpanExporter 直接断言 span 树。

覆盖四件事：
1. 未配置 endpoint 时零开销零异常（enabled()=False，agent_span 退化本地 uuid）；
2. 启用后 span 树形状正确：AGENT 根 → LLM/TOOL 子 span，父子关系成立；
3. 属性符合 OpenInference/GenAI 语义约定（openinference.span.kind、gen_ai.usage.*）；
4. 横切场景标签（set_scene）与长文本截断生效。
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.pop("TRACING_ENDPOINT", None)  # .env 里配了后端也别影响本测试

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability import tracing as tr


def test_disabled_by_default():
    tr._reset_for_test()
    os.environ.pop("TRACING_ENDPOINT", None)
    assert tr.enabled() is False
    with tr.agent_span("agent.x") as tid:
        assert len(tid) == 32 and int(tid, 16) > 0  # 本地 uuid，仍是合法关联键
    with tr.span("llm.chat", tr.KIND_LLM, {"a": 1}) as s:
        assert s is None  # no-op，业务不感知
    assert tr.current_trace_id() == ""
    print("ok 未启用时零开销零异常")


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


if __name__ == "__main__":
    test_disabled_by_default()
    test_span_tree_and_semantics()
    test_reconfigure_after_reset()
    print("3/3 通过")
