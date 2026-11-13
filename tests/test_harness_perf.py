"""Harness 性能护栏：防止"越管越慢"。

刻意不用时间断言。同一段代码在 CI 机器、在带杀软的 Windows、在 SSD 上差三倍，
写 `assert elapsed < 20ms` 只会得到两种结果：本地一直绿，换台机器乱红。

这里改数"干了多少次活"——它们才是慢的根本原因，而且与机器速度无关：
- 一次面板请求只该构建一次快照（旧版 overview 把同一份活干了两遍）；
- 整页数据只该开一个数据库连接（本机每次 get_conn() 是 8ms 量级）；
- 路由倒排索引跨请求复用，只在技能文件变化时重建。

这三条任何一条破掉，用户感知都是"点一下面板卡半秒"，但功能测试全绿。

跑法：pytest tests/test_harness_perf.py -q
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.services import harness_admin as ha  # noqa: E402
from app.skills import registry  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="harness-perf-"))
_SKILLS = _TMP / "skills"
_ORIG = {"skills": registry.SKILLS_DIR, "backup": ha.BACKUP_DIR, "db": db.DB_PATH}

SKILL = """---
name: {name}
title: {title}
when: 用户提到{topic}相关的事情需要处理
tools: [search_kb]
risk: read
output: 给出结论
---
## 步骤

1. 先查一下
2. 再总结
"""

TOPICS = ["面试复盘", "项目深挖", "算法刷题", "系统设计", "行为面", "简历修改",
          "薪资谈判", "八股文", "手写代码", "场景题"]


def _restore() -> None:
    registry.SKILLS_DIR = _ORIG["skills"]
    registry._cache.update(stamp=None, items={})
    registry._index.update(stamp=None, post={})
    ha.BACKUP_DIR = _ORIG["backup"]
    db.DB_PATH = _ORIG["db"]


def _fresh(n: int = 1) -> None:
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
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        (_SKILLS / f"skill_{i:02d}.md").write_text(
            SKILL.format(name=f"skill_{i:02d}", title=f"{topic}技能{i}", topic=topic),
            encoding="utf-8")
    registry.load_all(force=True)


def _count(monkeypatch, module, name: str) -> list[int]:
    """把某个函数包一层计数。返回的 list 会被原地累加，方便断言调用次数。"""
    hits = [0]
    orig = getattr(module, name)

    def wrapper(*a, **kw):
        hits[0] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(module, name, wrapper)
    return hits


def test_overview_builds_snapshot_once(monkeypatch):
    """overview 曾经 = 2×skill_entries + 2×tool_entries（它调 lint_all，lint_all 又重算一遍）。"""
    _fresh(5)
    se = _count(monkeypatch, ha, "skill_entries")
    te = _count(monkeypatch, ha, "tool_entries")
    ha.overview()
    assert se[0] == 1, f"skill_entries 被调了 {se[0]} 次，应该只构建一次快照"
    assert te[0] == 1, f"tool_entries 被调了 {te[0]} 次"


def test_board_opens_exactly_one_connection(monkeypatch):
    """整页 5 块数据（概览/工具/技能/体检/台账）共用一个连接。

    本机 connect+pragma+query+close 约 8ms；旧版这一页开 7 个连接，
    50ms 花在连数据库上，比解析技能本身还贵。
    """
    _fresh(5)
    conns = _count(monkeypatch, ha, "get_conn")
    out = ha.board()
    assert conns[0] == 1, f"开了 {conns[0]} 个连接，整页应该只开 1 个"
    assert out["lint"]["skills"] == 5 and out["overview"]["tools"]["total"] == 17


def test_router_index_is_reused_across_queries():
    """索引只在技能文件变化时重建。每轮对话都重建就等于没优化。"""
    _fresh(5)
    registry.select("我最近面试复盘总是做不好")      # 第一次允许重建
    base = registry._index_builds()
    for q in ("项目深挖讲不清楚", "算法刷题该怎么练", "系统设计要准备什么"):
        registry.select(q)
    assert registry._index_builds() == base, "技能没变，索引不该重建"


def test_router_index_rebuilds_when_skill_changes():
    _fresh(3)
    # query 必须够长：有效词元少于 4 个时 select 直接返回，根本不碰索引
    registry.select("我最近面试复盘总是做不好")
    base = registry._index_builds()
    (_SKILLS / "skill_00.md").write_text(
        SKILL.format(name="skill_00", title="改过的标题", topic="新话题"), encoding="utf-8")
    registry.load_all(force=True)
    registry.select("我最近面试复盘总是做不好")
    assert registry._index_builds() == base + 1, "技能改了，索引必须失效一次"


def test_route_preview_does_not_rebuild_doc_grams():
    """试跑命中词元直接查倒排表，不再为每个候选重建一遍 doc 的 gram 集合。"""
    _fresh(6)
    ha.route_preview("我最近面试复盘总是做不好")      # 预热
    base = registry._index_builds()
    for _ in range(3):
        r = ha.route_preview("项目深挖讲不清楚")
    assert registry._index_builds() == base
    assert r["n_tokens"] > 0


def test_lint_all_matches_snapshot():
    """lint_all 与 board.lint 必须是同一份计算，不能各算各的（否则面板与接口会不一致）。"""
    _fresh(4)
    a = ha.lint_all()
    b = ha.snapshot()["lint"]
    assert a["score"] == b["score"]
    assert a["counts"] == b["counts"]
    assert len(a["issues"]) == len(b["issues"])


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v(_MP()) if v.__code__.co_argcount else v()
            finally:
                _restore()
            print("ok", k)


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate():
        yield
        _restore()
except ImportError:
    pass
