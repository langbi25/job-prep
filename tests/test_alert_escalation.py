"""告警升级链 + 认领/关闭闭环（2026-11-18，规划-架构演进 §7 收尾项）。

分级路由解决"critical 走哪条通道"，这一层解决"推出去之后没人管怎么办"：
- 升级链错的部分：对已认领/已关闭的告警继续催 = 骚扰处理过的人；
  对 warning 也升级 = 天亮处理的事半夜炸群；
- 闭环缺的那半：没有 ack/resolve，升级链不知道什么时候该停，只能无限复读。
"""
import datetime

import pytest

from app.core import db
from app.observability import alerts


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    for k in ("ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_URL_CRITICAL",
              "ALERT_WEBHOOK_URL_WARNING", "ALERT_QUIET_START", "ALERT_QUIET_END",
              "ALERT_ESCALATE_MIN", "ALERT_ESCALATE_REPEAT_MIN", "ALERT_ESCALATE_MAX"):
        monkeypatch.delenv(k, raising=False)
    db.init_db()
    yield
    db.close_thread_conns()


def _insert_alert(rule="error_rate", severity="critical", age_min=20,
                  ack=False, resolved=False, escalations=0, last_escalate_min=None,
                  ts=None):
    """直接造一条历史告警行（绕过 _fire 的冷却/静默链路）。"""
    now = datetime.datetime.now()
    when = ts or (now - datetime.timedelta(minutes=age_min))
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO alerts (ts, rule, severity, message, metrics, delivered, "
            "ack_ts, resolved_ts, escalations, last_escalate_ts) "
            "VALUES (?,?,?,?,?,1,?,?,?,?)",
            (when.isoformat(timespec="milliseconds"), rule, severity, "测试告警", "{}",
             when.isoformat() if ack else None,
             when.isoformat() if resolved else None,
             escalations,
             (now - datetime.timedelta(minutes=last_escalate_min)).isoformat()
             if last_escalate_min is not None else None))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# --- 升级判定：纯函数边界 ------------------------------------------------------

def test_needs_escalate_reacts_only_after_min():
    """不满 escalate_min 不催——给值班的人留反应时间。"""
    assert not alerts._needs_escalate(14.9, 0, None, 15, 30, 3)
    assert alerts._needs_escalate(15, 0, None, 15, 30, 3)


def test_needs_escalate_stops_at_max():
    """升满 max 次就停——升级链不是无限复读机，之后该修的是值班安排。"""
    assert not alerts._needs_escalate(120, 3, 60, 15, 30, 3)
    assert alerts._needs_escalate(120, 2, 60, 15, 30, 3)


def test_needs_escalate_respects_repeat_window():
    """距上次升级不满 repeat_min 不重复催（判定周期才 60s，没有这层就是每分钟一条）。"""
    assert not alerts._needs_escalate(60, 1, 10, 15, 30, 3)
    assert alerts._needs_escalate(60, 1, 30, 15, 30, 3)


# --- 认领 / 关闭 ---------------------------------------------------------------

def test_ack_sets_ts_and_by(fresh_db):
    aid = _insert_alert()
    r = alerts.ack_alert(aid, "duty-zhang")
    assert r["ok"] and not r["already"]
    row = db.get_conn().execute(
        "SELECT ack_ts, ack_by FROM alerts WHERE id=?", (aid,)).fetchone()
    assert row["ack_by"] == "duty-zhang" and row["ack_ts"]


def test_ack_is_idempotent_and_keeps_first_owner(fresh_db):
    """重复认领不覆盖原认领人——'谁先接手的'是事实，不该被后来者改写。"""
    aid = _insert_alert()
    alerts.ack_alert(aid, "first")
    r = alerts.ack_alert(aid, "second")
    assert r["already"] and r["ack_by"] == "first"
    assert db.get_conn().execute(
        "SELECT ack_by FROM alerts WHERE id=?", (aid,)).fetchone()["ack_by"] == "first"


def test_resolve_implies_ack(fresh_db):
    """关闭隐含认领：处置过的人就是认领过的人，升级链两个开关都要停。"""
    aid = _insert_alert()
    alerts.resolve_alert(aid, "duty-li")
    row = db.get_conn().execute(
        "SELECT ack_ts, ack_by, resolved_ts FROM alerts WHERE id=?", (aid,)).fetchone()
    assert row["resolved_ts"] and row["ack_ts"] and row["ack_by"] == "duty-li"


def test_resolve_keeps_existing_ack(fresh_db):
    aid = _insert_alert()
    alerts.ack_alert(aid, "first")
    alerts.resolve_alert(aid, "second")
    assert db.get_conn().execute(
        "SELECT ack_by FROM alerts WHERE id=?", (aid,)).fetchone()["ack_by"] == "first"


def test_ack_unknown_id_returns_not_ok(fresh_db):
    """必须挂 fresh_db：不隔离的话会查到真实开发库——2026-11-18 的 DB_PATH 老坑。"""
    assert alerts.ack_alert(9999)["ok"] is False
    assert alerts.resolve_alert(9999)["ok"] is False


def test_open_alerts_lists_only_unhandled(fresh_db):
    a_open = _insert_alert(rule="error_rate")
    _insert_alert(rule="error_rate", ack=True)          # 已认领
    _insert_alert(rule="capacity_db_size", resolved=True)  # 已关闭
    _insert_alert(rule="test", severity="info")          # 链路测试不算欠账
    r = alerts.open_alerts()
    assert r["total"] == 1 and r["items"][0]["id"] == a_open


# --- 升级扫描 ------------------------------------------------------------------

def test_sweep_escalates_unacked_critical(fresh_db, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL_CRITICAL", "http://pager")
    posted = []
    monkeypatch.setattr(alerts, "_post",
                        lambda url, payload, timeout=5: posted.append((url, payload))
                        or type("R", (), {"status_code": 200, "text": ""})())
    aid = _insert_alert(age_min=20)
    out = alerts.escalation_sweep()
    assert len(out) == 1 and out[0]["id"] == aid and out[0]["n"] == 1
    assert out[0]["delivered"]
    assert posted[0][0] == "http://pager"          # 升级走 critical 通道
    assert "升级" in posted[0][1]["text"] and "无人认领" in posted[0][1]["text"]
    row = db.get_conn().execute(
        "SELECT escalations, last_escalate_ts, escalate_error FROM alerts WHERE id=?",
        (aid,)).fetchone()
    assert row["escalations"] == 1 and row["last_escalate_ts"] and row["escalate_error"] is None


def test_sweep_records_delivery_failure(fresh_db, monkeypatch):
    """升级推送失败也留痕——'升级链自己哑了'必须可发现，与主推送同一纪律。"""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://dead")

    def boom(url, payload, timeout=5):
        raise ConnectionError("refused")

    monkeypatch.setattr(alerts, "_post", boom)
    aid = _insert_alert()
    out = alerts.escalation_sweep()
    assert out[0]["delivered"] is False and "ConnectionError" in out[0]["error"]
    assert db.get_conn().execute(
        "SELECT escalate_error FROM alerts WHERE id=?", (aid,)).fetchone()["escalate_error"]


def test_sweep_skips_warning(fresh_db):
    """warning 是'天亮处理'级别，为它配半夜催办是告警疲劳的开端。"""
    _insert_alert(rule="slow_calls", severity="warning", age_min=60)
    assert alerts.escalation_sweep() == []


def test_sweep_skips_acked_and_resolved(fresh_db):
    _insert_alert(ack=True, age_min=60)
    _insert_alert(resolved=True, age_min=60)
    assert alerts.escalation_sweep() == []


def test_sweep_respects_max(fresh_db):
    _insert_alert(age_min=120, escalations=3)
    assert alerts.escalation_sweep() == []


def test_sweep_respects_repeat_window(fresh_db):
    """刚升级过 5 分钟，repeat 窗口（默认 30）内不再催。"""
    _insert_alert(age_min=60, escalations=1, last_escalate_min=5)
    assert alerts.escalation_sweep() == []


def test_sweep_re_escalates_after_repeat_window(fresh_db):
    _insert_alert(age_min=120, escalations=1, last_escalate_min=35)
    out = alerts.escalation_sweep()
    assert out and out[0]["n"] == 2


def test_sweep_only_latest_per_rule(fresh_db):
    """同规则冷却后会有第二条行：只升级最新一条，旧影子副本不跟着刷屏。"""
    old_id = _insert_alert(age_min=90)
    new_id = _insert_alert(age_min=20)
    out = alerts.escalation_sweep()
    assert [o["id"] for o in out] == [new_id]
    assert db.get_conn().execute(
        "SELECT escalations FROM alerts WHERE id=?", (old_id,)).fetchone()["escalations"] == 0


def test_sweep_disabled_by_env(fresh_db, monkeypatch):
    """ALERT_ESCALATE_MIN=0 整体关闭（与 ALERT_CHECK_SEC=0 同款语义）。"""
    monkeypatch.setenv("ALERT_ESCALATE_MIN", "0")
    _insert_alert(age_min=600)
    assert alerts.escalation_sweep() == []


def test_sweep_ignores_stale_rows(fresh_db):
    """24h 之前的 critical 不再升级——要么处理了要么已经死了，催 ancient history 没有意义。"""
    _insert_alert(age_min=60 * 25)
    assert alerts.escalation_sweep() == []


# --- 与 check_once / history 的集成 ---------------------------------------------

def test_check_once_includes_escalation_sweep(fresh_db, monkeypatch):
    """升级扫描挂在 check_once 末尾：和判定共享'同一时刻只在一个进程跑'的语义。"""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://generic")
    monkeypatch.setattr(alerts, "_post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    _insert_alert(age_min=20)
    r = alerts.check_once()
    assert [e["rule"] for e in r["escalated"]] == ["error_rate"]


def test_history_exposes_lifecycle_fields(fresh_db):
    aid = _insert_alert(ack=True, escalations=2)
    items = alerts.history(days=1)["items"]
    it = next(x for x in items if x["id"] == aid)
    assert it["ack_ts"] and it["escalations"] == 2


def test_config_exposes_escalation(fresh_db, monkeypatch):
    c = alerts.config()
    assert c["escalate_min"] == 15 and c["escalate_repeat_min"] == 30 and c["escalate_max"] == 3
    monkeypatch.setenv("ALERT_ESCALATE_MIN", "0")
    assert alerts.config()["escalate_min"] == 0
