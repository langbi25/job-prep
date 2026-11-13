"""SQLite 在线备份：不锁库、不停服，产出可验证的压缩快照。

为什么不用 `cp kb.sqlite`：
- WAL 模式下最新数据在 `-wal` 文件里，单拷主库会丢掉最近的写入，
  表现是"备份成功但少了最后几条 span"——这种错比备份失败更难发现；
- 服务在跑时直接拷文件可能拷到半写状态。
所以走 sqlite3 的 backup API：它对源库做一致性快照，写 WAL 的应用不受影响。

用法（容器内）：
    python scripts/backup_db.py            # 备份到 $BACKUP_DIR 或 /backup
    python scripts/backup_db.py --keep 14  # 改保留份数

退出码：0 成功 / 1 失败 / 2 源库不存在。
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "/app/data/kb.sqlite"


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
    ap = argparse.ArgumentParser(description="SQLite 一致性备份 + gzip + 轮转")
    ap.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP", "7")),
                    help="保留最近 N 份（默认 7）")
    args = ap.parse_args()

    src = _resolve_db()
    outdir = _resolve_out()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = outdir / f"kb-{stamp}.sqlite3"
    gz = outdir / f"kb-{stamp}.sqlite3.gz"

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
    print(f"[backup] {gz.name} | {size_kb:.0f} KB | took {cost:.1f}s | "
          f"spans={counts['spans']} blobs={counts['span_blobs']} "
          f"texts={counts['blob_texts']} audit={counts['audit_log']}")

    # 轮转：按文件名（时间戳）排序，只留最近 keep 份。
    # 先备份成功再删旧的——反过来会在"备份失败 + 老文件已删"时清空全部历史。
    olds = sorted(outdir.glob("kb-*.sqlite3.gz"))
    for stale in olds[:-args.keep] if args.keep > 0 else []:
        stale.unlink()
        print(f"[backup] rotated out {stale.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
