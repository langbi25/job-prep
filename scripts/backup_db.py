"""SQLite 在线备份：不锁库、不停服，产出可验证的压缩快照。

为什么不用 `cp kb.sqlite`：
- WAL 模式下最新数据在 `-wal` 文件里，单拷主库会丢掉最近的写入，
  表现是"备份成功但少了最后几条 span"——这种错比备份失败更难发现；
- 服务在跑时直接拷文件可能拷到半写状态。
所以走 sqlite3 的 backup API：它对源库做一致性快照，写 WAL 的应用不受影响。

三层快照（PITR-lite，2026-11-18）：
SQLite 没有原生 WAL 归档，秒级真 PITR 需要 Litestream 这类流式复制工具
（Go 依赖，另立决策）。这里的取舍是**用频率换粒度**：库只有 ~10MB，
backup API + integrity_check 一次不到 1s，5 分钟一份完全跑得起——
RPO 从 24h 压到 ≤5min，恢复点取"最近的快照"，丢的窗口 ≤ 一个采样间隔。

    micro  每 5 分钟   留 12 份（近 1 小时）
    hourly 每小时整点  留 24 份（近 1 天）
    daily  每日 02:00  留 7 份（近一周）

文件名 `kb-{tier}-{stamp}.sqlite3.gz`，轮转按层隔离（daily 的轮转
绝不能把 micro 的文件当"旧备份"删掉——这是分层最容易踩的坑）。

用法（容器内）：
    python scripts/backup_db.py                    # daily（兼容旧行为）
    python scripts/backup_db.py --tier micro       # 5 分钟快照
    python scripts/backup_db.py --keep 14          # 覆盖该层保留份数

退出码：0 成功 / 1 失败 / 2 源库不存在。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "/app/data/kb.sqlite"

# ---- 分层快照配置 -----------------------------------------------------------
# 保留份数可用环境变量覆盖：BACKUP_KEEP_MICRO / _HOURLY / _DAILY（--keep 优先级最高）。
TIERS: dict[str, int] = {"micro": 12, "hourly": 24, "daily": 7}
_TS_RE = re.compile(r"(\d{8}-\d{6})")


def tier_keep(tier: str) -> int:
    env = os.getenv(f"BACKUP_KEEP_{tier.upper()}", "").strip()
    if env.isdigit():
        return int(env)
    return TIERS[tier]


def backup_name(tier: str, stamp: str) -> str:
    return f"kb-{tier}-{stamp}.sqlite3.gz"


def parse_backup_ts(name: str) -> _dt.datetime | None:
    """从备份文件名解析时间戳；解析不了（坏名字/旧格式外的文件）返回 None。

    旧版 daily 文件名是 kb-20261116-020000.sqlite3.gz（无 tier 段），同样能解析。
    """
    m = _TS_RE.search(name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def rotate(outdir: Path, tier: str, keep: int) -> list[Path]:
    """按层轮转：只动本层前缀的文件。先排序后删除，调用方保证备份已成功。"""
    if keep <= 0:
        return []
    files = [p for p in outdir.glob(f"kb-{tier}-*.sqlite3.gz")
             if parse_backup_ts(p.name) is not None]
    files.sort(key=lambda p: p.name)
    stale = files[:-keep]
    for p in stale:
        p.unlink()
    return stale


def _resolve_db() -> Path:
    env = (os.getenv("DB_PATH") or "").strip()
    candidates = [Path(env)] if env else []
    candidates += [Path(DEFAULT_DB), Path("data/kb.sqlite")]
    for c in candidates:
        if c.is_file():
            return c
    print(f"[backup] source db missing, tried: {[str(c) for c in candidates]}", file=sys.stderr)
    sys.exit(2)


def _resolve_out() -> Path:
    env = (os.getenv("BACKUP_DIR") or "").strip()
    if env:
        out = Path(env)
    elif Path("/backup").is_dir():
        out = Path("/backup")
    else:
        out = Path("data/backup")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _counts(con: sqlite3.Connection) -> dict[str, int]:
    """备份里到底装了什么——空库也能"备份成功"，所以必须报数量。"""
    out = {}
    for t in ("spans", "span_blobs", "blob_texts", "audit_log", "console_messages", "kb_cards"):
        try:
            out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = -1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 一致性备份 + gzip + 分层轮转")
    ap.add_argument("--tier", choices=sorted(TIERS), default="daily",
                    help="快照层级（决定文件名与默认保留份数）")
    ap.add_argument("--keep", type=int, default=None,
                    help="覆盖该层保留份数（默认按层取 TIERS/环境变量）")
    args = ap.parse_args()

    src = _resolve_db()
    outdir = _resolve_out()
    keep = args.keep if args.keep is not None else tier_keep(args.tier)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = outdir / f"kb-{args.tier}-{stamp}.sqlite3"
    gz = outdir / backup_name(args.tier, stamp)

    started = time.time()
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    dst_con = sqlite3.connect(tmp)
    try:
        src_con.backup(dst_con)                     # 一致性快照（WAL 安全）
        integrity = dst_con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            print(f"[backup] integrity check failed: {integrity}", file=sys.stderr)
            return 1
        counts = _counts(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    # gzip 落盘后再删临时文件：中途失败时不会留下"看起来像备份"的半成品
    with tmp.open("rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()

    size_kb = gz.stat().st_size / 1024
    cost = time.time() - started
    print(f"[backup:{args.tier}] {gz.name} | {size_kb:.0f} KB | took {cost:.1f}s | "
          f"spans={counts['spans']} blobs={counts['span_blobs']} "
          f"texts={counts['blob_texts']} audit={counts['audit_log']}")

    # 轮转按层隔离：daily 的清理只看 kb-daily-*，绝不碰 micro/hourly 的文件。
    # 先备份成功再删旧的——反过来会在"备份失败 + 老文件已删"时清空全部历史。
    for stale in rotate(outdir, args.tier, keep):
        print(f"[backup:{args.tier}] rotated out {stale.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
