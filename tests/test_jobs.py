"""job runner 测试：注册/到期/互斥/重试/超时/历史，全部不碰真实任务注册表。"""
import json
import time

import pytest

from app.core import db, jobs


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "kb.sqlite")
    monkeypatch.setattr(jobs.config, "DATA_DIR", tmp_path)  # 锁文件也别写进真实 data/
    db.init_db()
    db.close_thread_conns()
    yield tmp_path
    db.close_thread_conns()


@pytest.fixture(autouse=True)
def _clear_registry():
    """每个测试用独立注册表，防止污染/被污染真实注册表。"""
    saved = dict(jobs._REGISTRY)
    jobs._REGISTRY.clear()
    yield
    jobs._REGISTRY.clear()
    jobs._REGISTRY.update(saved)


def _register(name="t", interval=3600, timeout=5, retries=0, fn=None):
    jobs.job(name, interval_s=interval, timeout_s=timeout, retries=retries)(fn or (lambda: None))


# --- 注册与落库 ---------------------------------------------------------------

def test_sync_registry_writes_rows(fresh_db):
    _register("a", interval=60, timeout=10, retries=2)
    _register("b", interval=120)
    added = jobs.sync_registry()
    assert added == 2
    conn = db.get_conn()
    a = conn.execute("SELECT * FROM jobs WHERE name='a'").fetchone()
    assert a["interval_s"] == 60 and a["timeout_s"] == 10 and a["retries"] == 2
    assert a["enabled"] == 1 and a["last_ts"] is None


def test_sync_preserves_enabled_and_last(fresh_db):
    """运维关掉的任务重启后必须还是关着的：代码 sync 不回写 enabled。"""
    _register("a", interval=60)
    jobs.sync_registry()
    conn = db.get_conn()
    conn.execute("UPDATE jobs SET enabled=0, last_ts='2026-11-16T10:00:00' WHERE name='a'")
    conn.commit()
    jobs.sync_registry()  # 再同步一次（模拟重启）
    row = conn.execute("SELECT enabled, last_ts FROM jobs WHERE name='a'").fetchone()
    assert row["enabled"] == 0 and row["last_ts"] == "2026-11-16T10:00:00"


# --- 到期判断 ------------------------------------------------------------------

def test_due_jobs_never_run_is_due(fresh_db):
    _register("a", interval=3600)
    jobs.sync_registry()
    assert jobs.due_jobs() == ["a"]


def test_due_jobs_recent_run_not_due(fresh_db):
    _register("a", interval=3600)
    jobs.sync_registry()
    jobs.run_job("a")
    assert jobs.due_jobs() == []


def test_disabled_job_not_due_and_not_run(fresh_db):
    _register("a")
    jobs.sync_registry()
    conn = db.get_conn()
    conn.execute("UPDATE jobs SET enabled=0 WHERE name='a'")
    conn.commit()
    assert jobs.due_jobs() == []
    assert jobs.run_job("a")["status"] == "disabled"


# --- 执行与历史 ----------------------------------------------------------------

def test_run_success_records_history(fresh_db):
    calls = []
    _register("a", fn=lambda: calls.append(1))
    jobs.sync_registry()
    out = jobs.run_job("a")
    assert out["status"] == "success" and out["attempt"] == 1
    assert calls == [1]
    hist = jobs.runs("a")
    assert len(hist) == 1 and hist[0]["status"] == "success"
    assert hist[0]["duration_ms"] >= 0
    # last_status/last_ts 已更新
    row = db.get_conn().execute("SELECT last_status FROM jobs WHERE name='a'").fetchone()
    assert row["last_status"] == "success"


def test_run_failure_retries_then_succeeds(fresh_db):
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise RuntimeError("boom")

    _register("a", retries=2, fn=flaky)
    jobs.sync_registry()
    out = jobs.run_job("a")
    assert out["status"] == "success" and out["attempt"] == 2
    assert state["n"] == 2
    statuses = [r["status"] for r in jobs.runs("a")]
    assert statuses == ["success", "failed"]  # 倒序：最新的在前


def test_run_failure_exhausts_retries(fresh_db):
    _register("a", retries=1, fn=lambda: (_ for _ in ()).throw(ValueError("x")))
    jobs.sync_registry()
    out = jobs.run_job("a")
    assert out["status"] == "failed" and out["attempt"] == 2
    assert len(jobs.runs("a")) == 2


def test_timeout_marks_and_moves_on(fresh_db):
    def slow():
        time.sleep(30)

    _register("a", timeout=1, fn=slow)
    jobs.sync_registry()
    t0 = time.monotonic()
    out = jobs.run_job("a")
    assert out["status"] == "timeout"
    assert time.monotonic() - t0 < 10  # 不等 30s 任务体自然结束
    row = db.get_conn().execute("SELECT last_status FROM jobs WHERE name='a'").fetchone()
    assert row["last_status"] == "timeout"


def test_unregistered_job_raises(fresh_db):
    with pytest.raises(KeyError):
        jobs.run_job("ghost")


# --- 互斥 ----------------------------------------------------------------

def test_file_lock_exclusive(fresh_db):
    with jobs.file_lock("a") as first:
        assert first is True
        with jobs.file_lock("a") as second:
            assert second is False  # 同进程重入也拿不到（try-lock 语义）


def test_run_locked_job_records_skip(fresh_db):
    """另一实例持锁时：记 skipped_locked，不执行任务体。"""
    ran = []
    _register("a", fn=lambda: ran.append(1))
    jobs.sync_registry()
    with jobs.file_lock("a"):
        out = jobs.run_job("a")
    assert out["status"] == "skipped_locked"
    assert ran == []
    hist = jobs.runs("a")
    assert hist[0]["status"] == "skipped_locked"


def test_lock_released_after_run(fresh_db):
    _register("a")
    jobs.sync_registry()
    jobs.run_job("a")
    with jobs.file_lock("a") as got:  # 任务结束后锁必须已释放
        assert got is True


# --- 快照（API 数据源）----------------------------------------------------------

def test_snapshot_shape(fresh_db):
    _register("a", interval=60)
    jobs.sync_registry()
    jobs.run_job("a")
    snap = jobs.snapshot()
    assert [j["name"] for j in snap["jobs"]] == ["a"]
    assert snap["jobs"][0]["last_status"] == "success"
    assert snap["recent_runs"] and snap["recent_runs"][0]["job"] == "a"


def test_prune_runs_removes_old(fresh_db):
    _register("a")
    jobs.sync_registry()
    jobs.run_job("a")
    conn = db.get_conn()
    conn.execute("UPDATE job_runs SET ts='2020-01-01T00:00:00'")
    conn.commit()
    assert jobs.prune_runs(keep_days=30) >= 1
    assert jobs.runs(limit=10) == []
