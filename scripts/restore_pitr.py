"""按时间点恢复（PITR-lite）：从三层快照里选"目标时刻之前最近的一份"还原。

三层快照（micro 5min / hourly 1h / daily 1d）给出的恢复点粒度：
    micro 层在 ⇒ 最多丢 5 分钟
    目标时刻早于最老 micro ⇒ 依次退到 hourly / daily，丢的窗口按层变大

为什么这不是"真 PITR"：SQLite 没有原生 WAL 归档，秒级恢复需要 Litestream
这类流式复制（Go 依赖）。当前数据量与 RPO 需求下，5 分钟粒度够用；
真要秒级时换 Litestream，见 docs/规划-架构演进.md 决策记录 D7。

原则：
- **绝不直接覆盖在线库**。默认恢复到临时目录做验证；要落地必须显式 --out，
  而且打印醒目警告（覆盖在线库前先停服务、先备份现状）。
- 恢复后必做 integrity_check + 行数对账——"文件能解压"不等于"库能用"。

用法（容器内或宿主，BACKUP_DIR 决定快照目录）：
    python scripts/restore_pitr.py --at "2026-11-18T00:30"           # 选点 + 验证恢复
    python scripts/restore_pitr.py --at "2026-11-18T00:30" --out r.sqlite3
    python scripts/restore_pitr.py --list                            # 列出所有恢复点

退出码：0 成功 / 1 失败 / 2 找不到可用恢复点。
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from backup_db import parse_backup_ts  # 同目录共享解析，避免两处正则漂移

# 与 restore_drill 保持一致的行数对账口径
KEY_TABLES = ("kb_cards", "question_pool", "ingests", "agent_plans",
              "llm_usage", "tool_calls", "spans", "turn_facts")


def _resolve_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    import os  # noqa: PLC0415
    env = (os.getenv("BACKUP_DIR") or "").strip()
    if env:
        return Path(env)
    if Path("/backup").is_dir():
        return Path("/backup")
    return Path("backups")


def scan_points(directory: Path) -> list[tuple[dt.datetime, Path]]:
    """目录里所有可用恢复点，按时间升序。解析不了的文件名直接跳过。"""
    out = []
    for p in directory.glob("kb-*.sqlite3.gz"):
        ts = parse_backup_ts(p.name)
        if ts is not None:
            out.append((ts, p))
    out.sort(key=lambda x: x[0])
    return out


def pick_for_time(directory: Path, at: dt.datetime) -> tuple[Path, dt.datetime] | None:
    """选 `at` 之前最近的恢复点；没有就 None。"""
    points = [x for x in scan_points(directory) if x[0] <= at]
    return points[-1] if points else None


def _table_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        out = {}
        for t in KEY_TABLES:
            try:
                out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = -1   # 表不存在（老快照没有新表），不算错
        return out
    finally:
        con.close()


def restore(snapshot: Path, out: Path) -> dict:
    """解压快照到 out，做完整性检查。返回结构化结果，不打印（打印归 CLI 层）。"""
    result: dict = {"snapshot": snapshot.name, "out": str(out), "ok": False}
    workdir = Path(tempfile.mkdtemp(prefix="pitr-"))
    db = workdir / "restored.sqlite3"
    try:
        try:
            with gzip.open(snapshot, "rb") as f_in, db.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        except (OSError, EOFError, gzip.BadGzipFile) as e:
            # 坏压缩包在这里变成"恢复失败"，不能把异常抛给调用方、
            # 更不能留下半个恢复产物让人觉得"恢复了"
            result["error"] = f"{type(e).__name__}: {e}"
            return result

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        result["integrity"] = integrity
        if integrity != "ok":
            return result
        result["counts"] = _table_counts(db)
        if out.is_file() and out.resolve() != db.resolve():
            out.unlink()
        shutil.copy2(db, out)
        result["ok"] = True
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _fmt_points(points: list[tuple[dt.datetime, Path]]) -> str:
    lines = []
    for ts, p in points:
        lines.append(f"  {ts:%Y-%m-%d %H:%M}  {p.name}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="按时间点恢复（PITR-lite）")
    ap.add_argument("--at", help="目标时刻，ISO 格式（如 2026-11-18T00:30）")
    ap.add_argument("--out", help="恢复产物路径；不给就恢复到临时目录做验证")
    ap.add_argument("--dir", help="快照目录（默认 $BACKUP_DIR / /backup / backups）")
    ap.add_argument("--list", action="store_true", help="只列出可用恢复点")
    args = ap.parse_args()

    directory = _resolve_dir(args.dir)
    points = scan_points(directory)
    if not points:
        print(f"[pitr] {directory} 里没有任何可用恢复点", file=sys.stderr)
        return 2
    if args.list:
        print(f"[pitr] {directory} 共 {len(points)} 个恢复点：")
        print(_fmt_points(points))
        return 0

    if not args.at:
        ap.error("需要 --at（或 --list）")
    try:
        at = dt.datetime.fromisoformat(args.at)
    except ValueError as e:
        print(f"[pitr] --at 解析失败：{e}", file=sys.stderr)
        return 1

    picked = pick_for_time(directory, at)
    if picked is None:
        print(f"[pitr] {at:%Y-%m-%d %H:%M} 之前没有任何恢复点。"
              f"最早的恢复点是：\n{_fmt_points(points[:3])}", file=sys.stderr)
        return 2
    ts, snap = picked
    lost_s = (at - ts).total_seconds()

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="pitr-out-")) / "restored.sqlite3"
    t0 = time.perf_counter()
    r = restore(snap, out)
    took = time.perf_counter() - t0

    if not r["ok"]:
        print(f"[pitr] FAIL integrity={r.get('integrity')}", file=sys.stderr)
        return 1
    print(f"[pitr] 目标 {at:%Y-%m-%d %H:%M} → 快照 {snap.name}（{ts:%Y-%m-%d %H:%M}）")
    print(f"[pitr] 丢失窗口 {lost_s/60:.1f} 分钟（= 目标 - 快照时刻，RPO 由采样间隔决定）")
    print(f"[pitr] 恢复到 {out} | {out.stat().st_size/1024:.0f} KB | took {took:.1f}s | "
          f"integrity={r['integrity']}")
    counts = r["counts"]
    print("[pitr] 行数对账：" +
          " ".join(f"{t}={n}" for t, n in counts.items() if n >= 0))
    empties = [t for t, n in counts.items() if n == 0]
    if empties:
        print(f"[pitr] 注意：这些表在恢复点时刻为空：{', '.join(empties)}"
              f"（不一定是错，可能是当时确实没数据）")
    if not args.out:
        print("[pitr] 未指定 --out：产物在临时目录，仅作验证，不会碰在线库")
    else:
        print("[pitr] ⚠ 恢复产物已写到指定路径。覆盖在线库前：先停服务、"
              "先备份现状（python scripts/backup_db.py --tier daily）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
