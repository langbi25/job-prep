"""Harness 管理台测试（零 LLM 调用）。

测的是"平台能不能真的管住东西"，不是页面能不能开：
工具目录与实现一致、体检能抓出真实故障、保存拒绝坏版本且不动原文、
启停真的影响路由、回滚真能回去、台账真的落行。

跑法：pytest tests/test_harness_admin.py -q
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.mcp import dispatch  # noqa: E402
from app.services import harness_admin as ha  # noqa: E402
from app.skills import registry  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="harness-test-"))
_SKILLS = _TMP / "skills"

# 这个模块会把注册表指向临时目录，必须原样还回去：
# 否则同一次 pytest 里后跑的 test_skills 会读到只有 demo_skill 的空目录，
# 表现成"技能全丢了"，而实际是测试之间互相污染。
_ORIG = {"skills": registry.SKILLS_DIR, "backup": ha.BACKUP_DIR, "db": db.DB_PATH}


def _restore() -> None:
    registry.SKILLS_DIR = _ORIG["skills"]
    registry._cache.update(stamp=None, items={})
    registry._index.update(stamp=None, post={})
    ha.BACKUP_DIR = _ORIG["backup"]
    db.DB_PATH = _ORIG["db"]

GOOD = """---
name: demo_skill
title: 演示技能
when: 用户说想看看演示技能能不能用
tools: [search_kb, draft_cards]
require_tools: [search_kb]
risk: read
output: 给出结论
---
## 步骤

1. 先 `search_kb` 查一下
2. 再总结成三句话
"""


def _fresh() -> None:
    """每个用例都换一份库与技能目录：保存/启停会写盘，共用目录会互相污染。"""
    p = _TMP / "kb.sqlite"
    if p.exists():
        p.unlink()
    db.DB_PATH = p
    db.init_db()

    if _SKILLS.exists():
        shutil.rmtree(_SKILLS)
    _SKILLS.mkdir()
    registry.SKILLS_DIR = _SKILLS
    registry._cache.update(stamp=None, items={})
    registry._index.update(stamp=None, post={})
    ha.BACKUP_DIR = _SKILLS / ".backup"


def _write(name: str, text: str) -> None:
    (_SKILLS / f"{name}.md").write_text(text, encoding="utf-8")
    registry.load_all(force=True)


def _codes(skill: str) -> set[str]:
    sk = registry.get(skill)
    assert sk is not None, skill
    return {i["code"] for i in ha.lint_skill(sk)}


def test_all_tools_match_impl():
    """工具目录必须和实现一一对应：schema 有、实现没有，模型调用必报未知工具。"""
    _fresh()
    tools, impl = ha.all_tools()
    names = {t["function"]["name"] for t in tools}
    assert names == set(impl), f"schema 与实现不一致：{names ^ set(impl)}"
    assert len(names) == 17
    assert "search_kb" in names and "draft_cards" in names


def test_tool_entries_reverse_index():
    """反查"改这个工具会影响哪些技能"——平台比目录多的就是这个。"""
    _fresh()
    _write("demo_skill", GOOD)
    entries = {t["name"]: t for t in ha.tool_entries()}
    assert entries["search_kb"]["read_only"] is True
    assert entries["draft_cards"]["read_only"] is False
    assert any(u["skill"] == "demo_skill" for u in entries["search_kb"]["used_by"])
    # require_tools 也要算引用，否则"只被验收要求的工具"会显示成孤儿
    assert any(u["via"] == "require" for u in entries["search_kb"]["used_by"])
    assert entries["remember"]["global_exempt"] is True


def test_clean_catalog_has_no_error():
    _fresh()
    _write("demo_skill", GOOD)
    res = ha.lint_all()
    assert res["counts"]["error"] == 0, res["issues"]
    assert res["score"] == 100


def test_lint_catches_typo_and_unreachable_contract():
    """两个真会发生的错：工具名拼错、验收要求一个白名单外的工具。"""
    _fresh()
    _write("bad_skill", GOOD
           .replace("name: demo_skill", "name: bad_skill")
           .replace("tools: [search_kb, draft_cards]", "tools: [search_kb, serch_kb]")
           .replace("require_tools: [search_kb]", "require_tools: [draft_cards]"))
    codes = _codes("bad_skill")
    assert "E_UNKNOWN_TOOL" in codes
    assert "E_REQUIRE_NOT_ALLOWED" in codes


def test_lint_catches_step_problems():
    _fresh()
    _write("demo_skill", GOOD)
    _write("combo_unknown", "---\nname: combo_unknown\ntitle: 坏组合\nwhen: 测试坏组合\n"
                            "steps:\n  - skill: not_exist\n---\n")
    _write("combo_nested", "---\nname: combo_nested\ntitle: 嵌套组合\nwhen: 测试嵌套组合\n"
                           "steps:\n  - skill: combo_unknown\n---\n")
    assert "E_STEP_UNKNOWN" in _codes("combo_unknown")
    assert "E_STEP_NESTED" in _codes("combo_nested")


def test_lint_catches_empty_body_and_missing_when():
    _fresh()
    _write("thin_skill", "---\nname: thin_skill\ntitle: 空技能\ntools: [search_kb]\n---\n")
    codes = _codes("thin_skill")
    assert "E_EMPTY_BODY" in codes
    assert "W_NO_WHEN" in codes


def test_save_rejects_broken_and_keeps_file_intact():
    """宁可保存失败，也不要把坏版本放进目录——坏版本的路由会静默失败。"""
    _fresh()
    _write("demo_skill", GOOD)
    before = (_SKILLS / "demo_skill.md").read_text(encoding="utf-8")

    res = ha.save_skill_text("demo_skill", GOOD.replace(
        "tools: [search_kb, draft_cards]", "tools: [search_kb, nope_tool]"))
    assert res["ok"] is False and res["saved"] is False
    assert any(e["code"] == "E_UNKNOWN_TOOL" for e in res["errors"])
    assert (_SKILLS / "demo_skill.md").read_text(encoding="utf-8") == before
    assert ha.backups("demo_skill") == []


def test_save_rejects_bad_name_and_mismatch():
    _fresh()
    _write("demo_skill", GOOD)
    for bad in ("../etc/passwd", "Demo", "x", ""):
        try:
            ha.save_skill_text(bad, GOOD)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝非法技能名：{bad!r}")

    try:
        ha.save_skill_text("other_name", GOOD)   # frontmatter 里还是 demo_skill
    except ValueError as e:
        assert "不一致" in str(e)
    else:
        raise AssertionError("name 与文件名不一致时应拒绝")


def test_save_ok_backs_up_and_reloads():
    _fresh()
    _write("demo_skill", GOOD)
    res = ha.save_skill_text("demo_skill", GOOD.replace("演示技能", "演示技能 v2"))
    assert res["ok"] and res["saved"] and res["created"] is False
    assert registry.get("demo_skill").title == "演示技能 v2"
    bs = ha.backups("demo_skill")
    assert len(bs) == 1, bs


def test_toggle_affects_routing_and_hint():
    """停用必须真的挡住路由——包括显式 hint，否则"停用"只是个标签。"""
    _fresh()
    _write("demo_skill", GOOD)
    sk, _ = registry.select("我想看看演示技能能不能用")
    assert sk is not None and sk.name == "demo_skill"

    ha.set_enabled("demo_skill", False)
    sk2, board = registry.select("我想看看演示技能能不能用")
    assert sk2 is None
    assert any(b["name"] == "demo_skill" and b.get("disabled") for b in board)
    assert registry.select("随便", hint="demo_skill")[0] is None
    assert "disabled: true" in (_SKILLS / "demo_skill.md").read_text(encoding="utf-8")

    ha.set_enabled("demo_skill", True)
    sk3, _ = registry.select("我想看看演示技能能不能用")
    assert sk3 is not None and sk3.name == "demo_skill"
    assert "disabled" not in (_SKILLS / "demo_skill.md").read_text(encoding="utf-8")


def test_route_preview_explains_why_not():
    _fresh()
    _write("demo_skill", GOOD)
    hit = ha.route_preview("我想看看演示技能能不能用")
    assert hit["picked"] == "demo_skill"
    assert "得分" in hit["reason"]

    miss = ha.route_preview("今天天气怎么样")
    assert miss["picked"] is None
    assert "词元" in miss["reason"] or "低于阈值" in miss["reason"]


def test_rollback_restores_previous_version():
    _fresh()
    _write("demo_skill", GOOD)
    ha.save_skill_text("demo_skill", GOOD.replace("演示技能", "演示技能 v2"))
    bs = ha.backups("demo_skill")
    res = ha.rollback("demo_skill", bs[0]["file"])
    assert res["ok"]
    assert "v2" not in registry.get("demo_skill").title
    # 回滚前把当前版本也备份了：回滚本身也要能回滚
    assert len(ha.backups("demo_skill")) == 2


def test_rollback_rejects_path_traversal():
    _fresh()
    _write("demo_skill", GOOD)
    for bad in ("../demo_skill.20260101-000000.md", "/etc/passwd", "demo_skill.md"):
        try:
            ha.rollback("demo_skill", bad)
        except (KeyError, ValueError):
            continue
        raise AssertionError(f"应拒绝非法备份名：{bad!r}")


def test_record_and_read_tool_calls():
    """台账要能分开"参数错"和"执行错"——两者的修法完全不同。"""
    _fresh()
    assert ha.recent_calls() == []

    dispatch.record_calls(
        [dispatch.ToolOutcome("c1", "search_kb", True, '{"ok":true}', latency_ms=12),
         dispatch.ToolOutcome("c2", "draft_cards", False, '{"ok":false}',
                              error="invalid_args", read_only=False),
         dispatch.ToolOutcome("c3", "search_kb", True, '{"ok":true}', latency_ms=30,
                              cached=True)],
        source="console", skill="demo_skill", session_id="s1", turn=2)

    rows = ha.recent_calls()
    assert len(rows) == 3
    by_id = {r["tool"]: r for r in rows if r["status"] == "ok"}
    assert by_id["search_kb"]["skill"] == "demo_skill"
    assert by_id["search_kb"]["turn"] == 2
    bad = [r for r in rows if r["status"] == "error"][0]
    assert bad["error_kind"] == "invalid_args"
    assert bad["read_only"] == 0

    st = ha.tool_stats()
    assert st["search_kb"]["calls"] == 2
    assert st["search_kb"]["ok_rate"] == 1.0


def test_composite_with_broken_step_is_not_selected():
    """缺段的组合技能不能被选中。

    实测踩到过：停用 gap_to_cards 后，diagnose_to_cards 仍被路由选中，
    但只展开出第 1 段——用户以为"诊断+补卡"都做了，实际只做了诊断。
    """
    _fresh()
    _write("demo_skill", GOOD)
    _write("child_b", GOOD.replace("name: demo_skill", "name: child_b")
           .replace("title: 演示技能", "title: 第二段技能")
           .replace("when: 用户说想看看演示技能能不能用", "when: 另一件完全不相干的事情"))
    _write("combo_ab", "---\nname: combo_ab\ntitle: 组合演示\n"
                       "when: 用户说想看看组合演示技能能不能用\nrisk: read\n"
                       "steps:\n  - skill: demo_skill\n  - skill: child_b\n---\n")

    sk, _ = registry.select("我想看看组合演示技能能不能用")
    assert sk is not None and sk.name == "combo_ab"

    ha.set_enabled("demo_skill", False)
    sk2, board = registry.select("我想看看组合演示技能能不能用")
    assert sk2 is None, "缺段的组合技能不该被选中"
    assert any(b["name"] == "combo_ab" and b.get("broken") for b in board)
    # 显式指定也要被拦住，并说清原因
    r = ha.route_preview("随便", hint="combo_ab")
    assert r["picked"] is None and "缺段" in r["reason"]


def test_overview_shape():
    _fresh()
    _write("demo_skill", GOOD)
    ov = ha.overview()
    assert ov["tools"]["total"] == 17
    assert ov["skills"]["total"] == 1
    assert ov["health"]["score"] == 100
    assert {r["role"] for r in ov["roles"]} == {"researcher", "analyst", "writer", "critic"}
    assert ov["thresholds"]["select_min"] == registry.SELECT_MIN


# ---------------------------------------------------------------- 版本管理

GOOD_V2 = GOOD.replace("2. 再总结成三句话", "2. 再总结成三句话\n3. 最后给出下一步")


def test_versions_seed_current_file_as_v1():
    """第一次看历史时，把当前文件记为 v1。

    不这么做的话，第一次保存会把改动记成 v1，改之前的样子就永远看不到了——
    而那恰恰是最需要对比的一份。
    """
    _fresh()
    _write("demo_skill", GOOD)
    vs = ha.versions("demo_skill")
    assert len(vs) == 1
    assert vs[0]["version"] == 1 and vs[0]["source"] == "import"
    assert ha.versions("demo_skill") == vs, "重复查看不应该重复记版本"


def test_save_records_version_with_message_and_keeps_previous_text():
    _fresh()
    _write("demo_skill", GOOD)
    ha.versions("demo_skill")                      # 先播种 v1
    res = ha.save_skill_text("demo_skill", GOOD_V2, message="补一条下一步")
    assert res["saved"] and res["version"] == 2 and res["unchanged"] is False

    vs = ha.versions("demo_skill")
    assert [(v["version"], v["source"]) for v in vs] == [(2, "save"), (1, "import")]
    assert vs[0]["message"] == "补一条下一步"
    assert vs[0]["chars"] > vs[1]["chars"]


def test_save_without_changes_does_not_add_version():
    """内容没变就不记。否则启停来回点会把历史刷满空版本，真正的改动被埋掉。"""
    _fresh()
    _write("demo_skill", GOOD)
    ha.versions("demo_skill")
    res = ha.save_skill_text("demo_skill", GOOD, message="什么都没改")
    assert res["saved"] is True and res["unchanged"] is True
    assert len(ha.versions("demo_skill")) == 1


def test_diff_reports_added_and_removed_rows():
    _fresh()
    _write("demo_skill", GOOD)
    ha.versions("demo_skill")
    ha.save_skill_text("demo_skill", GOOD_V2, message="补一条")
    d = ha.diff_versions("demo_skill", 1, 2)
    assert d["stats"]["added"] >= 1
    ops = {r["op"] for r in d["rows"]}
    assert "add" in ops
    added = [r for r in d["rows"] if r["op"] == "add"]
    assert any("下一步" in r["text"] for r in added)
    # 相同行在折叠后仍要保留上下文，否则看不出改动发生在哪
    assert any(r["op"] == "equal" for r in d["rows"])


def test_revert_creates_new_version_instead_of_rewinding():
    """回滚是新版本，不是改写历史：否则面板显示 v5、文件其实是 v2 的内容。"""
    _fresh()
    _write("demo_skill", GOOD)
    ha.versions("demo_skill")
    ha.save_skill_text("demo_skill", GOOD_V2, message="补一条")
    out = ha.revert_to("demo_skill", 1)
    assert out["ok"] and out["version"] == 3
    assert (_SKILLS / "demo_skill.md").read_text(encoding="utf-8") == GOOD
    vs = ha.versions("demo_skill")
    assert [v["version"] for v in vs] == [3, 2, 1]
    assert vs[0]["source"] == "revert"


def test_revert_rejects_unknown_version():
    _fresh()
    _write("demo_skill", GOOD)
    try:
        ha.revert_to("demo_skill", 99)
    except KeyError:
        pass
    else:
        raise AssertionError("不存在的版本应该报 KeyError")


def test_toggle_is_recorded_as_version():
    """启停也会改正文（写 disabled 标记），必须在历史里留痕。"""
    _fresh()
    _write("demo_skill", GOOD)
    ha.versions("demo_skill")
    ha.set_enabled("demo_skill", False)
    vs = ha.versions("demo_skill")
    assert vs[0]["source"] == "toggle" and "停用" in (vs[0]["message"] or "")
    assert ha.set_enabled("demo_skill", True)["enabled"] is True
    assert len(ha.versions("demo_skill")) == 3


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v()
            finally:
                _restore()
            print("ok", k)


try:  # pytest：每个用例后恢复，避免污染同批次其它测试文件
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate():
        yield
        _restore()
except ImportError:  # 直接 python 跑时没有 fixture，主流程里已包 finally
    pass
