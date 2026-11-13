"""单工具启停测试。

开关的价值全在"真的调不动"上。四层各对应一个断言：
1. 文件语义：启用 = 从文件消失（默认即启用），停用 = 写一条带原因的记录；
2. 工具清单：模型看不见它（schema 与 impl 都要剔，只剔 schema 等于没剔）；
3. 管理面板：仍然看得到它，否则停用之后就再也开不回来；
4. 执行层：历史消息 / 编排白名单仍点名它时被拒，且实现函数一次都没被调用。
"""
import json

import pytest

from app.core import tool_flags
from app.mcp import dispatch


@pytest.fixture()
def flags(tmp_path, monkeypatch):
    p = tmp_path / "tool_flags.json"
    monkeypatch.setattr(tool_flags, "FLAGS_PATH", p)
    return p


def test_disable_writes_reason_and_enable_removes(flags):
    assert tool_flags.is_enabled("get_daily_brief")
    tool_flags.set_enabled("get_daily_brief", False, "上游接口挂了")
    assert not tool_flags.is_enabled("get_daily_brief")
    assert tool_flags.blocked_reason("get_daily_brief") == "上游接口挂了"
    assert json.loads(flags.read_text(encoding="utf-8"))["get_daily_brief"]["reason"] == "上游接口挂了"

    tool_flags.set_enabled("get_daily_brief", True)
    assert tool_flags.is_enabled("get_daily_brief")
    assert tool_flags.blocked_reason("get_daily_brief") is None
    assert "get_daily_brief" not in json.loads(flags.read_text(encoding="utf-8")), \
        "启用 = 回归默认（文件里不该留 enabled:true 的残渣）"


def test_corrupt_file_means_all_enabled(flags):
    """开关文件写坏了不能把全部工具锁死——那会让整个 agent 变成哑巴。"""
    flags.write_text("{ 这不是 json", encoding="utf-8")
    assert tool_flags.is_enabled("any_tool")
    assert tool_flags.snapshot()["disabled_count"] == 0


def test_all_tools_hides_disabled_from_model(flags):
    """停用后模型看不到它：schema 和 impl 都剔掉。

    只剔 schema 是个常见错误——技能白名单点名字仍能在 impl 里找到函数，
    停用就只停了一半。
    """
    from app.services import harness_admin

    before_schemas, before_impl = harness_admin.all_tools()
    assert any((t.get("function") or {}).get("name") == "get_daily_brief" for t in before_schemas)
    assert "get_daily_brief" in before_impl

    tool_flags.set_enabled("get_daily_brief", False, "测试")
    after_schemas, after_impl = harness_admin.all_tools()
    assert not any((t.get("function") or {}).get("name") == "get_daily_brief"
                   for t in after_schemas)
    assert "get_daily_brief" not in after_impl


def test_panel_still_lists_disabled_tool(flags):
    """面板必须还能看到它，否则停用之后没有入口开回来。"""
    from app.services import harness_admin

    tool_flags.set_enabled("get_daily_brief", False, "测试停用")
    rows = {t["name"]: t for t in harness_admin.tool_entries(days=1)}
    assert "get_daily_brief" in rows
    assert rows["get_daily_brief"]["enabled"] is False
    assert rows["get_daily_brief"]["disabled_reason"] == "测试停用"

    ov = harness_admin.overview(days=1)
    assert ov["tools"]["disabled"] == 1
    assert ov["tools"]["enabled"] == ov["tools"]["total"] - 1


def test_disabled_tool_shows_up_in_skill_lint(flags):
    """停用工具的连带影响必须出现在体检里。

    技能白名单里的工具被停用后，它跑起来必然验收失败（缺必需工具），
    而运行记录只会显示"失败"——不看体检就只能靠猜。
    """
    from app.services import harness_admin

    tool_flags.set_enabled("get_top_questions", False, "上游挂了")
    rows = {e["name"]: e["issues"] for e in harness_admin.skill_entries(days=1)}
    codes = [i["code"] for i in rows.get("gap_to_cards", [])]
    assert "W_TOOL_DISABLED" in codes
    msg = next(i["msg"] for i in rows["gap_to_cards"] if i["code"] == "W_TOOL_DISABLED")
    assert "get_top_questions" in msg and "验收失败" in msg


def test_execution_is_blocked_and_impl_never_called(flags):
    """核心断言：停用后即使有人点名调用，实现函数也不会被执行一次。"""
    called = []
    impl = {"get_daily_brief": lambda args: called.append(1) or {"ok": True}}
    schemas = dispatch.build_schemas([{
        "type": "function",
        "function": {"name": "get_daily_brief", "description": "x",
                     "parameters": {"type": "object", "properties": {}}},
    }])
    call = {"id": "c1", "function": {"name": "get_daily_brief", "arguments": "{}"}}

    ok = dispatch._exec_one(call, impl, schemas, session_id=None, turn=1,
                            readonly=True, cache={})
    assert ok.ok is True and called, "启用时应当正常执行（前置条件）"

    tool_flags.set_enabled("get_daily_brief", False, "正在改坏数据")
    called.clear()
    blocked = dispatch._exec_one(call, impl, schemas, session_id=None, turn=1,
                                 readonly=True, cache={})
    assert blocked.ok is False
    assert blocked.error == "disabled", "台账要能按 disabled 单独统计"
    assert not called, "实现函数一次都不该被调用"
    body = json.loads(blocked.payload)
    assert "已被平台停用" in body["error"] and "不要重试" in body["error"]
    assert "正在改坏数据" in body["error"], "原因要传给模型，它才知道该怎么绕"
