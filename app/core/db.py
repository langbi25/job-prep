import sqlite3
from pathlib import Path

from .config import DB_PATH, DATA_DIR, ensure_dirs

_SCHEMA = Path(__file__).resolve().parent.parent.parent / "schema.sql"

# FTS 表 → 其触发器。表定义过期（列名对不上 / 不是外部内容表）时连触发器一起重建，
# 否则旧触发器会继续往"不存在的列"或已被删除的表里写。
_FTS_TRIGGERS = {
    "kb_cards_fts": ("cards_ai", "cards_au", "cards_ad"),
    "question_fts": ("questions_ai", "questions_au", "questions_ad"),
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _stale_fts(conn: sqlite3.Connection) -> list[str]:
    """找出定义过期的 FTS 表。

    两种历史坑：
    1) kb_cards_fts 声明了内容表里不存在的 body 列 → 读列值必报错；
    2) question_fts 曾是独立表（无 content=），靠代码手工同步 → 索引漂移。
    """
    stale = []
    for table in _FTS_TRIGGERS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row["sql"]:
            continue
        if "content=" not in row["sql"] or "body" in row["sql"]:
            stale.append(table)
    return stale


def _drop_stale_fts(conn: sqlite3.Connection) -> None:
    for table in _stale_fts(conn):
        for trg in _FTS_TRIGGERS[table]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """轻量列迁移：给已存在的库补新增列（schema.sql 只对新建库生效）。

    SQLite 的 ALTER TABLE ADD COLUMN 很便宜，逐个判断比引入迁移框架更务实。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(kb_cards)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE kb_cards ADD COLUMN merged_into TEXT REFERENCES kb_cards(id)")

    # 题目池的"答案三态"：由面经抽取阶段判定，决定建卡走原文规范化还是 LLM 起草。
    # 存下来而不是只留在 extracted_json 里，是因为卡建完、面经归档后还要能回溯
    # "这张卡的参考答案是原文来的还是模型编的"。
    qcols = {r["name"] for r in conn.execute("PRAGMA table_info(question_pool)")}
    if "answer_completeness" not in qcols:
        conn.execute("ALTER TABLE question_pool ADD COLUMN answer_completeness TEXT")
    if "answer_gaps_json" not in qcols:
        conn.execute("ALTER TABLE question_pool ADD COLUMN answer_gaps_json TEXT DEFAULT '[]'")


def init_db():
    """执行 schema.sql 建库建表（幂等），并保证两套全文索引与内容表一致。"""
    ensure_dirs()
    conn = get_conn()
    try:
        _drop_stale_fts(conn)
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        _ensure_columns(conn)
        # 外部内容 FTS 与内容表漂移时 MATCH 只会静默漏召回，比报错更难查。
        # 千级行 rebuild 是毫秒级，启动时全量重建换确定性很划算。
        for table in _FTS_TRIGGERS:
            try:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()
