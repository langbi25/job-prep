"""容量告警三条（体积 / 写锁 / 增速）。

和业务告警一样，重点是**什么时候不该报**：容量信号比业务指标更容易误报——
一条样本就能让比率变成 0% 或 100%，一次夜间长事务就能让 P95 冲顶。所以这里
逐个钉住样本量门槛、比率口径和冷却行为。
"""
import datetime

import pytest

from app.core import db
from app.observability import alerts
from app.services import capacity_service as cs

MB = 1024 * 1024


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("CAPACITY_DB_LIMIT_MB", "100")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")     # 默认不推，只落库
    monkeypatch.setenv("ALERT_LOCAL_COOLDOWN_MIN", "360")
    monkeypatch.setenv("ALERT_DB_SIZE_RATIO", "0.8")
    monkeypatch.setenv("ALERT_LOCK_MIN_SAMPLES", "3")
    monkeypatch.setenv("ALERT_LOCK_BLOCKED_RATE", "0.05")
    monkeypatch.setenv("ALERT_DB_ETA_DAYS", "30")
    monkeypatch.setenv("ALERT_GROWTH_MIN_SAMPLES", "20")
    db.init_db()
    yield
    db.close_thread_conns()


def _at(minutes_ago: float) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)
            ).isoformat(timespec="seconds")


def _sample(db_bytes: int, *, minutes_ago: float = 0, blocked: int = 0, wait: int = 0) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO capacity_samples "
        "(ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count) "
        "VALUES (?,?,0,?,?,0,0.5,8)",
        (_at(minutes_ago), db_bytes, wait, blocked))
    conn.commit()


def _rules(result: dict) -> list[str]:
    return [t["rule"] for t in result["triggered"]]


def _skipped_rules(result: dict) -> list[str]:
    return [s["rule"] for s in result["skipped"]]


def _alert_rows(rule: str) -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT ts, severity, message, delivered, error FROM alerts WHERE rule=? "
        "ORDER BY id", (rule,)).fetchall()
    return [dict(r) for r in rows]


# --- 库体积 -----------------------------------------------------------------

def test_db_size_alert_triggers_at_threshold(fresh_db):
    _sample(int(85 * MB))
    r = alerts.check_once()
    assert alerts.RULE_CAPACITY_DB in _rules(r)
    row = _alert_rows(alerts.RULE_CAPACITY_DB)[0]
    assert row["severity"] == "warning"
    assert "85.0 MB" in row["message"] and "100 MB" in row["message"]


def test_db_size_alert_promotes_to_critical_over_limit(fresh_db):
    _sample(int(105 * MB))
    r = alerts.check_once()
    assert alerts.RULE_CAPACITY_DB in _rules(r)
    row = _alert_rows(alerts.RULE_CAPACITY_DB)[0]
    assert row["severity"] == "critical"
    assert "已超上限" in row["message"]


def test_db_size_silent_below_ratio(fresh_db):
    _sample(int(50 * MB))
    assert alerts.RULE_CAPACITY_DB not in _rules(alerts.check_once())


def test_no_sample_means_no_alert(fresh_db):
    """一条样本都没有（worker 还没跑过）时不能报——这时体积是"未知"不是"正常"。"""
    r = alerts.check_once()
    assert r["capacity"]["n"] == 0
    assert alerts.RULE_CAPACITY_DB not in _rules(r)


# --- 写锁 -------------------------------------------------------------------

def test_write_lock_needs_min_samples(fresh_db):
    """两条样本里一条被挡 = 50%，但样本太少，不能据此喊狼。"""
    _sample(10 * MB, minutes_ago=10, blocked=1, wait=251)
    _sample(10 * MB, minutes_ago=5, blocked=0, wait=1)
    r = alerts.check_once()
    assert alerts.RULE_CAPACITY_LOCK not in _rules(r)


def test_write_lock_triggers_over_blocked_rate(fresh_db):
    for i, blocked in enumerate([1, 1, 0, 0]):
        _sample(10 * MB, minutes_ago=20 - i * 5, blocked=blocked, wait=251 if blocked else 1)
    r = alerts.check_once()
    assert alerts.RULE_CAPACITY_LOCK in _rules(r)
    msg = _alert_rows(alerts.RULE_CAPACITY_LOCK)[0]["message"]
    assert "50%" in msg and "P95" in msg


def test_write_lock_silent_when_clean(fresh_db):
    for i in range(4):
        _sample(10 * MB, minutes_ago=20 - i * 5, blocked=0, wait=2)
    assert alerts.RULE_CAPACITY_LOCK not in _rules(alerts.check_once())


def test_old_samples_outside_window_ignored(fresh_db):
    """挡住发生在两小时前、窗口是 30 分钟——现在不排队就不该报。"""
    for i in range(4):
        _sample(10 * MB, minutes_ago=120 + i, blocked=1, wait=251)
    assert alerts.RULE_CAPACITY_LOCK not in _rules(alerts.check_once())


# --- 增速 -------------------------------------------------------------------

def _growth_series(last_mb: int, per_day_mb: int, n: int = 20) -> None:
    """在 6 天里均匀铺 n 条样本，体积按 per_day_mb 线性增长。"""
    span_days = 6.0
    for i in range(n):
        days_ago = span_days * (n - 1 - i) / (n - 1)
        mb = last_mb - per_day_mb * days_ago
        _sample(int(mb * MB), minutes_ago=days_ago * 24 * 60)


def test_growth_alert_when_eta_within_threshold(fresh_db):
    _growth_series(last_mb=80, per_day_mb=5)      # (100-80)/5 = 4 天
    r = alerts.check_once()
    assert alerts.RULE_CAPACITY_GROWTH in _rules(r)
    assert "天到" in _alert_rows(alerts.RULE_CAPACITY_GROWTH)[0]["message"]


def test_growth_silent_when_slow(fresh_db):
    _growth_series(last_mb=30, per_day_mb=0.5)    # (100-30)/0.5 = 140 天
    assert alerts.RULE_CAPACITY_GROWTH not in _rules(alerts.check_once())


def test_growth_needs_enough_samples(fresh_db):
    """样本 3 条时斜率极不稳定，不许报增速——同"分布信号要够样本"一个道理。"""
    _growth_series(last_mb=80, per_day_mb=5, n=3)
    assert alerts.RULE_CAPACITY_GROWTH not in _rules(alerts.check_once())


# --- 冷却与投递 -------------------------------------------------------------

def test_cooldown_suppresses_second_alert(fresh_db):
    _sample(int(85 * MB))
    first = alerts.check_once()
    assert alerts.RULE_CAPACITY_DB in _rules(first)
    second = alerts.check_once()
    assert alerts.RULE_CAPACITY_DB not in _rules(second)
    assert alerts.RULE_CAPACITY_DB in _skipped_rules(second)
    assert len(_alert_rows(alerts.RULE_CAPACITY_DB)) == 1


def test_no_webhook_still_records_with_reason(fresh_db):
    """没配 webhook 也要落库：否则"规则在工作但推不出去"这件事完全不可见。"""
    _sample(int(85 * MB))
    alerts.check_once()
    row = _alert_rows(alerts.RULE_CAPACITY_DB)[0]
    assert row["delivered"] == 0
    assert "未配置" in (row["error"] or "")


def test_local_cooldown_is_longer_than_normal(fresh_db):
    cfg = alerts.config()
    assert cfg["enabled"] is False
    assert cfg["local_cooldown_min"] > cfg["cooldown_min"]


def test_check_once_reports_capacity_snapshot(fresh_db):
    _sample(int(50 * MB))
    cap = alerts.check_once()["capacity"]
    assert cap["n"] == 1 and cap["size_ratio"] == pytest.approx(0.5, abs=0.01)
    assert cap["db_limit_bytes"] == 100 * MB


# --- 调度 -------------------------------------------------------------------

def test_alerts_check_job_registered(fresh_db):
    from app.core import jobdefs, jobs
    jobdefs.register_default_jobs()
    assert "alerts_check" in jobs.registry()
    # 告警判定搬进 job 之后，执行历史还能回答"昨晚检查跑没跑"
    assert "capacity_sample" in jobs.registry() and "prune_spans" in jobs.registry()


def test_alerts_check_can_be_disabled(fresh_db, monkeypatch):
    """ALERT_CHECK_SEC=0 的语义要与单进程模式一致：关掉，而不是每 10 秒跑一次。"""
    from app.core import jobdefs, jobs
    jobs._REGISTRY.clear()
    monkeypatch.setenv("ALERT_CHECK_SEC", "0")
    jobdefs.register_default_jobs()
    assert "alerts_check" not in jobs.registry()
    # 关掉告警不该把别的任务也带走
    assert {"capacity_sample", "prune_spans"} <= set(jobs.registry())
