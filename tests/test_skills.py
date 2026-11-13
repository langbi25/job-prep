"""技能层测试（零 LLM 调用）。

技能是"注入给模型的 SOP + 工具白名单 + 验收契约"，所以可测的是：
文件解析、路由选择（含模糊不选）、白名单过滤、验收判定、使用记账。
跑法：pytest tests/test_skills.py -q
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.skills import registry  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="skill-test-"))
_ALL_TOOLS = ["search_kb", "get_daily_brief", "ingest_text", "draft_cards",
              "ingest_retrospective", "remember", "recall_memory"]


def _fresh_db() -> sqlite3.Connection:
    p = _TMP / "kb.sqlite"
    if p.exists():
        p.unlink()
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def test_load_all():
    skills = registry.load_all(force=True)
    assert set(skills) >= {"interview_ingest", "weakness_diagnosis", "retro_to_cards"}
    sk = skills["interview_ingest"]
    assert sk.title and sk.when and sk.body
    assert "ingest_text" in sk.tools
    assert sk.risk == "write"
    assert sk.max_iters >= 1
    assert "步骤" in sk.body


def test_select_interview():
    sk, board = registry.select("我贴一份字节后端的面试面经，帮我收进去")
    assert sk is not None and sk.name == "interview_ingest", board


def test_select_weakness():
    sk, board = registry.select("我最近总是挂，哪里弱？接下来该补什么")
    assert sk is not None and sk.name == "weakness_diagnosis", board


def test_select_retro():
    sk, board = registry.select("把这份复盘文档提炼成考点，然后生成题目和卡片")
    assert sk is not None and sk.name == "retro_to_cards", board


def test_select_none_for_unrelated():
    """无关请求不能硬套 SOP：套错会限制工具白名单，把该做的事挡在外面。"""
    sk, board = registry.select("今天天气怎么样")
    assert sk is None
    assert isinstance(board, list)


def test_hint_forces_skill():
    sk, board = registry.select("随便说点什么", hint="weakness_diagnosis")
    assert sk is not None and sk.name == "weakness_diagnosis"
    sk2, _ = registry.select("随便", hint="不存在的技能")
    assert sk2 is None


def test_allowed_tools():
    sk = registry.get("interview_ingest")
    allowed = registry.allowed_tools(sk, _ALL_TOOLS)
    assert "ingest_text" in allowed and "draft_cards" in allowed
    assert "get_daily_brief" not in allowed
    # 技能没写 tools 时放开全部，避免漏字段把模型锁死
    empty = registry.Skill(name="x", title="x", when="", body="", tools=())
    assert registry.allowed_tools(empty, _ALL_TOOLS) == _ALL_TOOLS


def test_check_missing_required():
    sk = registry.get("interview_ingest")
    bad = registry.check(sk, ["search_kb", "draft_cards"])
    assert not bad["ok"] and bad["missing"] == ["ingest_text"]
    ok = registry.check(sk, ["ingest_text", "draft_cards", "ingest_text"])
    assert ok["ok"] and ok["used"] == ["ingest_text", "draft_cards"]


def test_describe_contains_whitelist():
    sk = registry.get("weakness_diagnosis")
    text = registry.describe(sk)
    assert sk.title in text and "允许使用的工具" in text and "验收标准" in text


def test_record_and_stats():
    conn = _fresh_db()
    try:
        registry.record("interview_ingest", session_id="cs-1", status="ok", steps=3,
                        tools_used=["ingest_text"], tokens=100, duration_ms=1200)
        registry.record("interview_ingest", session_id="cs-2", status="partial", steps=1,
                        tools_used=[], duration_ms=800)
        rows = registry.stats()
        assert rows and rows[0]["skill"] == "interview_ingest"
        assert rows[0]["runs"] == 2 and rows[0]["ok_runs"] == 1
    finally:
        conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)


# ---------- 复习意图（2026-11-13） ----------
#
# "复习"是最高频的意图，却长期被 diagnose_to_cards 抢走——它的 when 里写了
# "把薄弱点做成复习卡"，而中文短句的有效 2-gram 太少，覆盖率算不出真正的意图。
# 现在靠 skills/*.md 的 aliases 字段做"用户明说了"的强信号。

def test_daily_review_skill_loaded():
    sk = registry.get("daily_review")
    assert sk is not None, "复习技能必须存在，否则这类意图只能靠模型自由发挥"
    assert "list_due_cards" in sk.tools
    assert sk.require_tools == ("list_due_cards",)
    assert sk.risk == "read", "复习不该有写权限：打分是用户点的，不是模型替他决定的"
    assert "复习" in sk.aliases


def test_select_daily_review():
    for q in ("今天该复习什么", "先复习哪几张卡比较值", "帮我抽背一下"):
        sk, board = registry.select(q)
        assert sk is not None and sk.name == "daily_review", (q, board)


def test_short_review_word_routes():
    """两个字也要能路由：这时 2-gram 覆盖率恒为 0，只能靠别名。"""
    sk, _ = registry.select("复习")
    assert sk is not None and sk.name == "daily_review"


def test_review_word_does_not_steal_diagnosis():
    """反过来也要成立：诊断类说法不能被复习技能抢走。"""
    sk, board = registry.select("看看我哪里弱，顺手给我出几张卡")
    assert sk is not None and sk.name == "diagnose_to_cards", board


def test_alias_does_not_widen_unrelated_queries():
    """别名是强信号，不是万能钥匙：没说那个词就照旧不选。"""
    sk, _ = registry.select("今天天气怎么样")
    assert sk is None
