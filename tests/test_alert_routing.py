"""告警分级路由 + 静默窗口（2026-11-18）。

测试重点与容量告警一致：**什么时候不该报/不该推**。
- 分级错位（critical 走了 warning 通道）= 半夜叫不醒人，比没分级更糟；
- 静默窗口若把"没推"藏起来（只推不落库），早上没人知道夜里报过什么；
- 静默记录不占冷却（除了它自己写库形成的冷却）会让恢复后立刻轰炸。
"""
import datetime

import pytest

from app.core import db
from app.observability import alerts


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL_CRITICAL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL_WARNING", raising=False)
    monkeypatch.delenv("ALERT_QUIET_START", raising=False)
    monkeypatch.delenv("ALERT_QUIET_END", raising=False)
    db.init_db()
    yield
    db.close_thread_conns()


# --- 分级路由：URL 选择 --------------------------------------------------------

def test_route_critical_independent_url(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://generic")
    monkeypatch.setenv("ALERT_WEBHOOK_URL_CRITICAL", "http://pager")
    url, _ = alerts._route_url("critical")
    assert url == "http://pager"


def test_route_critical_falls_back_to_generic(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://generic")
    url, _ = alerts._route_url("critical")
    assert url == "http://generic"


def test_route_warning_independent_url(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://generic")
    monkeypatch.setenv("ALERT_WEBHOOK_URL_WARNING", "http://group-chat")
    url, _ = alerts._route_url("warning")
    assert url == "http://group-chat"
    url, _ = alerts._route_url("info")     # info/test 也走 warning 通道
    assert url == "http://group-chat"


def test_deliver_reports_channel_when_unconfigured(fresh_db):
    ok, err = alerts._deliver("msg", "critical")
    assert ok is False and "未配置" in (err or "")


def test_deliver_routes_by_severity(fresh_db, monkeypatch):
    """critical/warning 必须打到各自的 URL——分级错位等于半夜叫不醒人。"""
    seen = []

    def fake_post(url, payload, timeout=5):
        seen.append((url, payload))
        class R: status_code = 200; text = ""
        return R()

    monkeypatch.setenv("ALERT_WEBHOOK_URL_CRITICAL", "http://pager")
    monkeypatch.setenv("ALERT_WEBHOOK_URL_WARNING", "http://chat")
    monkeypatch.setattr(alerts, "_post", fake_post)
    ok1, _ = alerts._deliver("【job-prep 告警】错误率过高", "critical")
    ok2, _ = alerts._deliver("【job-prep 告警】慢调用", "warning")
    assert ok1 and ok2
    assert seen[0][0] == "http://pager" and seen[1][0] == "http://chat"
    # 通道在 payload 里也可见：generic 格式带 level，critical 前缀加强
    assert seen[0][1]["level"] == "critical"
    assert "CRITICAL" in seen[0][1]["text"]


def test_severity_map_defaults():
    assert alerts.RULE_SEVERITY[alerts.RULE_ERROR_RATE] == "critical"
    assert alerts.RULE_SEVERITY[alerts.RULE_SLOW] == "warning"
    assert len(alerts.RULE_SEVERITY) == 8   # 8 条规则每条都有分级


# --- 静默窗口：纯函数 -----------------------------------------------------------

def test_quiet_window_day_range():
    """9:00-18:00 之内静默、之外不静默。"""
    assert alerts.in_quiet_window(datetime.datetime(2026, 11, 18, 12, 0), "09:00", "18:00")
    assert not alerts.in_quiet_window(datetime.datetime(2026, 11, 18, 20, 0), "09:00", "18:00")


def test_quiet_window_crosses_midnight():
    """23:00-07:00：夜里 2 点静默，中午不静默，23:30 也静默。"""
    at2 = datetime.datetime(2026, 11, 18, 2, 0)
    at_noon = datetime.datetime(2026, 11, 18, 12, 0)
    at2330 = datetime.datetime(2026, 11, 18, 23, 30)
    assert alerts.in_quiet_window(at2, "23:00", "07:00")
    assert not alerts.in_quiet_window(at_noon, "23:00", "07:00")
    assert alerts.in_quiet_window(at2330, "23:00", "07:00")


def test_quiet_window_unconfigured_or_invalid():
    noon = datetime.datetime(2026, 11, 18, 12, 0)
    assert not alerts.in_quiet_window(noon, "", "")
    assert not alerts.in_quiet_window(noon, "09:00", "09:00")   # 两端相等=未配置
    assert not alerts.in_quiet_window(noon, "9点", "18:00")     # 解析失败按未配置
    assert not alerts.in_quiet_window(noon, "25:00", "07:00")   # 越界按未配置


# --- 静默窗口：判定链路 ----------------------------------------------------------

def test_quiet_window_suppresses_delivery_but_records(fresh_db, monkeypatch):
    """静默时段：告警照常落库（delivered=0 + 静默原因），绝不静默失败。"""
    monkeypatch.setenv("ALERT_QUIET_START", "00:00")
    monkeypatch.setenv("ALERT_QUIET_END", "23:59")
    monkeypatch.setenv("ALERT_DB_SIZE_RATIO", "0.0")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://generic")   # 配了也会被静默拦住
    posted = []
    monkeypatch.setattr(alerts, "_post",
                        lambda *a, **k: posted.append(a) or type("R", (), {"status_code": 200, "text": ""})())
    cs = __import__("app.services.capacity_service", fromlist=["x"])
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO capacity_samples (ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, "
        "rows_total, load1, cpu_count) VALUES (?, 90*1024*1024, 0, 0, 0, 0, 0.5, 8)",
        (datetime.datetime.now().isoformat(timespec="seconds"),))
    conn.commit()

    r = alerts.check_once()
    assert alerts.RULE_CAPACITY_DB in [t["rule"] for t in r["triggered"]]
    row = db.get_conn().execute(
        "SELECT delivered, error FROM alerts WHERE rule=?",
        (alerts.RULE_CAPACITY_DB,)).fetchone()
    assert row["delivered"] == 0 and "静默" in (row["error"] or "")
    assert posted == []          # 静默时段一个请求都不该发出去


def test_quiet_record_then_recovery_delivers(fresh_db, monkeypatch):
    """静默只落库也占冷却；冷却过后（模拟第二天）正常推送。"""
    monkeypatch.setenv("ALERT_QUIET_START", "00:00")
    monkeypatch.setenv("ALERT_QUIET_END", "23:59")
    monkeypatch.setenv("ALERT_DB_SIZE_RATIO", "0.0")
    monkeypatch.setenv("ALERT_COOLDOWN_MIN", "30")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://generic")
    monkeypatch.setattr(alerts, "_post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cs = __import__("app.services.capacity_service", fromlist=["x"])
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO capacity_samples (ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, "
        "rows_total, load1, cpu_count) VALUES (?, 90*1024*1024, 0, 0, 0, 0, 0.5, 8)",
        (datetime.datetime.now().isoformat(timespec="seconds"),))
    conn.commit()

    r1 = alerts.check_once()      # 静默：只落库
    assert r1["triggered"][0]["delivered"] is False
    r2 = alerts.check_once()      # 还在冷却（静默记录占冷却），跳过
    assert alerts.RULE_CAPACITY_DB in [s["rule"] for s in r2["skipped"]]
    # 把最后一条告警时间拨回 1 小时前 → 冷却已过，且已不在静默时段
    # 注意写法：UPDATE 和 commit 必须用同一个连接对象。链式写
    # `db.get_conn().execute(...)` 再 `db.get_conn().commit()` 会被连接池的
    # 借出检查回滚——第二次借出看到 in_transaction=True 就回滚上个事务
    # （防"忘 commit"的保护，2026-11-18 实测踩中）。
    conn2 = db.get_conn()
    conn2.execute(
        "UPDATE alerts SET ts=? WHERE rule=?",
        ((datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds"),
         alerts.RULE_CAPACITY_DB))
    conn2.commit()
    # 关掉静默（两端相等=未配置，确定性不依赖真实时钟），冷却已过 → 正常推送
    monkeypatch.setenv("ALERT_QUIET_START", "01:00")
    monkeypatch.setenv("ALERT_QUIET_END", "01:00")
    r3 = alerts.check_once()
    assert r3["triggered"][0]["delivered"] is True


def test_config_exposes_routing_and_severity(fresh_db, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL_CRITICAL", "http://pager")
    c = alerts.config()
    assert c["routes"]["critical"] == "独立"
    assert c["routes"]["warning"] == "未配置"
    assert c["severity_map"][alerts.RULE_ERROR_RATE] == "critical"
