"""关系层（card_question_links）的回归测试（零 LLM 调用）。

跑法：
    python tests/test_relations.py          # 不需要 pytest，直接跑
    pytest tests/test_relations.py -q       # 装了 pytest 也能跑

背景：题↔卡关系原来只存在 question_pool.related_card_ids_json 列里——
装不下 match_method / confidence / created_at，「建卡记下的」和「向量猜的」
混在一个列表里，作战地图与看板的覆盖率口径打架。2026-11-06 起关系进表，
json 列降级为镜像。这里测的是双写一致性、迁移回灌和三个读方的行为。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.services import kb_service, multi_agent  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="rel-test-"))


def _fresh_db() -> sqlite3.Connection:
    """干净库 + 基础数据（1 题已入池）。返回连接，调用方负责关。"""
    p = _TMP / "kb.sqlite"
    if p.exists():
        p.unlink()
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO question_pool (id, text, topic, freq_count, first_seen_at, last_seen_at) "
        "VALUES ('q-9999', '怎么防止缓存击穿', 'Redis', 1, '2026-11-06', '2026-11-06')")
    conn.execute(
        "INSERT INTO kb_cards (id, domain, topic, title, status, file_path, created_at, updated_at) "
        "VALUES ('kb-test-001', 'backend', 'Redis', '缓存击穿', 'active', "
        "'backend/Redis/缓存击穿.md', '2026-11-06', '2026-11-06')")
    conn.commit()
    return conn


def test_link_dual_write():
    """link_question_card 必须双写：关系表为主，json 列为镜像。"""
    conn = _fresh_db()
    try:
        assert kb_service.link_question_card("q-9999", "kb-test-001", match_method="drafted")
        row = conn.execute("SELECT * FROM card_question_links").fetchone()
        assert row["match_method"] == "drafted" and row["question_id"] == "q-9999"
        mirror = conn.execute(
            "SELECT related_card_ids_json FROM question_pool WHERE id='q-9999'").fetchone()[0]
        assert "kb-test-001" in mirror
        # 重复挂不报错也不重复写
        assert not kb_service.link_question_card("q-9999", "kb-test-001", match_method="drafted")
        assert conn.execute("SELECT COUNT(*) FROM card_question_links").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM card_question_links WHERE card_id='kb-test-001' "
            "AND match_method='drafted'").fetchone()[0] == 1
    finally:
        conn.close()


def test_backfill_from_json_is_idempotent():
    """存量 json 关联由 _ensure_columns 回灌（match_method='legacy'），且不重复灌。"""
    conn = _fresh_db()
    try:
        conn.execute(
            "UPDATE question_pool SET related_card_ids_json='[\"kb-test-001\"]' WHERE id='q-9999'")
        conn.commit()
        conn.close()
        c = db.get_conn()      # 回灌迁移（get_conn 默认 Row 工厂）
        db._ensure_columns(c)
        c.close()
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        n1 = conn.execute("SELECT COUNT(*) FROM card_question_links").fetchone()[0]
        assert n1 == 1
        assert conn.execute(
            "SELECT match_method FROM card_question_links").fetchone()[0] == "legacy"
        # 模拟重启：再来一次迁移，不能多出重复行
        conn.close()
        c = db.get_conn()
        db._ensure_columns(c)
        c.close()
        conn = sqlite3.connect(db.DB_PATH)
        assert conn.execute("SELECT COUNT(*) FROM card_question_links").fetchone()[0] == n1
    finally:
        conn.close()


def test_transfer_moves_both_stores():
    """去重归档时关系转移：关系表和 json 镜像同步从 dup 挂到 keeper。"""
    conn = _fresh_db()
    try:
        conn.execute(
            "INSERT INTO kb_cards (id, domain, topic, title, status, file_path, created_at, updated_at) "
            "VALUES ('kb-dup-001', 'backend', 'Redis', '缓存击穿(重复)', 'active', "
            "'backend/Redis/缓存击穿(重复).md', '2026-11-06', '2026-11-06')")
        conn.execute("INSERT INTO card_question_links (question_id, card_id, match_method, created_at) "
                     "VALUES ('q-9999', 'kb-dup-001', 'drafted', '2026-11-06')")
        conn.execute(
            "UPDATE question_pool SET related_card_ids_json='[\"kb-dup-001\"]' WHERE id='q-9999'")
        conn.commit()
        moved = multi_agent._transfer_question_links(conn, "kb-dup-001", "kb-test-001")
        conn.commit()
        assert moved == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM card_question_links WHERE card_id='kb-dup-001'").fetchone()[0] == 0
        row = conn.execute(
            "SELECT match_method FROM card_question_links "
            "WHERE question_id='q-9999' AND card_id='kb-test-001'").fetchone()
        assert row and row["match_method"] == "drafted"   # 原 match_method 保留
        mirror = conn.execute(
            "SELECT related_card_ids_json FROM question_pool WHERE id='q-9999'").fetchone()[0]
        assert "kb-test-001" in mirror and "kb-dup-001" not in mirror
    finally:
        conn.close()


def test_linked_card_only_counts_active():
    """硬关联只认 active 卡：卡归档后题目必须回到「无卡」，不能拿死卡充覆盖。"""
    from app.services import coverage_service
    conn = _fresh_db()
    try:
        kb_service.link_question_card("q-9999", "kb-test-001", match_method="drafted")
        conn.commit()
        conn2 = sqlite3.connect(db.DB_PATH)
        conn2.row_factory = sqlite3.Row
        hit = coverage_service._linked_card(conn2, "q-9999")
        assert hit and hit["via"] == "linked"
        conn2.close()
        conn.execute("UPDATE kb_cards SET status='archived' WHERE id='kb-test-001'")
        conn.commit()
        conn2 = sqlite3.connect(db.DB_PATH)
        conn2.row_factory = sqlite3.Row
        assert coverage_service._linked_card(conn2, "q-9999") is None
        conn2.close()
    finally:
        conn.close()


def test_embeddings_carry_model():
    """embeddings 必须带 model 列；迁移给存量行回填当前模型 id。"""
    conn = _fresh_db()
    try:
        conn.close()
        db.init_db()   # 走完整迁移：补列 + 回填
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(embeddings)")}
        assert "model" in cols
        conn.execute(
            "INSERT INTO embeddings (entity_type, entity_id, text_hash, vector) "
            "VALUES ('card', 'kb-test-001', 'h0', '[]')")
        conn.commit()
        # 裸 INSERT（不带 model）默认空串：这是给"忘了写 model 的新代码"留的信号
        assert conn.execute(
            "SELECT model FROM embeddings WHERE entity_id='kb-test-001'").fetchone()[0] == ""
    finally:
        conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
