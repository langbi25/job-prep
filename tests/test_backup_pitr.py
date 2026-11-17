"""PITR 测试：分层备份命名/轮转、按时间点选快照、真实恢复链路。

备份脚本的 bug 最危险的一类是"静默丢历史"：
- daily 轮转把 micro 文件当旧的删了（层间误删）；
- 时间戳解析坏掉，恢复点选择错层；
- 选点逻辑 off-by-one（把"目标时刻之后"的快照当成可用恢复点——
  那是时间旅行，恢复出未来数据比恢复失败更可怕）。
这里用真实 gzip + 真实 sqlite 文件全链路验证，不用 mock。
"""
import datetime as dt
import gzip
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import backup_db, restore_drill, restore_pitr  # noqa: E402


def _make_db(path: Path, cards: int) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE kb_cards (id TEXT PRIMARY KEY, title TEXT)")
        for i in range(cards):
            con.execute("INSERT INTO kb_cards VALUES (?,?)", (f"c{i}", f"卡片{i}"))
        con.commit()
    finally:
        con.close()


def _snap(dir_, tier: str, stamp: str, cards: int) -> Path:
    """造一份带真实内容的分层快照（走真实 gzip，不 mock）。"""
    src = dir_ / f"src-{stamp}.sqlite3"
    _make_db(src, cards)
    gz = dir_ / backup_db.backup_name(tier, stamp)
    with src.open("rb") as f_in, gzip.open(gz, "wb", compresslevel=1) as f_out:
        import shutil
        shutil.copyfileobj(f_in, f_out)
    src.unlink()
    return gz


# ---- 命名与解析 ---------------------------------------------------------------

def test_backup_name_and_parse_roundtrip():
    name = backup_db.backup_name("micro", "20261118-001500")
    assert name == "kb-micro-20261118-001500.sqlite3.gz"
    ts = backup_db.parse_backup_ts(name)
    assert ts is not None and ts.strftime("%Y%m%d-%H%M%S") == "20261118-001500"


def test_parse_legacy_daily_name():
    """旧版 daily 命名（kb-20261116-020000...，无 tier 段）也要能解析。"""
    ts = backup_db.parse_backup_ts("kb-20261116-020000.sqlite3.gz")
    assert ts is not None and ts.year == 2026 and ts.hour == 2


def test_parse_rejects_non_backup_names():
    assert backup_db.parse_backup_ts("kb-daily-garbage.sqlite3.gz") is None
    assert backup_db.parse_backup_ts("notes.txt") is None


# ---- 分层轮转：层间必须隔离 -----------------------------------------------------

def test_rotate_scoped_to_tier(tmp_path):
    """daily 轮转只删 kb-daily-*：删掉 micro/hourly 等于把 PITR 降级回冷备。"""
    for tier, n in (("micro", 3), ("daily", 3)):
        for i in range(n):
            _snap(tmp_path, tier, f"2026111{i}-0200{i}0", cards=1)
    stale = backup_db.rotate(tmp_path, "daily", keep=1)
    assert len(stale) == 2 and all("daily" in p.name for p in stale)
    remaining = sorted(p.name for p in tmp_path.glob("kb-*.sqlite3.gz"))
    assert sum("micro" in n for n in remaining) == 3   # micro 全部幸存
    assert sum("daily" in n for n in remaining) == 1


def test_rotate_ignores_unparseable_names(tmp_path):
    (tmp_path / "kb-daily-垃圾.sqlite3.gz").write_bytes(b"x")
    for i in range(3):
        _snap(tmp_path, "daily", f"2026111{i}-020000", cards=1)
    stale = backup_db.rotate(tmp_path, "daily", keep=1)
    assert len(stale) == 2   # 垃圾名既不参与排序也不被删，留给人工处理


# ---- 按时间点选快照 -----------------------------------------------------------

def test_pick_for_time_nearest_before(tmp_path):
    """三个恢复点都能选时，必须取 ≤ 目标时刻的最新一个。"""
    for stamp in ("20261117-230000", "20261117-235500", "20261118-000500"):
        _snap(tmp_path, "micro", stamp, cards=1)
    picked = restore_pitr.pick_for_time(tmp_path, dt.datetime(2026, 11, 18, 0, 0))
    assert picked is not None and "235500" in picked[1].name


def test_pick_for_time_before_all_returns_none(tmp_path):
    _snap(tmp_path, "daily", "20261118-020000", cards=1)
    assert restore_pitr.pick_for_time(tmp_path, dt.datetime(2026, 11, 17, 0, 0)) is None


def test_pick_for_time_never_returns_future_snapshot(tmp_path):
    """目标时刻之后的新快照绝不能被选中——恢复出未来数据比失败更可怕。"""
    _snap(tmp_path, "daily", "20261117-020000", cards=1)
    _snap(tmp_path, "micro", "20261119-100000", cards=9)
    picked = restore_pitr.pick_for_time(tmp_path, dt.datetime(2026, 11, 18, 0, 0))
    assert picked is not None and "20261117" in picked[1].name


def test_scan_points_sorts_across_tiers(tmp_path):
    """micro/hourly/daily 混在一个目录，扫描输出按时间升序（不是字典序）。"""
    _snap(tmp_path, "micro", "20261118-001000", cards=1)
    _snap(tmp_path, "daily", "20261117-020000", cards=1)
    _snap(tmp_path, "hourly", "20261117-230000", cards=1)
    pts = restore_pitr.scan_points(tmp_path)
    assert [t.strftime("%H") for t, _ in pts] == ["02", "23", "00"]
    assert pts[0][1].name.startswith("kb-daily-")   # 时间最早的是 daily


# ---- 真实恢复链路 --------------------------------------------------------------

def test_restore_full_chain(tmp_path):
    """快照 → 解压 → integrity → 行数对账，产出能直接打开的库。"""
    gz = _snap(tmp_path, "micro", "20261118-001500", cards=7)
    out = tmp_path / "restored.sqlite3"
    r = restore_pitr.restore(gz, out)
    assert r["ok"] and r["integrity"] == "ok"
    assert r["counts"]["kb_cards"] == 7
    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM kb_cards").fetchone()[0] == 7
    finally:
        con.close()


def test_restore_rejects_corrupt_gzip(tmp_path):
    bad = tmp_path / "kb-micro-20261118-001500.sqlite3.gz"
    bad.write_bytes(b"not a gzip file")
    out = tmp_path / "r.sqlite3"
    r = restore_pitr.restore(bad, out)
    assert not r["ok"]
    assert not out.exists()   # 失败时不留"看起来像恢复产物"的半成品


# ---- 与 restore_drill 的衔接 ---------------------------------------------------

def test_drill_pick_latest_understands_tiers(tmp_path):
    """restore_drill.pick_latest 必须按时间取最新：分层命名后字典序是错的。"""
    _snap(tmp_path, "daily", "20261116-020000", cards=1)
    _snap(tmp_path, "micro", "20261118-001500", cards=1)
    latest = restore_drill.pick_latest(tmp_path)
    assert latest is not None and "micro" in latest.name


def test_tier_keep_env_override(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP_MICRO", "30")
    assert backup_db.tier_keep("micro") == 30
    monkeypatch.delenv("BACKUP_KEEP_MICRO")
    assert backup_db.tier_keep("micro") == backup_db.TIERS["micro"]
