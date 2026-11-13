"""常驻备份循环：每天固定时刻跑一次 backup_db.py。

为什么用容器 sidecar 而不是 Windows 计划任务：
- 计划任务只在登录会话里可靠，且要把 docker 的绝对路径写死在 bat 里；
- sidecar 跟着 compose 一起起停，`restart: unless-stopped` 天然扛重启，
  备份频率与"服务是否在跑"一致——服务没跑就没有新数据，不备份也不丢东西。

启动即先跑一次：容器重建后立刻有一份新鲜快照，不用等到凌晨。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

AT_HOUR = int(os.getenv("BACKUP_AT_HOUR", "2"))
AT_MINUTE = int(os.getenv("BACKUP_AT_MINUTE", "0"))
KEEP = os.getenv("BACKUP_KEEP", "7")
HERE = Path(__file__).resolve().parent


def _next_run(now: datetime) -> datetime:
    target = now.replace(hour=AT_HOUR, minute=AT_MINUTE, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _run_once() -> int:
    cmd = [sys.executable, str(HERE / "backup_db.py"), "--keep", KEEP]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = (cp.stdout or "").strip() or (cp.stderr or "").strip()
    print(f"[{stamp}] rc={cp.returncode} {out}", flush=True)
    log = Path(os.getenv("BACKUP_DIR", "/backup")) / "backup.log"
    try:
        with log.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] sidecar rc={cp.returncode} {out}\n")
    except OSError as e:  # 只读挂载/权限问题不该让循环退出
        print(f"[{stamp}] log write failed: {e}", flush=True)
    return cp.returncode


def main() -> int:
    print(f"[backup-loop] started, daily at {AT_HOUR:02d}:{AT_MINUTE:02d}, keep {KEEP}", flush=True)
    _run_once()                      # 先来一份，别等第一晚
    while True:
        nxt = _next_run(datetime.now())
        wait = (nxt - datetime.now()).total_seconds()
        print(f"[backup-loop] next run {nxt:%Y-%m-%d %H:%M} (in {wait/3600:.1f}h)", flush=True)
        # 分段睡：单次 time.sleep 24h 在挂起/时钟跳变时会漂移
        while wait > 0:
            time.sleep(min(wait, 300))
            wait = (nxt - datetime.now()).total_seconds()
        _run_once()


if __name__ == "__main__":
    sys.exit(main())
