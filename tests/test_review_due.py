"""到期口径一致性测试。

背景：同一个「今日到期」曾经在三个地方各算一次，其中注入 prompt 的那份写成
`srs_due IS NOT NULL AND srs_due<=今天`，只算逾期、漏掉新卡。结果是 prompt 说
「今日到期 3 张」而复习页有 25 张——模型据此回答用户，那 22 张新卡永远进不了对话。

所以这些断言不是在测 SQL 对不对，是在测「不同入口对同一个问题必须给同一个答案」。
口径分歧的代价不是显示难看，是模型照着错数字行动。
"""
import datetime

import pytest

from app.core import db
from app.services import console_service, review_service


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


def _cards(rows: list[tuple]) -> None:
    """rows = [(id, srs_due|None, status)]，一次事务写完。"""
    conn = db.get_conn()
    for cid, due, status in rows:
        conn.execute(
            "INSERT INTO kb_cards (id,domain,topic,title,question,file_path,mastery,"
            "review_count,srs_due,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, "backend", "redis", f"题{cid}", "问题", f"knowledge/{cid}.md", 0.0, 0,
             due, status, "2026-11-01T00:00:00", "2026-11-01T00:00:00"))
    conn.commit()


def _d(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def test_splits_new_cards_from_overdue(fresh_db):
    _cards([
        ("c-new-1", None, "active"),
        ("c-new-2", None, "active"),
        ("c-over-1", _d(-16), "active"),
        ("c-future", _d(3), "active"),
        ("c-archived", _d(-5), "archived"),
    ])
    st = review_service.due_stats()
    assert st["new"] == 2, "srs_due 为空的是新卡，不是「已排期」"
    assert st["overdue"] == 1, "未到期和归档卡都不能算逾期"
    assert st["due_total"] == 3
    assert st["active"] == 4, "归档卡不算活卡"
    assert st["oldest_overdue_days"] == 16


def test_stats_and_list_agree(fresh_db):
    _cards([("a", None, "active"), ("b", _d(-2), "active"), ("c", _d(5), "active")])
    st = review_service.due_stats()
    cards = review_service.due_cards(limit=1000)
    assert st["due_total"] == len(cards), "计数与列表必须指向同一批卡"


def test_daily_brief_not_truncated_at_100(fresh_db):
    """旧实现是 len(due_cards(limit=100))，120 张卡会静默报成 100。"""
    _cards([(f"bulk-{i:03d}", None, "active") for i in range(120)])
    brief = review_service.daily_brief()
    assert brief["due_cards"] == 120, "超过 100 张不能截断"
    assert brief["due_new"] == 120
    assert brief["due_overdue"] == 0
    assert len(brief["due_preview"]) == 5, "预览仍然只给前 5 张"


def test_prompt_snapshot_uses_same_numbers(fresh_db):
    """这条是真正的护栏：prompt 里的数字必须来自 due_stats，不能另写 SQL。"""
    _cards([("n1", None, "active"), ("n2", None, "active"), ("o1", _d(-16), "active")])
    st = review_service.due_stats()
    text = console_service._env_snapshot()
    assert f"待首次学习 {st['new']} 张" in text, text
    assert f"逾期复习 {st['overdue']} 张" in text, text
    assert f"最久逾期 {st['oldest_overdue_days']} 天" in text, text


def test_prompt_omits_overdue_clause_when_none(fresh_db):
    """没有逾期时不该出现「最久逾期 0 天」这种噪音。"""
    _cards([("n1", None, "active")])
    text = console_service._env_snapshot()
    assert "待首次学习 1 张" in text
    assert "逾期复习 0 张" in text
    assert "最久逾期" not in text
