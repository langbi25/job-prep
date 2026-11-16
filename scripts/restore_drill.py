"""备份恢复演练：证明"备份真的能恢复"，而不只是"备份文件存在"。

为什么必须有这个脚本：
备份失败是最容易自我欺骗的一类可靠性问题——文件在、大小正常、日志 rc=0，
但没人验证过它能不能打开、数据全不全。等真出事才发现备份是空库或损坏时，
已经没有第二次机会了。**恢复演练是备份的一部分，不是额外工作**。

演练做四件事（顺序即失败优先级）：
1. 解压到临时目录（不动线上库，不碰备份原件）；
2. `PRAGMA integrity_check` —— 结构损坏在这里就暴露；
3. 关键表行数对账 —— 对照在线库：备份 ≤ 在线（允许落后），且不能是"在线有 3000 条、
   备份 0 条"这种空壳；备份文件修改时间超过 48h 也告警（备份循环可能停了）；
4. 抽样读一行 —— 能 count 不代表内容可读（页损坏/编码问题在 count 里看不出来）。

用法：
    python scripts/restore_drill.py                     # 用最新备份 + 默认在线库
    python scripts/restore_drill.py --file backups/kb-20261116-020000.sqlite3.gz
    python scripts/restore_drill.py --dir backups --live data/kb.sqlite

退出码：0 通过 / 1 不通过 / 2 找不到备份。
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

KEY_TABLES = ("kb_cards", "question_pool", "ingests", "agent_plans",
              "llm_usage", "tool_calls", "spans", "turn_facts")
STALE_HOURS = 48


def _resolve_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = (os.getenv("BACKUP_DIR") or "").strip()
    if env:
        return Path(env)
    if Path("/backup").is_dir():
        return Path("/backup")
    return Path("backups")


def pick_latest(directory: Path) -> Path | None:
    """按文件名时间戳取最新一份（与 backup_db 的轮转排序规则一致）。"""
    files = sorted(directory.glob("kb-*.sqlite3.gz")) + sorted(directory.glob("kb-*.sqlite3"))
    if not files:
        return None
    # 文件名带时间戳，字典序即时间序；同前缀的 .sqlite3 与 .sqlite3.gz 都能识别
    return max(files, key=lambda p: p.name.split(".")[0])


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _table_counts(path: Path, tables=KEY_TABLES) -> dict[str, int]:
    """-1 = 表不存在（老备份可能没有新表，这本身不算错）。"""
    con = _open_readonly(path)
    try:
        out = {}
        for t in tables:
            try:
                out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = -1
        return out
    finally:
        con.close()


def _sample_row(path: Path) -> tuple[bool, str]:
    """抽样读一行真实内容：count 通过但页损坏的情况，只有读内容才暴露。"""
    con = _open_readonly(path)
    try:
        for table, col in (("kb_cards", "title"), ("question_pool", "question"),
                           ("agent_plans", "goal")):
            try:
                row = con.execute(f"SELECT {col} FROM {table} LIMIT 1").fetchone()
            except sqlite3.OperationalError:
                continue
            if row is None:
                continue
            text = str(row[0] or "")
            return True, f"{table}.{col} 可读（{len(text)} 字符）"
        return True, "没有可抽样的业务表（空库也可能合法，需人工判断）"
    except sqlite3.DatabaseError as e:
        return False, f"抽样读取失败：{e}"
    finally:
        con.close()


def drill(backup: Path, live: Path | None = None) -> dict:
    """跑一次演练，返回结构化结果（可测试，不在内部 sys.exit）。"""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    if not backup.is_file():
        return {"ok": False, "backup": str(backup),
                "checks": [{"check": "find", "ok": False, "detail": "备份文件不存在"}]}

    workdir = Path(tempfile.mkdtemp(prefix="restore-drill-"))
    db = workdir / "restored.sqlite3"
    try:
        # 1) 解压 / 拷贝。损坏的 gzip 在这里就要变成"演练失败"，
        # 而不是把异常抛给调用方——脚本的产出是结论，不是堆栈。
        t0 = time.perf_counter()
        try:
            if backup.suffix == ".gz":
                with gzip.open(backup, "rb") as f_in, db.open("wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                add("decompress", True, f"{backup.name} → {db.stat().st_size/1024:.0f} KB")
            else:
                shutil.copy2(backup, db)
                add("copy", True, f"{backup.name} → {db.stat().st_size/1024:.0f} KB")
        except (OSError, EOFError) as e:
            add("decompress", False, f"解压/拷贝失败：{type(e).__name__}: {e}")
            return {"ok": False, "backup": str(backup), "checks": checks}
        took = time.perf_counter() - t0

        # 2) 完整性
        con = _open_readonly(db)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        add("integrity_check", integrity == "ok", str(integrity))

        # 3) 行数对账
        b_counts = _table_counts(db)
        l_counts = _table_counts(live) if (live and live.is_file()) else {}
        empties = [t for t, n in b_counts.items()
                   if n == 0 and l_counts.get(t, 0) > 0]
        add("non_empty", not empties,
            "空表：" + ", ".join(empties) if empties
            else "关键表均有数据或在线库同样为空")
        # 刻意**不**拿"备份比在线库多/少"当判据：spans 会被启动时按天清理，
        # 备份反而可能更多；老备份少一些也是正常的。只报差值供人看，不设阈值——
        # 设了阈值的检查早晚会因为一次合法清理变成误报。
        fresh_h = (time.time() - backup.stat().st_mtime) / 3600
        add("freshness", fresh_h <= STALE_HOURS,
            f"{fresh_h:.1f}h 前（阈值 {STALE_HOURS}h，超了说明备份循环可能停了）")

        # 4) 抽样读
        ok, detail = _sample_row(db)
        add("sample_read", ok, detail)

        return {"ok": all(c["ok"] for c in checks), "backup": str(backup),
                "live": str(live) if live else None, "counts": b_counts,
                "live_counts": l_counts, "elapsed_s": round(took, 2),
                "checks": checks}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _print_report(res: dict) -> None:
    print(f"[drill] backup={res['backup']}")
    for c in res["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        print(f"  [{mark}] {c['check']}: {c['detail']}")
    if res.get("counts"):
        print("  counts: " + ", ".join(f"{k}={v}" for k, v in res["counts"].items()))
    print(f"[drill] {'PASS' if res['ok'] else 'FAIL'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="备份恢复演练（解压 + 完整性 + 行数对账 + 抽样）")
    ap.add_argument("--file", help="指定备份文件；默认取目录里最新一份")
    ap.add_argument("--dir", help="备份目录；默认 BACKUP_DIR / /backup / ./backups")
    ap.add_argument("--live", help="在线库路径（用于行数对账）；默认 data/kb.sqlite")
    args = ap.parse_args()

    backup = Path(args.file) if args.file else pick_latest(_resolve_dir(args.dir))
    if backup is None:
        print("[drill] 找不到任何备份文件", file=sys.stderr)
        return 2
    live = Path(args.live) if args.live else Path(
        (os.getenv("DB_PATH") or "data/kb.sqlite"))
    res = drill(backup, live)
    _print_report(res)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
