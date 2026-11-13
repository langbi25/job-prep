"""可观测告警测试。

告警的价值集中在"出事的那几分钟"，所以判定必须满足四个不变量：
1. 错误率按**顶层调用**算——异常沿 span 链向上传播会让一次失败标红多层，
   拿全部 span 算错误率等于把分母换掉，阈值形同虚设；
2. 冷却期内不重复触发，且冷却状态重启不丢（查表不查内存）；
3. 没送达也要落库（delivered=0 + error）——webhook 断了之后告警静默失败
   等于没有告警；
4. 未配置 webhook 时只落库不推送，判定本身照常工作（本地事实源原则）。
"""
import datetime
import json

import pytest

from app.core import db
from app.observability import alerts, tracing as tr


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    tr.close_span_conn()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()
    tr.close_span_conn()


@pytest.fixture()
def no_webhook(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")
    yield


def _seed_span(name: str, *, kind: str = "AGENT", status: str = "ok",
               duration_ms: int = 100, parent_id: str | None = None,
               start_ts: str | None = None) -> str:
    """直接往 spans 表插一行，绕过 tracing 的计时器——测试要的是可控的历史数据。"""
    sid = f"sp-{name}-{status}-{duration_ms}-{tr.uuid.uuid4().hex[:6]}"
    ts = start_ts or datetime.datetime.now().isoformat(timespec="milliseconds")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO spans (span_id,trace_id,parent_id,name,kind,session_id,turn,"
            "seq,start_ts,end_ts,duration_ms,status,error,attrs_json,events_json,created_at)"
            " VALUES (?,?,?,?,?,NULL,0,1,?,?,?,?,NULL,'{}','[]',?)",
            (sid, f"tr-{sid}", parent_id, name, kind, ts, ts, duration_ms, status, ts))
        conn.commit()
    finally:
        conn.close()
    return sid


def test_error_rate_counts_top_level_only(fresh_db, no_webhook):
    """一次失败会沿 span 链标红多层：1 次真实失败 = 3 个 error span。
    顶层口径下错误率必须是 1/2，而不是 3/4。"""
    root_ok = _seed_span("agent.ok")
    _seed_span("agent.bad", status="error", parent_id=None)
    _seed_span("llm.bad", kind="LLM", status="error", parent_id=root_ok)
    _seed_span("tool.bad", kind="TOOL", status="error", parent_id=root_ok)
    m = alerts.window_metrics(5)
    assert m["total"] == 2, "只有顶层（parent_id IS NULL）计入分母"
    assert m["errors"] == 1 and m["error_rate"] == 0.5


def test_error_rate_rule_triggers_and_records(fresh_db, no_webhook, monkeypatch):
    monkeypatch.setenv("ALERT_MIN_CALLS", "2")
    monkeypatch.setenv("ALERT_ERROR_RATE", "0.5")
    for i in range(2):
        _seed_span(f"agent.bad{i}", status="error")
    _seed_span("agent.ok")
    out = alerts.check_once()
    assert [t["rule"] for t in out["triggered"]] == ["error_rate"]
    assert out["triggered"][0]["delivered"] is False
    assert out["triggered"][0]["error"], "未配置 webhook 时落库不推送，且原因可读"
    conn = db.get_conn()
    try:
        row = dict(conn.execute("SELECT * FROM alerts WHERE rule='error_rate'").fetchone())
    finally:
        conn.close()
    assert row and row["delivered"] == 0
    assert json.loads(row["metrics"])["total"] == 3, "metrics 快照留判定现场"


def test_cooldown_suppresses_repeat(fresh_db, no_webhook, monkeypatch):
    monkeypatch.setenv("ALERT_MIN_CALLS", "2")
    for i in range(3):
        _seed_span(f"agent.bad{i}", status="error")
    first = alerts.check_once()
    assert len(first["triggered"]) == 1
    second = alerts.check_once()
    assert not second["triggered"], "冷却期内不得重复轰炸"
    assert second["skipped"] == [{"rule": "error_rate", "reason": "冷却期内"}]
    conn = db.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM alerts WHERE rule='error_rate'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "跳过的不落库，alerts 表只留真实触发"


def test_cooldown_survives_restart(fresh_db, no_webhook, monkeypatch):
    """冷却查表不查内存：换一个进程对象（清掉线程状态）后仍应被抑制。"""
    monkeypatch.setenv("ALERT_MIN_CALLS", "2")
    for i in range(3):
        _seed_span(f"agent.bad{i}", status="error")
    assert alerts.check_once()["triggered"]
    alerts._loop_stop.set()          # 模拟重启：只清内存态，不清表
    alerts._loop_thread = None
    assert alerts.check_once()["skipped"], "冷却状态在表里，重启不丢"


def test_slow_rule_triggers(fresh_db, no_webhook, monkeypatch):
    monkeypatch.setenv("ALERT_SLOW_COUNT", "2")
    monkeypatch.setenv("ALERT_SLOW_MS", "1000")
    _seed_span("agent.slow1", duration_ms=1500)
    _seed_span("agent.slow2", duration_ms=2000)
    _seed_span("agent.fast", duration_ms=100)
    out = alerts.check_once()
    assert [t["rule"] for t in out["triggered"]] == ["slow_calls"]
    msg = [r["message"] for r in _alert_rows() if r["rule"] == "slow_calls"][0]
    assert "慢调用" in msg


def test_old_spans_out_of_window_do_not_trigger(fresh_db, no_webhook, monkeypatch):
    monkeypatch.setenv("ALERT_MIN_CALLS", "1")
    old = (datetime.datetime.now() - datetime.timedelta(minutes=30)
           ).isoformat(timespec="milliseconds")
    _seed_span("agent.old", status="error", start_ts=old)
    out = alerts.check_once()
    assert not out["triggered"], "窗口外的历史错误不该触发现在的告警"
    assert out["metrics"]["total"] == 0


def _alert_rows() -> list[dict]:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM alerts")]
    finally:
        conn.close()


def test_delivery_failure_recorded(fresh_db, monkeypatch):
    """webhook 报 500 也要落库（delivered=0）——告警静默失败等于没有告警。"""
    monkeypatch.setenv("ALERT_MIN_CALLS", "1")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/xyz")
    _seed_span("agent.bad", status="error")

    class _Resp:
        status_code = 500
        text = "upstream exploded"

    sent = {}

    def _fake_post(self, url, json=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        return _Resp()

    monkeypatch.setattr(alerts.requests.Session, "post", _fake_post)
    out = alerts.check_once()
    assert out["triggered"][0]["delivered"] is False
    assert "500" in out["triggered"][0]["error"]
    assert sent["url"].startswith("https://hooks.example/")
    row = [r for r in _alert_rows() if r["rule"] == "error_rate"][0]
    assert row["delivered"] == 0 and row["error"]


def test_wecom_payload_format(fresh_db, monkeypatch):
    monkeypatch.setenv("ALERT_FORMAT", "wecom")
    payload = alerts._payload("测试", "wecom")
    assert payload["msgtype"] == "markdown" and "content" in payload["markdown"]
    payload = alerts._payload("测试", "dingtalk")
    assert payload["markdown"]["title"] and payload["markdown"]["text"] == "测试"
    assert alerts._payload("测试", "generic") == {"text": "测试"}


def test_history_masks_webhook(fresh_db, no_webhook):
    h = alerts.history(days=1)
    assert h["available"] is True and h["config"]["enabled"] is False
    assert "hooks.example" not in json.dumps(h["config"]), "webhook URL 是凭据，不能原样吐出"


def test_background_loop_off_without_webhook(fresh_db, no_webhook):
    assert alerts.start_background() is False
    assert alerts._loop_thread is None, "未配置 webhook 时零开销，不养空转线程"


def test_test_endpoint_records_row(fresh_db, no_webhook):
    out = alerts.send_test()
    assert out["delivered"] is False
    rows = [r for r in _alert_rows() if r["rule"] == "test"]
    assert rows, "测试告警也落库，链路验收有迹可循"
