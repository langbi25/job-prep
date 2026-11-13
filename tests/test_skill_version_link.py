"""技能运行 × 版本 联表测试。

要解决的是"改过 3 版，但看不出哪版更好"：skill_runs 原来只有技能名，没有版本号，
所有版本的表现被平均成一条线；tokens 也一直是 0（record 没传），面板上"技能成本"
永远显示 0 元——比显示贵更危险，会让人以为这些技能免费。

五个不变量：
1. 落运行记录时自动挂上该技能当前最高版本；
2. 技能没进版本管理时版本为 NULL，不编一个"第 0 版"；
3. 分版本统计把 NULL 单列，否则历史运行会凭空消失；
4. 版本面板把 runs / 成功率 / 耗时 / 费用挂到每个版本上；
5. 对话链路真的把 tokens 与费用写进了 skill_runs（不是只加了个字段）。
"""
import pytest

from app.core import db, pricing
from app.skills import registry


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


def _version(skill: str, ver: int, text: str = "正文") -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO skill_versions (skill,version,text,sha,message,source,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (skill, ver, text, f"sha{ver}", f"v{ver}", "test", f"2026-11-0{ver}T00:00:00"))
    conn.commit()


def _run_row(skill: str) -> dict:
    conn = db.get_conn()
    return dict(conn.execute(
        "SELECT * FROM skill_runs WHERE skill=? ORDER BY id DESC LIMIT 1", (skill,)).fetchone())


def test_baseline_versions_filled_once(fresh_db):
    """启动时给所有技能补 v1，且只补一次——否则每次重启都会刷一条无意义版本。"""
    from app.services import harness_admin

    first = harness_admin.ensure_baseline_versions()
    assert first["count"] == len(registry.load_all())

    second = harness_admin.ensure_baseline_versions()
    assert second["count"] == 0, "幂等：第二次不该再写"

    conn = db.get_conn()
    got = {r["skill"] for r in conn.execute("SELECT DISTINCT skill FROM skill_versions")}
    assert set(registry.load_all()) <= got
    # 补的基线是 v1，且内容与磁盘一致（不是空占位）
    row = conn.execute(
        "SELECT version, length(text) AS n, source FROM skill_versions WHERE skill='gap_to_cards'"
    ).fetchone()
    assert row["version"] == 1
    assert row["n"] > 0
    assert row["source"] == "import"


def test_record_attaches_latest_version(fresh_db):
    """没显式传版本时，自动挂当前最高版本——v3 存在就不该记成 v1。"""
    _version("gap_to_cards", 1)
    _version("gap_to_cards", 3)
    registry.record("gap_to_cards", session_id="s1", status="ok", steps=2,
                    tools_used=["a"], tokens=1234, cost_cny=0.0042)
    row = _run_row("gap_to_cards")
    assert row["skill_version"] == 3
    assert row["tokens"] == 1234
    assert abs(row["cost_cny"] - 0.0042) < 1e-9


def test_version_is_null_when_skill_not_versioned(fresh_db):
    """不在版本管理里就留 NULL。写 0 会让统计多出一条含义不明的"第 0 版"。"""
    registry.record("no_versions", session_id=None, status="ok", steps=1, tools_used=[])
    assert _run_row("no_versions")["skill_version"] is None


def test_version_stats_keeps_unversioned_bucket(fresh_db):
    """未登记的运行要单独成组，不能因为 GROUP BY 丢进某个版本里。"""
    _version("weakness_diagnosis", 1)
    for _ in range(2):
        registry.record("weakness_diagnosis", session_id="s", status="ok", steps=3,
                        tools_used=["get_daily_brief"], tokens=100, cost_cny=0.01)
    # 模拟版本管理接入之前的历史：直接写 NULL
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO skill_runs (skill,session_id,status,steps_done,tools_used,tokens,"
        "duration_ms,skill_version,cost_cny,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("weakness_diagnosis", "s", "ok", 3, "x", 50, 1000, None, 0.005,
         "2026-11-01T00:00:00"))
    conn.commit()

    rows = registry.version_stats("weakness_diagnosis")
    by_ver = {r["version"]: r for r in rows}
    assert by_ver[1]["runs"] == 2
    assert by_ver[None]["runs"] == 1, "历史运行不能消失"
    assert abs(by_ver[1]["cost_cny"] - 0.02) < 1e-6


def test_version_board_carries_runtime_performance(fresh_db):
    """版本面板要能回答"哪一版更好"——runs / 成功率 / 耗时 / 费用缺一不可。"""
    from app.services import harness_admin

    _version("gap_to_cards", 1)
    _version("gap_to_cards", 2)
    # v1 两次（一次失败），v2 一次
    registry.record("gap_to_cards", session_id="s", status="ok", steps=2, tools_used=[],
                    duration_ms=1000, tokens=100, cost_cny=0.01, version=1)
    registry.record("gap_to_cards", session_id="s", status="failed", steps=0, tools_used=[],
                    duration_ms=3000, tokens=100, cost_cny=0.02, version=1)
    registry.record("gap_to_cards", session_id="s", status="ok", steps=1, tools_used=[],
                    duration_ms=500, tokens=50, cost_cny=0.005, version=2)

    board = harness_admin.version_board("gap_to_cards")
    by_ver = {v["version"]: v for v in board["versions"]}
    assert by_ver[1]["runs"] == 2
    assert by_ver[1]["ok_rate"] == 0.5
    assert by_ver[1]["avg_ms"] == 2000
    assert by_ver[2]["runs"] == 1 and by_ver[2]["ok_rate"] == 1.0
    assert abs(by_ver[1]["cost_cny"] - 0.03) < 1e-6


def test_stats_sums_tokens_and_cost(fresh_db):
    """技能列表要显示"花了多少"，所以聚合查询必须带上 tokens/cost。"""
    _version("retro_to_cards", 1)
    registry.record("retro_to_cards", session_id="s", status="ok", steps=2, tools_used=[],
                    tokens=1000, cost_cny=0.01)
    registry.record("retro_to_cards", session_id="s", status="ok", steps=2, tools_used=[],
                    tokens=500, cost_cny=0.02)
    st = {r["skill"]: r for r in registry.stats()}
    assert st["retro_to_cards"]["tokens"] == 1500
    assert abs(st["retro_to_cards"]["cost_cny"] - 0.03) < 1e-6
    assert abs(st["retro_to_cards"]["avg_cost"] - 0.015) < 1e-6


def test_console_chat_records_tokens_into_skill_runs(fresh_db, monkeypatch):
    """端到端：对话链路跑完要把 tokens/cost 写进 skill_runs，而不只是返回给前端。

    这条是这次修复的核心——字段加了但没人填，等于没加。
    """
    from app.services import console_service

    _version("gap_to_cards", 1)
    sk = registry.load_all()["gap_to_cards"]

    monkeypatch.setattr(registry, "select", lambda msg, hint=None: (sk, []))
    monkeypatch.setattr(console_service, "_history", lambda sid: [])
    monkeypatch.setattr(console_service, "_save", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(console_service.assembler, "assemble", lambda *a, **k: type(
        "C", (), {"messages": [], "as_dict": staticmethod(lambda: {})})())
    monkeypatch.setattr(console_service.harness_admin, "all_tools", lambda: ([], {}))

    # 假 LLM：把"记账"这一步真的走一遍 pricing.record_usage，模拟咽喉埋点
    def fake_chat(system, user, tools=None, messages=None, temperature=0.1,
                  json_mode=True, on_delta=None):
        pricing.record_usage("deepseek-chat", prompt_tokens=800, completion_tokens=200,
                             scene="console:auto")
        return {"content": "查完了：25 张到期", "tool_calls": None}

    monkeypatch.setattr(console_service.llm, "_chat_json", fake_chat)

    res = console_service.chat("今天复习什么", session_id="s-x", mode="auto")
    assert res["reply"]

    row = _run_row("gap_to_cards")
    assert row["tokens"] == 1000, "1000 tokens 要落到台账"
    assert row["cost_cny"] > 0, "费用不能是 0——那会让面板看起来免费"
    assert row["skill_version"] == 1
