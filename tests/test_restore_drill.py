"""恢复演练测试（零网络）。

演练脚本自己也会出 bug，而且它的 bug 最危险——"演练通过"会让人以为备份可用。
四种情形必须能区分：
1. 正常备份 → PASS；
2. 压缩包损坏 → FAIL（integrity/decompress 报错）；
3. 空壳库（在线有数据、备份 0 条）→ FAIL；
4. 备份文件超过 48h → FAIL（备份循环停了但没人知道）。
"""
import gzip
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import restore_drill  # noqa: E402


def _make_db(path: Path, cards: int = 3, plans: int = 2) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE kb_cards (id TEXT PRIMARY KEY, title TEXT)")
        con.execute("CREATE TABLE agent_plans (id TEXT PRIMARY KEY, goal TEXT)")
        con.execute("CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, cost REAL)")
        for i in range(cards):
            con.execute("INSERT INTO kb_cards VALUES (?,?)", (f"c{i}", f"卡片{i}"))
        for i in range(plans):
            con.execute("INSERT INTO agent_plans VALUES (?,?)", (f"p{i}", f"目标{i}"))
        for i in range(4):
            con.execute("INSERT INTO llm_usage (cost) VALUES (0.01)")
        con.commit()
    finally:
        con.close()


@pytest.fixture()
def tmpdir_(tmp_path):
    return tmp_path


def test_pick_latest_by_timestamp(tmpdir_):
    (tmpdir_ / "kb-20260101-020000.sqlite3.gz").write_bytes(b"x")
    (tmpdir_ / "kb-20261116-020000.sqlite3.gz").write_bytes(b"x")
    (tmpdir_ / "kb-20261115-020000.sqlite3").write_bytes(b"x")
    assert restore_drill.pick_latest(tmpdir_).name == "kb-20261116-020000.sqlite3.gz"


def test_pick_latest_empty_dir(tmpdir_):
    assert restore_drill.pick_latest(tmpdir_) is None


def test_drill_passes_on_healthy_backup(tmpdir_):
    live = tmpdir_ / "live.sqlite"
    _make_db(live, cards=5, plans=3)
    raw = tmpdir_ / "dump.sqlite3"
    _make_db(raw, cards=3, plans=2)
    bz = tmpdir_ / "kb-20261116-020000.sqlite3.gz"
    with raw.open("rb") as fi, gzip.open(bz, "wb") as fo:
        fo.write(fi.read())

    res = restore_drill.drill(bz, live)
    assert res["ok"], res["checks"]
    assert res["counts"]["kb_cards"] == 3
    assert res["live_counts"]["kb_cards"] == 5
    assert {c["check"] for c in res["checks"]} >= {
        "decompress", "integrity_check", "non_empty", "freshness", "sample_read"}


def test_drill_fails_on_corrupt_gzip(tmpdir_):
    bad = tmpdir_ / "kb-20261116-020000.sqlite3.gz"
    bad.write_bytes(b"not a gzip file")
    res = restore_drill.drill(bad, None)
    assert not res["ok"]


def test_drill_fails_on_empty_shell_backup(tmpdir_):
    """在线有 5 张卡、备份 0 张——文件能打开、integrity ok，但这不叫备份可用。"""
    live = tmpdir_ / "live.sqlite"
    _make_db(live, cards=5, plans=3)
    raw = tmpdir_ / "empty.sqlite3"
    _make_db(raw, cards=0, plans=0)
    bz = tmpdir_ / "kb-20261116-020000.sqlite3.gz"
    with raw.open("rb") as fi, gzip.open(bz, "wb") as fo:
        fo.write(fi.read())
    res = restore_drill.drill(bz, live)
    assert not res["ok"]
    assert any(c["check"] == "non_empty" and not c["ok"] for c in res["checks"])


def test_drill_fails_on_stale_backup(tmpdir_):
    raw = tmpdir_ / "old.sqlite3"
    _make_db(raw)
    bz = tmpdir_ / "kb-20260101-020000.sqlite3.gz"
    with raw.open("rb") as fi, gzip.open(bz, "wb") as fo:
        fo.write(fi.read())
    old = time.time() - 72 * 3600
    import os
    os.utime(bz, (old, old))
    res = restore_drill.drill(bz, None)
    assert not res["ok"]
    assert any(c["check"] == "freshness" and not c["ok"] for c in res["checks"])


def test_drill_on_plain_sqlite_copy(tmpdir_):
    raw = tmpdir_ / "kb-20261116-020000.sqlite3"
    _make_db(raw)
    res = restore_drill.drill(raw, None)
    assert res["ok"], res["checks"]


def test_missing_file(tmpdir_):
    res = restore_drill.drill(tmpdir_ / "nope.sqlite3.gz", None)
    assert not res["ok"]
