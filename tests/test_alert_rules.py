"""三条新告警规则（成本 / 工具失败 / 编排失败）。

这三条与错误率、慢调用的关键差别：它们的数据源不是 spans，而是三张业务台账
（llm_usage / tool_calls / agent_plans）。测的重点不是"能不能报"，而是
**什么时候不该报**——告警太吵比没有告警更快被无视：
- 成本：绝对下限（¥0.5）没到就不报，不然调试期几分钱的波动会天天响；
- 工具：样本不足 5 次不报，否则"调了 2 次错 2 次"就是 100% 失败率；
- 编排：capped / timeout 不算失败，它们是预算与时间的问题。
"""
import datetime
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.observability import alerts  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="alert-rules-"))


@pytest.fixture()
def fresh_db():
    db.close_thread_conns()
    db.DB_PATH = _TMP / f"kb-{time.time_ns()}.sqlite"
    db.init_db()
    yield
    db.close_thread_conns()


@pytest.fixture(autouse=True)
def no_webhook(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")
    # 阈值固定，避免 .env 或环境变量把测试变成看运气
    monkeypatch.setenv("ALERT_EMPTY_OK", "1")
    yield


def _day(offset: int = 0) -> str:
    return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()


def _spend(day: str, cost: float, scene: str = "console") -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO llm_usage (ts, model, scene, prompt_tokens, completion_tokens, "
        "cost_cny, latency_ms, status) VALUES (?,?,?,?,?,?,?,?)",
        (f"{day}T10:00:00", "deepseek-chat", scene, 100, 50, cost, 800, "ok"))
    conn.commit()


def _tool_call(status: str = "ok", tool: str = "search_kb") -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO tool_calls (ts, tool, source, skill, status, error_kind, "
        "latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (datetime.datetime.now().isoformat(timespec="seconds"), tool, "console", "-",
         status, None if status == "ok" else "failed", 40,
         datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def _plan(status: str) -> str:
    pid = f"plan-{time.time_ns()}"
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO agent_plans (id, goal, plan_json, status, steps_json, result_json, "
        "tokens_used, cost_cny, duration_ms, mode, resume_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "目标", "[]", status, json.dumps([]), "{}", 100, 0.02, 10000,
         "assist", 0, datetime.datetime.now().isoformat(timespec="seconds"),
         datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return pid


def _rules(out: dict) -> list[str]:
    return [t["rule"] for t in out["triggered"]]


# ------------------------------------------------------------------ 成本

def test_cost_spike_triggers_on_multiple_of_daily_average(fresh_db):
    for d in (1, 2, 3):
        _spend(_day(d), 0.10)          # 日均 ¥0.10
    _spend(_day(0), 0.60)              # 今天 ¥0.60 = 6 倍

    out = alerts.check_once()
    assert alerts.RULE_COST in _rules(out)
    assert out["cost"]["spike_ratio"] == 6.0
    msg = next(t for t in out["triggered"] if t["rule"] == alerts.RULE_COST)
    assert msg["delivered"] is False and msg["error"]


def test_cost_ignores_small_absolute_amounts(fresh_db):
    """几分钱的波动不该报警：比例再高也没意义。"""
    for d in (1, 2, 3):
        _spend(_day(d), 0.01)
    _spend(_day(0), 0.20)              # 20 倍，但只有 2 毛

    out = alerts.check_once()
    assert alerts.RULE_COST not in _rules(out)
    assert out["cost"]["spike_ratio"] == 20.0        # 算出来了，只是不报


def test_cost_triggers_near_daily_budget(fresh_db):
    # 日均也很高（¥4/天），spike 不成立，但今天已经 ¥4.2 / 预算 ¥5 = 84%
    for d in (1, 2, 3):
        _spend(_day(d), 4.0)
    _spend(_day(0), 4.2)

    out = alerts.check_once()
    assert alerts.RULE_COST in _rules(out)
    assert out["cost"]["budget_ratio"] >= 0.8


def test_cost_cooldown(fresh_db):
    for d in (1, 2, 3):
        _spend(_day(d), 0.10)
    _spend(_day(0), 1.0)

    assert alerts.RULE_COST in _rules(alerts.check_once())
    second = alerts.check_once()
    assert alerts.RULE_COST not in _rules(second)
    assert {"rule": alerts.RULE_COST, "reason": "冷却期内"} in second["skipped"]


# ------------------------------------------------------------------ 工具

def test_tool_failure_triggers(fresh_db):
    for _ in range(3):
        _tool_call("ok")
    for _ in range(3):
        _tool_call("error")

    out = alerts.check_once()
    assert alerts.RULE_TOOL_FAILURE in _rules(out)
    assert out["tools"]["error_rate"] == 0.5
    assert out["tools"]["top_tools"][0]["tool"] == "search_kb"


def test_tool_failure_ignores_policy_denials(fresh_db):
    """只读模式拒绝写工具是策略生效，不是故障——否则每天都会误报。"""
    conn = db.get_conn()
    for _ in range(3):
        _tool_call("ok")
    for _ in range(3):
        conn.execute(
            "INSERT INTO tool_calls (ts, tool, source, skill, status, error_kind, "
            "latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"), "remember",
             "console", "-", "error", "policy_denied", 0,
             datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()

    out = alerts.check_once()
    assert alerts.RULE_TOOL_FAILURE not in _rules(out)
    assert out["tools"]["denied"] == 3
    assert out["tools"]["errors"] == 0
    assert out["tools"]["error_rate"] == 0.0


def test_tool_failure_needs_min_samples(fresh_db):
    """2 次全错 = 100%，但样本不足，不该报——否则单次调试就会被当成故障。"""
    _tool_call("error")
    _tool_call("error")
    out = alerts.check_once()
    assert alerts.RULE_TOOL_FAILURE not in _rules(out)
    assert out["tools"]["error_rate"] == 1.0


# ------------------------------------------------------------------ 编排

def test_plan_failure_triggers(fresh_db):
    _plan("failed")
    _plan("partial")
    _plan("done")

    out = alerts.check_once()
    assert alerts.RULE_PLAN_FAILURE in _rules(out)
    assert out["plans"]["failed"] == 2
    assert out["plans"]["failure_rate"] == 0.6667


def test_plan_soft_stops_are_not_failures(fresh_db):
    """capped/timeout 是"钱/时间不够"，修法完全不同，不能算进失败率。"""
    _plan("capped")
    _plan("timeout")
    _plan("done")

    out = alerts.check_once()
    assert alerts.RULE_PLAN_FAILURE not in _rules(out)
    assert out["plans"]["soft_stopped"] == 2
    assert out["plans"]["failed"] == 0


def test_plan_failure_needs_min_samples(fresh_db):
    _plan("failed")
    out = alerts.check_once()
    assert alerts.RULE_PLAN_FAILURE not in _rules(out)
    assert out["plans"]["total"] == 1
