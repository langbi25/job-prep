"""工具注册表测试：钉住「工具清单只有一个事实源」这个不变量。

这些断言的价值不在覆盖率，而在**防漂移**。此前同一批能力有三份手写清单
（harness.TOOLS 6 个 / agent_tools.SCHEMAS 11 个 / gateway.SPECS 9 个），
描述各写一遍；改一处漏两处时没有任何测试会红，要等到模型照着过期说明乱传参数
才在现场暴露。现在三处都从 registry 派生，下面逐条钉住派生关系。

不依赖 pytest（项目里没装），pytest 与 `python tests/test_tool_registry.py` 双模式可跑。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import spec  # noqa: E402
from app.mcp import gateway, registry, tools  # noqa: E402
from app.mcp.server import mcp  # noqa: E402
from app.services import agent_tools, harness  # noqa: E402


def _agent_impl() -> dict:
    return {**harness._IMPL, **agent_tools.IMPL}


def test_agent_surface_matches_derived_schemas():
    """harness.TOOLS + agent_tools.SCHEMAS 恰好是注册表的 agent 面，逐条 schema 完全相等。

    相等（而不是"包含"）是刻意的：只要有人绕过 registry 手写一条 schema，这里就红。
    """
    names = [t["function"]["name"] for t in list(harness.TOOLS) + list(agent_tools.SCHEMAS)]
    assert names == registry.names("agent"), names
    for t in list(harness.TOOLS) + list(agent_tools.SCHEMAS):
        d = registry.get(t["function"]["name"])
        assert d is not None
        assert t == d.schema(), t["function"]["name"]


def test_no_tool_declared_twice():
    """两个模块不能实现同名工具——同名意味着两条实现路径，调用时只有一条会被走到。"""
    a = {t["function"]["name"] for t in harness.TOOLS}
    b = {t["function"]["name"] for t in agent_tools.SCHEMAS}
    assert not (a & b), a & b


def test_every_registered_tool_has_handler():
    """模型看得见却调不动，比看不见更糟：一次失败后它会开始乱试别的工具。"""
    impl = _agent_impl()
    assert registry.missing_handlers(impl, "agent") == []
    # 反向：实现了却没登记 = 死代码，模型永远看不到
    assert registry.orphan_handlers(impl) == []


def test_mcp_surface_is_exactly_gateway_specs():
    """对外清单与 SPECS 必须一一对应且同序（顺序即 tools/list 的返回顺序）。"""
    assert list(gateway.SPECS) == registry.names("mcp")
    assert all(s.name == n for n, s in gateway.SPECS.items())
    assert all(s.handler is not None for s in gateway.SPECS.values())


def test_readonly_flag_agrees_with_write_tools():
    """读写标记只有一处事实源。写工具的 read_only 错了，等于给外部客户端开了写权限。"""
    writes = {n for n in registry.names("agent") if not registry.get(n).read_only}
    assert writes == set(agent_tools.WRITE_TOOLS), writes ^ set(agent_tools.WRITE_TOOLS)
    assert {n for n, s in gateway.SPECS.items() if not s.read_only} == {
        "draft_cards", "ingest_text", "ingest_retrospective"}


def test_every_tool_has_short_summary():
    """summary 是给人看的一行摘要（面板、catalog、tools/list 的 description），
    过长会在界面上被截断，变成"看到一半的说明"——比没有更误导。"""
    for name, d in registry.REGISTRY.items():
        assert d.summary and len(d.summary) <= 60, (name, len(d.summary))
        assert d.description and len(d.description) >= len(d.summary) * 0.6, name


def test_parameters_are_wellformed():
    """required 引用不存在的属性，是 JSON Schema 最容易被忽略的一类错误：
    模型照 required 填了字段，服务端却按 properties 校验，报错信息对不上。"""
    for name, d in registry.REGISTRY.items():
        p = d.parameters
        assert p.get("type") == "object", name
        props = p.get("properties") or {}
        req = set(p.get("required") or [])
        assert req <= set(props), (name, req - set(props))
        for k, v in props.items():
            assert isinstance(v, dict) and v.get("type"), (name, k)


def test_enum_follows_business_lines():
    """参数里的枚举是快照，业务线改了就过期。这条断言把"过期"变成红灯而不是线上乱填。"""
    want = spec.enum()
    assert registry.get("get_weak_topics").parameters["properties"]["direction"]["enum"] == want
    assert registry.get("make_study_plan").parameters["properties"]["direction"]["enum"] == want


def test_get_card_is_mcp_only():
    """刻意决定，钉住防漂移：get_card 只对外，内置循环靠 search_kb 的要点摘要。

    哪天要给 Agent 也开全文下钻，改这个断言的人会先看到这段注释——
    然后需要同时评估"多一次工具调用会不会拖慢学习计划"。
    """
    assert registry.get("get_card").surfaces == ("mcp",)
    assert "get_card" not in harness._IMPL


def test_mcp_signature_layer_agrees_with_registry():
    """MCP 侧的 inputSchema 由 pydantic 注解生成（SDK 要求函数签名），与注册表是两套描述。

    这里比对属性名、required 与枚举：三者任一不一致，外部客户端看到的参数就与实际接受的
    参数不同。`draft_cards` 曾漏了 topic——文档要求"按主题补卡必须传 topic"，
    MCP 客户端却传不进来，只能从全池抓题、补错主题。
    """
    listed = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(listed) == set(registry.names("mcp"))
    for name, t in listed.items():
        d = registry.get(name)
        got = t.input_schema.get("properties") or {}
        want = d.parameters.get("properties") or {}
        assert set(got) == set(want), (name, set(got) ^ set(want))
        assert set(t.input_schema.get("required") or []) == set(d.parameters.get("required") or []), name
        for k in set(got) & set(want):
            assert (got[k] or {}).get("enum") == (want[k] or {}).get("enum"), (name, k)


def test_registered_signatures_cover_mcp_surface():
    """签名层（tools.REGISTERED）必须是 mcp 面的完整投影，顺序一致。"""
    assert [f.__name__ for f in tools.REGISTERED] == registry.names("mcp")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
