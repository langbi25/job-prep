"""常驻备份循环：三层快照调度（micro 每 5 分钟 / hourly 每小时 / daily 每日）。

为什么用容器 sidecar 而不是 Windows 计划任务：
- 计划任务只在登录会话里可靠，且要把 docker 的绝对路径写死在 bat 里；
- sidecar 跟着 compose 一起起停，`restart: unless-stopped` 天然扛重启，
  备份频率与"服务是否在跑"一致——服务没跑就没有新数据，不备份也不丢东西。

三层的原因（PITR-lite，2026-11-18）：库 ~10MB，backup API + integrity_check
一次不到 1s，5 分钟一份完全跑得起。RPO 从 24h 压到 ≤5min；恢复点 =
"目标时刻之前最近的快照"，见 scripts/restore_pitr.py。

启动即先跑一次 daily：容器重建后立刻有一份新鲜快照，不用等到凌晨。
micro/hourly 不抢跑——它们本来间隔就短。
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
HERE = Path(__file__).resolve().parent

# 每层开关与节奏：0 = 关掉该层（daily 无开关，它是兜底层）
MICRO_SEC = int(os.getenv("BACKUP_MICRO_SEC", "300"))     # 0 关
HOURLY = os.getenv("BACKUP_HOURLY", "1") not in ("0", "false", "off")


def _next_micro(now: datetime) -> datetime | None:
    if MICRO_SEC <= 0:
        return None
    return now + timedelta(seconds=MICRO_SEC)


def _next_hourly(now: datetime) -> datetime | None:
    if not HOURLY:
        return None
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return target


def _next_daily(now: datetime) -> datetime:
    target = now.replace(hour=AT_HOUR, minute=AT_MINUTE, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _run_once(tier: str) -> int:
    cmd = [sys.executable, str(HERE / "backup_db.py"), "--tier", tier]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = (cp.stdout or "").strip() or (cp.stderr or "").strip()
    print(f"[{stamp}] {tier} rc={cp.returncode} {out}", flush=True)
    log = Path(os.getenv("BACKUP_DIR", "/backup")) / "backup.log"
    try:
        with log.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] sidecar {tier} rc={cp.returncode} {out}\n")
    except OSError as e:  # 只读挂载/权限问题不该让循环退出
        print(f"[{stamp}] log write failed: {e}", flush=True)
    return cp.returncode


def main() -> int:
    parts = [f"daily {AT_HOUR:02d}:{AT_MINUTE:02d}"]
    if HOURLY:
        parts.append("hourly :00")
    if MICRO_SEC > 0:
        parts.append(f"micro every {MICRO_SEC}s")
    print(f"[backup-loop] started: {', '.join(parts)}", flush=True)
    _run_once("daily")                # 先来一份，别等第一晚

    while True:
        now = datetime.now()
        queue = [(_next_daily(now), "daily")]
        h = _next_hourly(now)
        if h:
            queue.append((h, "hourly"))
        m = _next_micro(now)
        if m:
            queue.append((m, "micro"))
        nxt, tier = min(queue, key=lambda x: x[0])
        wait = (nxt - datetime.now()).total_seconds()
        print(f"[backup-loop] next {tier} at {nxt:%Y-%m-%d %H:%M} (in {wait/60:.0f}m)",
              flush=True)
        # 分段睡：单次 time.sleep 长时间在挂起/时钟跳变时会漂移
        while wait > 0:
            time.sleep(min(wait, 60))
            wait = (nxt - datetime.now()).total_seconds()
        _run_once(tier)


if __name__ == "__main__":
    sys.exit(main())
