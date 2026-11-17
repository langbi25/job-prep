"""独立 worker 进程入口：`python -m app.worker`（compose 里 worker 服务用）。

阶段 A（进程职责分离）：调度循环从 api 进程搬进独立容器，api 变纯请求面。
职责边界从此清晰——api 只回答 HTTP，定时任务/后台清理在 worker 里跑，
backup 早就独立了，现在三种后台形态终于统一成"一个进程一种职责"。

两个跨进程机制（都依赖 /app/data 是共享卷）：

1. **文件锁互斥是跨容器的**。job-*.lock 落在 DATA_DIR（named volume），
   worker、api、backup 看到的是同一个文件——手动触发按钮（api）撞上
   定时执行（worker）时，后到的一方拿不到锁记 skipped_locked，不会双跑。
2. **SQLite 单写者靠 busy_timeout 排队**。WAL 模式同一时刻只允许一个写者，
   worker 写 job_runs、api 写业务表撞上时，靠连接层的 busy_timeout 等待
   而不是立刻抛 "database is locked"（app/core/db.py，默认 5000ms）。

单进程模式（本地 uvicorn 直跑）不受影响：JOBS_ENABLED 默认 1，api 里的
调度线程照旧；只有 compose 部署给 api 设 JOBS_ENABLED=0、由本进程接管。
"""
from __future__ import annotations

import signal
import threading


def main() -> None:
    from .core import jobdefs, jobs
    from .core.config import ensure_dirs
    from .core.db import init_db

    ensure_dirs()
    init_db()
    jobdefs.register_default_jobs()
    added = jobs.sync_registry()
    names = ", ".join(sorted(jobs.registry())) or "(无)"
    print(f"[worker] registry synced: {len(jobs.registry())} jobs（新增 {added}）: {names}")

    if not jobs.start_scheduler():
        # worker 容器里 JOBS_ENABLED=0 是配置矛盾：这个进程存在的唯一意义
        # 就是跑调度。宁可退出让 restart 策略之外的人看见，也不空转装忙。
        print("[worker] JOBS_ENABLED=0：worker 进程不应设此项，退出")
        return

    stop = threading.Event()

    def _bye(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    print(f"[worker] scheduler on: 每 {jobs.scan_interval()}s 扫一轮到期任务")
    stop.wait()
    jobs.stop_scheduler()
    print("[worker] stopped cleanly")


if __name__ == "__main__":
    main()
