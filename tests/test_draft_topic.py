"""draft_cards 的主题过滤契约。

真实故障：用户说「给实时数仓补卡」，工具却从全池按频次抓了「归因引擎」「跨层证据」的题，
起草出两张与该主题无关的卡。模型自己发现了并提示「请不要批准」，但真实用户很可能批量通过，
于是错误主题的卡进了知识库，还会被塞进模拟面试的评分上下文。

根因两条，都必须锁住：
1. 工具没有 topic 参数，只能「全池按频次取前 50」；
2. 主题没匹配到时会静默退化成全池挑——「没有」和「不知道」被当成同一件事。
"""
import pytest

from app.core import db
from app.services import agent_tools, multi_agent


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


def _q(qid: str, topic: str, text: str, freq: int = 1) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO question_pool (id,text,topic,freq_count,first_seen_at,last_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (qid, text, topic, freq, "2026-11-01", "2026-11-01"))
    conn.commit()


def _card(cid: str, topic: str, question: str) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO kb_cards (id,domain,topic,title,question,file_path,mastery,review_count,"
        "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "backend", topic, question[:30], question, f"knowledge/{cid}.md", 0.0, 0,
         "active", "2026-11-01", "2026-11-01"))
    conn.commit()


def test_rejects_when_neither_topic_nor_ids(fresh_db):
    r = agent_tools.IMPL["draft_cards"]({"limit": 2})
    assert r["ok"] is False
    assert "topic" in r["error"], "必须明确告诉模型二选一，否则它只会瞎猜"


def test_unknown_topic_does_not_fall_back_to_whole_pool(fresh_db):
    """核心断言：主题没题时返回 skipped，绝不拿别的主题凑数。"""
    _q("q-a", "实时数仓架构", "设计一个实时数仓", freq=9)
    r = agent_tools.IMPL["draft_cards"]({"topic": "量子计算编译优化", "limit": 2})
    assert r["ok"] is True
    assert r["drafted"] == 0
    assert "量子计算编译优化" in r["skipped"], "要指名是哪个主题没题"


def test_topic_with_all_cards_skips_without_drafting(fresh_db):
    _q("q-b", "归因引擎", "归因引擎怎么做多触点归因", freq=5)
    _card("kb-attr-001", "归因引擎", "归因引擎怎么做多触点归因")
    r = agent_tools.IMPL["draft_cards"]({"topic": "归因引擎", "limit": 2})
    assert r["drafted"] == 0
    assert "没有待建卡" in r["skipped"], "已有卡就不该再起草一张重复的"


def test_topic_filter_is_bidirectional_contains(fresh_db):
    """用户说「实时数仓」，题池记的是「实时数仓架构」——单向 LIKE 会漏。"""
    _q("q-c", "实时数仓架构", "设计一个实时数仓", freq=9)
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id FROM question_pool WHERE merged_into IS NULL "
        "AND (topic = ? OR topic LIKE ? OR ? LIKE '%' || topic || '%')",
        ("实时数仓", "%实时数仓%", "实时数仓")).fetchall()
    assert [r["id"] for r in rows] == ["q-c"]


def test_topic_filter_excludes_other_topics(fresh_db):
    """有 topic 时必须只看该主题，别的主题的高频题再高也不该进来。"""
    _q("q-hi", "归因引擎", "归因引擎的高频题", freq=99)
    _q("q-me", "实时数仓架构", "实时数仓的题", freq=1)
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id FROM question_pool WHERE merged_into IS NULL "
        "AND (topic = ? OR topic LIKE ? OR ? LIKE '%' || topic || '%') "
        "ORDER BY freq_count DESC LIMIT 50",
        ("实时数仓架构", "%实时数仓架构%", "实时数仓架构")).fetchall()
    ids = [r["id"] for r in rows]
    assert ids == ["q-me"], f"频次 99 的无关主题混进来了: {ids}"
