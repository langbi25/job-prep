"""通用 job runner：表驱动任务 + 单机互斥 + 重试/超时 + 执行历史。

解决"单机部署没有调度器"的散装状态：span 清理只在启动时跑一次（注释自己
承认了），告警是专用线程，备份是独立进程——三种后台任务三种写法，新增一个
定时任务就要再发明一遍"到期判断 + 防重叠 + 失败可见"。

四个设计决定：

1. **代码注册 + 落库开关**。interval/timeout/retries 的真相源是代码（装饰器
   参数），`enabled` 开关落库——运维关掉的任务重启后必须还是关着的，写回
   代码里就成了"改配置要发版"。
2. **互斥用文件锁不用 DB 锁**。fcntl/msvcrt 的锁随进程死自动释放，不需要
   "锁过期时间"这种容易调错的魔法数；SQLite 没有原子的 try-lock 语义，
   自己在表里做状态机反而要处理"进程崩了谁清场"。
3. **超时用线程 join，不假装能杀**。Python 杀不掉线程，超时后任务体还在跑，
   runner 只是"不再等它并记 timeout"。真要硬中断得上进程池——单机部署
   任务都是清理/同步类幂等操作，泄漏一个跑到自然结束的线程可接受，
   这个限制写在文档里而不是藏着。
4. **调度循环单线程顺序跑**。到期任务一个个执行，不做并发——这些任务
   都在写同一个 SQLite，并发跑只会互相抢写锁；任务间隔以小时计，串行
   几秒钟的差距没有意义。
"""
from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from typing import Callable

from . import config
from .db import get_conn

# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict] = {}
_registry_lock = threading.Lock()


def job(name: str, *, interval_s: int, timeout_s: int = 120, retries: int = 0):
    """注册定时任务的装饰器。参数是代码侧默认值，jobs 表的 enabled 可覆盖启停。"""
    def deco(fn: Callable):
        with _registry_lock:
            _REGISTRY[name] = {"fn": fn, "interval_s": int(interval_s),
                               "timeout_s": int(timeout_s), "retries": int(retries)}
        return fn
    return deco


def registry() -> dict[str, dict]:
    with _registry_lock:
        return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# 落库：配置同步 + 执行历史
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def sync_registry() -> int:
    """把代码注册表 upsert 进 jobs 表。

    interval/timeout/retries 以代码为准（改代码即生效）；enabled/last_* 保留
    库里的值（运行时开关跨重启保持）。返回本次新登记的任务数。
    """
    conn = get_conn()
    added = 0
    for name, spec in registry().items():
        cur = conn.execute(
            "INSERT INTO jobs (name, interval_s, timeout_s, retries, enabled, updated_at) "
            "VALUES (?,?,?,?,1,?) ON CONFLICT(name) DO UPDATE SET "
            "interval_s=excluded.interval_s, timeout_s=excluded.timeout_s, "
            "retries=excluded.retries, updated_at=excluded.updated_at",
            (name, spec["interval_s"], spec["timeout_s"], spec["retries"], _now()))
        added += 1 if cur.rowcount == 1 else 0
    conn.commit()
    return added


def due_jobs() -> list[str]:
    """按 enabled + last_ts 计算到期任务。从没跑过的任务立即到期（首次部署即生效）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, interval_s, last_ts FROM jobs WHERE enabled = 1").fetchall()
    now = datetime.datetime.now()
    due = []
    for r in rows:
        if not r["last_ts"]:
            due.append(r["name"])
            continue
        try:
            last = datetime.datetime.fromisoformat(r["last_ts"])
        except ValueError:
            due.append(r["name"])
            continue
        if (now - last).total_seconds() >= r["interval_s"]:
            due.append(r["name"])
    return due


def _record_run(job_name: str, *, attempt: int, status: str,
                duration_ms: int, error: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO job_runs (job, ts, attempt, status, duration_ms, error) "
        "VALUES (?,?,?,?,?,?)",
        (job_name, _now(), attempt, status, duration_ms, error[:2000]))
    conn.execute(
        "UPDATE jobs SET last_status = ?, last_ts = ?, updated_at = ? WHERE name = ?",
        (status, _now(), _now(), job_name))
    conn.commit()


def runs(job_name: str | None = None, limit: int = 20) -> list[dict]:
    """执行历史，供 API 与排障读取。"""
    conn = get_conn()
    if job_name:
        rows = conn.execute(
            "SELECT job, ts, attempt, status, duration_ms, error FROM job_runs "
            "WHERE job = ? ORDER BY id DESC LIMIT ?", (job_name, int(limit))).fetchall()
    else:
        rows = conn.execute(
            "SELECT job, ts, attempt, status, duration_ms, error FROM job_runs "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def prune_runs(keep_days: int = 30) -> int:
    """执行历史只留 30 天——它是排障证据不是审计台账，占地方没收益。"""
    conn = get_conn()
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=int(keep_days))).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM job_runs WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# 单机互斥：文件锁
# ---------------------------------------------------------------------------

@contextmanager
def file_lock(name: str):
    """跨平台 try-lock。拿不到 yield False（另一实例在跑），拿到 yield True。

    fcntl/msvcrt 的锁绑定进程，进程崩溃自动释放——所以不需要"锁过期时间"。
    """
    lock_dir = config.DATA_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"job-{name}.lock"
    f = open(path, "a+b")
    acquired = False
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                f.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def run_job(job_name: str, *, force: bool = False) -> dict:
    """执行单个任务（带互斥/超时/重试），返回本次执行结果。

    force=True 供手动触发（API 按钮），跳过到期判断但不跳过互斥——
    手动触发撞上定时执行时宁可 skip 也不能双跑。
    """
    spec = registry().get(job_name)
    if spec is None:
        raise KeyError(f"未注册的任务: {job_name}")
    conn = get_conn()
    row = conn.execute(
        "SELECT enabled FROM jobs WHERE name = ?", (job_name,)).fetchone()
    if row and not row["enabled"] and not force:
        return {"job": job_name, "status": "disabled"}

    with file_lock(job_name) as got:
        if not got:
            _record_run(job_name, attempt=0, status="skipped_locked", duration_ms=0)
            return {"job": job_name, "status": "skipped_locked"}

        max_attempts = spec["retries"] + 1
        for attempt in range(1, max_attempts + 1):
            box: dict = {}

            def _target():
                t0 = time.perf_counter()
                try:
                    spec["fn"]()
                    box["ms"] = int((time.perf_counter() - t0) * 1000)
                    box["err"] = None
                except Exception as e:  # noqa: BLE001
                    box["ms"] = int((time.perf_counter() - t0) * 1000)
                    box["err"] = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"

            th = threading.Thread(target=_target, name=f"job-{job_name}", daemon=True)
            th.start()
            th.join(timeout=spec["timeout_s"])
            if th.is_alive():
                # 杀不掉线程（Python 限制）：记 timeout 并放手，任务体跑到自然结束。
                # 换进程池才能硬中断——清理类幂等任务不值得为它引入进程序列化。
                _record_run(job_name, attempt=attempt, status="timeout",
                            duration_ms=spec["timeout_s"] * 1000,
                            error=f"超过 timeout_s={spec['timeout_s']}")
                result = {"job": job_name, "status": "timeout", "attempt": attempt}
                break
            if box["err"] is None:
                _record_run(job_name, attempt=attempt, status="success",
                            duration_ms=box["ms"])
                result = {"job": job_name, "status": "success",
                          "duration_ms": box["ms"], "attempt": attempt}
                break
            _record_run(job_name, attempt=attempt, status="failed",
                        duration_ms=box["ms"], error=box["err"])
            result = {"job": job_name, "status": "failed",
                      "duration_ms": box["ms"], "attempt": attempt,
                      "error": box["err"].splitlines()[0] if box["err"] else ""}
        return result


def snapshot() -> dict:
    """注册表 + 最近执行，供 GET /api/obs/jobs。"""
    sync_registry()
    conn = get_conn()
    jobs = [dict(r) for r in conn.execute(
        "SELECT name, interval_s, timeout_s, retries, enabled, last_status, last_ts "
        "FROM jobs ORDER BY name").fetchall()]
    return {"jobs": jobs, "recent_runs": runs(limit=50)}


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------

_scan_thread: threading.Thread | None = None
_scan_stop = threading.Event()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def scan_interval() -> int:
    return max(_env_int("JOBS_SCAN_SEC", 30), 5)


def jobs_enabled() -> bool:
    return os.environ.get("JOBS_ENABLED", "1").strip() not in ("0", "false", "False")


def _scan_loop() -> None:
    while not _scan_stop.is_set():
        try:
            for name in due_jobs():
                if _scan_stop.is_set():
                    break
                try:
                    run_job(name)
                except Exception as e:  # noqa: BLE001  单任务崩不拖垮调度循环
                    print(f"[jobs] {name} crashed: {type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[jobs] scan failed: {type(e).__name__}: {e}")
        _scan_stop.wait(timeout=scan_interval())


def start_scheduler() -> bool:
    """启动调度线程。JOBS_ENABLED=0 时不启动（测试 / 调试用）。

    启动即扫一轮：从没跑过的任务立即执行——新任务上线不必等到第一个间隔。
    """
    global _scan_thread
    if not jobs_enabled():
        return False
    if _scan_thread is not None and _scan_thread.is_alive():
        return True
    sync_registry()
    _scan_stop.clear()
    _scan_thread = threading.Thread(target=_scan_loop, name="job-scheduler", daemon=True)
    _scan_thread.start()
    return True


def stop_scheduler() -> None:
    _scan_stop.set()
    if _scan_thread is not None:
        _scan_thread.join(timeout=scan_interval() + 10)
