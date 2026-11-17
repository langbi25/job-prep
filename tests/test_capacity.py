"""容量采样与扩容判定（阶段 C）。

这一层的价值全在"阈值判得对不对"：把数喂进 evaluate() 直接断言状态，
比跑一遍容器便宜得多，也把"什么情况下该换库"写成了可执行的规格。
"""
import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import observability
from app.core import db
from app.services import capacity_service as cs

LIMIT_100MB = 100 * 1024 * 1024


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


def _insert(ts: str, db_bytes: int, *, wait=0, blocked=0, rows=0, load1=None, cpu=8):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO capacity_samples "
        "(ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count) "
        "VALUES (?,?,0,?,?,?,?,?)", (ts, db_bytes, wait, blocked, rows, load1, cpu))
    conn.commit()


def _days_ago(d: float) -> str:
    return (datetime.datetime.now() - datetime.timedelta(days=d)).isoformat(timespec="seconds")


def _eval(**kw):
    """默认全健康的参数，用例只覆盖自己关心的那项。"""
    base = dict(db_bytes=10 * 1024 * 1024, limit_bytes=LIMIT_100MB,
                growth_bytes_per_day=1024 * 1024, eta_days=90,
                lock_wait_p95_ms=5, lock_blocked_rate=0.0, samples_n=100,
                api_replicas_n=1, load1=0.5, cpu_count=8, limited_ratio=0.01)
    base.update(kw)
    return cs.evaluate(**base)


def _by_key(ev, key):
    return next(t for t in ev["triggers"] if t["key"] == key)


# --- 写锁探测 ---------------------------------------------------------------

def test_probe_idle_is_fast(fresh_db):
    r = cs.probe_write_lock()
    assert r["blocked"] is False
    assert r["wait_ms"] < cs.PROBE_WAIT_MS


def test_probe_detects_held_write_lock(fresh_db):
    """另一个连接占住写锁时，探测必须报 blocked——这是"写事务长到影响别人"的信号。"""
    import sqlite3
    holder = sqlite3.connect(str(db.DB_PATH))
    holder.execute("BEGIN IMMEDIATE")
    try:
        r = cs.probe_write_lock(wait_ms=50)
    finally:
        holder.rollback()
        holder.close()
    assert r["blocked"] is True
    assert "locked" in (r.get("error") or "")


def test_probe_missing_db_does_not_create_file(tmp_path, monkeypatch):
    """库不存在时要跳过：connect 会顺手建一个空库，把问题从"没初始化"变成"库被清空"。"""
    p = tmp_path / "nope.sqlite"
    monkeypatch.setattr(db, "DB_PATH", str(p))
    r = cs.probe_write_lock()
    assert r["blocked"] is False and "不存在" in (r.get("error") or "")
    assert not p.exists()


# --- 采样 -------------------------------------------------------------------

def test_sample_persists_metrics(fresh_db):
    # 空库的 MAX(rowid) 全是 0——先写两行 job_runs，让"写入量代理"有东西可数
    conn = db.get_conn()
    for _ in range(2):
        conn.execute("INSERT INTO job_runs (job, ts, status) VALUES ('probe', ?, 'success')",
                     (_days_ago(0),))
    conn.commit()
    rec = cs.sample()
    assert rec["db_bytes"] > 0 and rec["cpu_count"] > 0
    row = db.get_conn().execute(
        "SELECT db_bytes, rows_total, lock_blocked FROM capacity_samples").fetchone()
    assert row["db_bytes"] == rec["db_bytes"]
    assert row["rows_total"] >= 2     # 热表 rowid 上界之和，写量代理


def test_sample_prunes_beyond_keep_days(fresh_db):
    _insert(_days_ago(cs.KEEP_DAYS + 10), 1024)
    cs.sample()
    n = db.get_conn().execute(
        "SELECT COUNT(*) FROM capacity_samples WHERE ts < ?",
        (_days_ago(cs.KEEP_DAYS + 1),)).fetchone()[0]
    assert n == 0


# --- 趋势 -------------------------------------------------------------------

def test_history_growth_and_eta(fresh_db, monkeypatch):
    monkeypatch.setenv("CAPACITY_DB_LIMIT_MB", "100")
    _insert(_days_ago(4), 1 * 1024 * 1024)
    _insert(_days_ago(2), 3 * 1024 * 1024)
    h = cs.history(7)
    assert h["n"] == 2
    # 两天涨 2MB → 1MB/天（跨度为 2 天，四舍五入误差容忍）
    assert 0.9 * 1024 * 1024 < h["growth_bytes_per_day"] < 1.1 * 1024 * 1024
    assert 90 < h["eta_days"] < 105          # (100-3)/1 ≈ 97 天


def test_history_flat_db_has_no_eta(fresh_db):
    _insert(_days_ago(4), 5 * 1024 * 1024)
    _insert(_days_ago(2), 5 * 1024 * 1024)
    h = cs.history(7)
    assert h["growth_bytes_per_day"] == 0
    assert h["eta_days"] is None       # 没有增长就不该编一个"永远到不了"的天数


def test_history_short_span_refuses_to_extrapolate(fresh_db):
    """两个点只差 5 分钟就不报日增速：除出来的数能差十倍，贴到面板上会被当成明天就爆。"""
    _insert(_days_ago(0.01), 1 * 1024 * 1024)
    _insert(_days_ago(0.005), 6 * 1024 * 1024)
    h = cs.history(7)
    assert h["growth_bytes_per_day"] is None


def test_history_blocked_rate_and_p95(fresh_db):
    # 18 条正常 + 2 条被挡：最近秩 P95 在 20 个样本里取第 19 个，正好落在离群点上。
    # 只放 1 条离群值的话 P95 会停在 5ms——这不是 bug，是分位数的定义，测试要跟着口径走。
    for i in range(18):
        _insert(_days_ago(1 + i / 100), 1024 * 1024, wait=5, blocked=0, rows=i)
    for i in range(2):
        _insert(_days_ago(0.5 + i / 100), 1024 * 1024, wait=300, blocked=1, rows=90 + i)
    h = cs.history(7)
    assert h["lock_blocked_rate"] == pytest.approx(0.1, abs=0.001)   # 2/20
    assert h["lock_wait_max_ms"] == 300
    assert h["lock_wait_p95_ms"] == 300


# --- 判定：Postgres ---------------------------------------------------------

def test_postgres_ok_by_default():
    ev = _eval()
    pg = _by_key(ev, "postgres")
    assert pg["state"] == "ok"
    assert ev["verdict"].startswith("全部未触发")


def test_postgres_triggered_by_size():
    pg = _by_key(_eval(db_bytes=LIMIT_100MB), "postgres")
    assert pg["state"] == "triggered" and "上限" in pg["why"]


def test_postgres_warn_at_70_percent():
    pg = _by_key(_eval(db_bytes=int(LIMIT_100MB * 0.75)), "postgres")
    assert pg["state"] == "warn"


def test_postgres_triggered_by_lock_p95():
    pg = _by_key(_eval(lock_wait_p95_ms=cs.LOCK_WAIT_P95_TRIGGER_MS + 1), "postgres")
    assert pg["state"] == "triggered" and "P95" in pg["why"]


def test_postgres_triggered_by_blocked_rate():
    pg = _by_key(_eval(lock_blocked_rate=0.02, samples_n=50), "postgres")
    assert pg["state"] == "triggered"


def test_postgres_blocked_rate_needs_enough_samples():
    """样本只有 5 条时说"2% 被挡"没有统计意义，最多算预警。"""
    pg = _by_key(_eval(lock_blocked_rate=0.4, samples_n=5), "postgres")
    assert pg["state"] == "warn"


def test_postgres_lock_p95_needs_enough_samples():
    """同上：两条样本里一条排队就宣布该换库，是拿噪声当结论。"""
    pg = _by_key(_eval(lock_wait_p95_ms=400, samples_n=3), "postgres")
    assert pg["state"] == "warn" and "样本" in pg["why"]


def test_postgres_reports_insufficient_samples():
    pg = _by_key(_eval(samples_n=3), "postgres")
    assert "趋势未成形" in pg["why"]


# --- 判定：副本 / CPU / 多机 -------------------------------------------------

def test_replicas_ok_single_instance():
    rp = _by_key(_eval(), "replicas")
    assert rp["state"] == "ok" and "单副本" in rp["why"]


def test_replicas_triggered_when_multi_instance():
    rp = _by_key(_eval(api_replicas_n=2), "replicas")
    assert rp["state"] == "triggered"
    assert "Redis" in rp["action"]      # 多副本的前置条件是闸门状态共享


def test_replicas_warn_only_after_postgres_triggered():
    """库到触发线时加副本只会放大单写者竞争——顺序必须是先换库。"""
    ev = _eval(db_bytes=LIMIT_100MB)
    assert _by_key(ev, "replicas")["state"] == "warn"


def test_cpu_ok_when_idle():
    assert _by_key(_eval(load1=0.5, cpu_count=8), "cpu")["state"] == "ok"


def test_cpu_warn_and_trigger():
    assert _by_key(_eval(load1=6.0, cpu_count=8), "cpu")["state"] == "warn"     # 0.75
    assert _by_key(_eval(load1=9.0, cpu_count=8), "cpu")["state"] == "triggered"


def test_cpu_manual_without_loadavg():
    assert _by_key(_eval(load1=None), "cpu")["state"] == "manual"


def test_k8s_always_manual():
    assert _by_key(_eval(), "k8s")["state"] == "manual"


# --- 汇总与接口 -------------------------------------------------------------

def test_report_shape(fresh_db):
    r = cs.report(days=7)
    assert r["sample"]["db_bytes"] > 0
    assert len(r["triggers"]) == 4
    assert set(r["limits"]) >= {"db_limit_mb", "sample_interval_s", "keep_days", "api_replicas"}
    assert r["history"]["n"] >= 1
    assert isinstance(r["verdict"], str) and r["verdict"]


def test_report_reuses_fresh_sample(fresh_db):
    """刚采过的样本不该被再次探测——面板每刷新一次就打一次写锁探测是浪费。"""
    first = cs.sample()
    r = cs.report()
    assert r["sample"]["ts"] == first["ts"]


def test_capacity_route(fresh_db):
    app = FastAPI()
    app.include_router(observability.router)
    d = TestClient(app).get("/api/obs/capacity?refresh=true").json()
    assert d["triggers"] and d["limits"]["sample_interval_s"] == cs.SAMPLE_INTERVAL_S


def test_capacity_job_registered(fresh_db):
    """采样必须是 job runner 里的一等任务：注册漏了，趋势就永远是空的。"""
    from app.core import jobdefs, jobs
    jobdefs.register_default_jobs()
    assert "capacity_sample" in jobs.registry()
    assert "prune_spans" in jobs.registry()      # 逐个判重，不能因为前者存在就把后者吞掉
