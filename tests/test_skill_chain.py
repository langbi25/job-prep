"""技能嵌套（组合技能）测试（零 LLM、零网络）。

覆盖：普通技能单段展开、组合技能按序展开、非法子技能跳过、禁止二层嵌套、
全失败回退自身、段数上限、分段注入文案、require_tools 覆盖、白名单语义。

跑法：
    pytest tests/test_skill_chain.py -q
    python tests/test_skill_chain.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.skills import registry  # noqa: E402


def _skill(name: str, *, steps=(), tools=(), require=(), body="SOP") -> registry.Skill:
    return registry.Skill(name=name, title=f"{name} 标题", when="什么时候用",
                          body=body, tools=tuple(tools), require_tools=tuple(require),
                          steps=tuple(steps))


def _patch(monkeypatch_map: dict):
    """把技能表替换成测试用的小集合。"""
    registry.load_all()
    registry._cache["items"] = monkeypatch_map


def test_plain_skill_is_single_segment():
    _patch({"a": _skill("a")})
    segs = registry.expand(_skill("a"))
    assert len(segs) == 1 and segs[0].skill.name == "a"
    assert segs[0].require_tools is None


def test_combo_expands_in_order():
    _patch({
        "diag": _skill("diag", tools=["search_kb"]),
        "card": _skill("card", tools=["draft_cards"], require=["draft_cards"]),
        "combo": _skill("combo", steps=[
            {"skill": "diag", "goal": "先定位"},
            {"skill": "card", "goal": "再补卡"},
        ]),
    })
    segs = registry.expand(registry.get("combo"))
    assert [s.skill.name for s in segs] == ["diag", "card"]
    assert segs[0].goal == "先定位" and segs[1].goal == "再补卡"
    # 没写 require_tools 就走子技能自己的契约
    assert segs[1].require_tools is None
    assert segs[1].skill.require_tools == ("draft_cards",)


def test_combo_require_tools_override():
    _patch({
        "diag": _skill("diag"),
        "card": _skill("card", require=["ingest_retrospective"]),
        "combo": _skill("combo", steps=[
            {"skill": "diag"},
            {"skill": "card", "require_tools": ["draft_cards"]},
        ]),
    })
    segs = registry.expand(registry.get("combo"))
    assert segs[1].require_tools == ("draft_cards",)
    # 覆盖要真的生效：用 ingest_retrospective 的契约判就会失败，覆盖成本不该失败
    v = registry.check(segs[1].skill, ["draft_cards"], require_tools=segs[1].require_tools)
    assert v["ok"] and not v["missing"]


def test_missing_child_skipped():
    _patch({"diag": _skill("diag"),
            "combo": _skill("combo", steps=[{"skill": "diag"}, {"skill": "nope"}])})
    segs = registry.expand(registry.get("combo"))
    assert [s.skill.name for s in segs] == ["diag"]


def test_nested_combo_rejected():
    """只允许一层：组合技能引用组合技能会让段数失控，注入文案也讲不清。"""
    _patch({
        "leaf": _skill("leaf"),
        "inner": _skill("inner", steps=[{"skill": "leaf"}]),
        "outer": _skill("outer", steps=[{"skill": "inner"}, {"skill": "leaf"}]),
    })
    segs = registry.expand(registry.get("outer"))
    assert [s.skill.name for s in segs] == ["leaf"], "内层组合应被跳过，只留下叶子技能"


def test_all_children_invalid_falls_back_to_self():
    """展开失败不能返回空：否则这一轮既没有 SOP 也没有工具白名单，直接瞎跑。"""
    _patch({"combo": _skill("combo", steps=[{"skill": "ghost"}])})
    segs = registry.expand(registry.get("combo"))
    assert len(segs) == 1 and segs[0].skill.name == "combo"


def test_segment_cap():
    steps = [{"skill": f"s{i}"} for i in range(6)]
    _patch({**{f"s{i}": _skill(f"s{i}") for i in range(6)},
            "combo": _skill("combo", steps=steps)})
    segs = registry.expand(registry.get("combo"))
    assert len(segs) == registry.MAX_SEGMENTS


def test_bad_step_entries_ignored():
    _patch({"a": _skill("a"), "combo": _skill("combo", steps=["a", None, {"skill": "a"}])})
    segs = registry.expand(registry.get("combo"))
    assert [s.skill.name for s in segs] == ["a"], "非 dict 的 step 应被跳过，不能崩"


def test_describe_shows_position_and_goal():
    sk = _skill("a", tools=["search_kb"], require=["search_kb"])
    text = registry.describe(sk, position=(1, 2), goal="定位薄弱主题")
    assert "第 1/2 段" in text
    assert "定位薄弱主题" in text
    assert "还有后续段落" in text          # 非末段：告诉它别把整件事做完
    last = registry.describe(sk, position=(2, 2), goal="补卡")
    assert "最后一段" in last
    # 单段时不该出现分段文案
    single = registry.describe(sk)
    assert "第 1/1 段" not in single
    assert "最后一段" not in single


def test_allowed_tools_ignores_combo_tools_field():
    """组合技能自身不执行，它的 tools 字段不能把调用方白名单收空。"""
    combo = _skill("combo", tools=["only_one"], steps=[{"skill": "a"}])
    got = registry.allowed_tools(combo, ["x", "y"])
    assert got == ["x", "y"]


def test_catalog_exposes_steps():
    """面板要能显示"这个技能是组合的、包含哪几段"。"""
    _patch({"a": _skill("a"), "combo": _skill("combo", steps=[{"skill": "a", "goal": "g"}])})
    c = {s["name"]: s for s in registry.catalog()}
    assert c["combo"]["steps"] == [{"skill": "a", "goal": "g"}]
    assert "steps" not in c["a"]


def test_real_skills_load_from_disk():
    """真实技能文件必须能加载：组合技能引用的子技能要真实存在。"""
    registry.load_all(force=True)
    items = registry.load_all()
    assert "gap_to_cards" in items and "diagnose_to_cards" in items
    segs = registry.expand(items["diagnose_to_cards"])
    assert [s.skill.name for s in segs] == ["weakness_diagnosis", "gap_to_cards"]
    # 第二段的工具里必须有写工具（否则"补卡"根本写不了）
    assert "draft_cards" in segs[1].skill.tools
    # 第一段不能有写工具——这正是分段的理由
    assert "draft_cards" not in segs[0].skill.tools


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
