"""评测素材自动进料测试。

质量闭环的输入必须来自真实使用。这一层要守住的不变量：
1. 点踩/失败轮自动进料，去重不堆版本（同一轮反复点踩只进一条）；
2. 进料 ≠ 入集——素材没有标注，不进 golden 口径，只进 data/ 待人工提升；
3. 进料失败绝不影响对话（文件不可写时返回 None，不抛）；
4. conftest 默认 EVAL_INTAKE=0，被测路径要显式打开——测试不该往真实数据目录倒垃圾。
"""
import json

import pytest

from app.core import db
from app.services import eval_intake, turn_facts


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    # 进料文件也指到临时目录，不写真实 data/
    monkeypatch.setattr(eval_intake, "path", lambda: tmp_path / "intake.jsonl")
    monkeypatch.setenv("EVAL_INTAKE", "1")
    yield
    db.close_thread_conns()


def _seed_messages(session_id: str, question: str, answer: str) -> None:
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO console_messages (session_id,role,content,created_at) "
                     "VALUES (?,?,?,datetime('now'))", (session_id, "user", question))
        conn.execute("INSERT INTO console_messages (session_id,role,content,created_at) "
                     "VALUES (?,?,?,datetime('now'))", (session_id, "assistant", answer))
        conn.commit()
    finally:
        conn.close()


def test_reject_hook_intakes_failure_turn(fresh_db):
    _seed_messages("s1", "Kafka 的 ISR 机制是什么？", "这个嘛……我也不太确定。")
    tid = turn_facts.record(session_id="s1", turn=1, reply="这个嘛……我也不太确定。")
    out = turn_facts.set_feedback(tid, -1, note="答非所问")
    assert out["feedback"] == -1
    cases = eval_intake.stats()
    assert cases["count"] == 1
    c = cases["latest"][0]
    assert c["source"] == "user_reject" and c["session_id"] == "s1" and c["turn"] == 1
    assert "ISR" in c["question"], "问题原文要进素材，光有分数标注没法复判"
    assert c["meta"]["note"] == "答非所问"


def test_failed_turn_intakes_without_feedback(fresh_db):
    _seed_messages("s2", "帮我看看错题本", "")
    turn_facts.record(session_id="s2", turn=1, status="failed", error="LLM 500: boom")
    cases = eval_intake.stats()
    assert cases["count"] == 1 and cases["by_source"] == {"run_failed": 1}


def test_ok_turn_does_not_intake(fresh_db):
    """重试后成功的轮（llm_unstable）是链路抖动不是坏答案，不进素材。"""
    turn_facts.record(session_id="s3", turn=1, reply="正常回答")
    assert eval_intake.stats()["count"] == 0


def test_dedup_by_session_turn_question(fresh_db):
    _seed_messages("s4", "同一道题", "同一个答案")
    tid = turn_facts.record(session_id="s4", turn=1, reply="同一个答案")
    turn_facts.set_feedback(tid, -1)
    turn_facts.set_feedback(tid, -1, note="再点一次")
    assert eval_intake.stats()["count"] == 1, "同一轮重复点踩只进一条"


def test_disabled_env_writes_nothing(fresh_db, monkeypatch):
    monkeypatch.setenv("EVAL_INTAKE", "0")
    turn_facts.record(session_id="s5", turn=1, status="failed", error="x")
    assert eval_intake.stats()["count"] == 0


def test_record_failure_does_not_raise_when_unwritable(fresh_db, monkeypatch):
    monkeypatch.setattr(eval_intake, "path", lambda: "Z:/no/such/dir/intake.jsonl"
                        if __import__("os").name == "nt" else "/proc/no/such/dir/x.jsonl")
    out = eval_intake.record_case(question="q", answer="a", reason="r", source="t")
    assert out is None, "进料失败必须自吞，绝不能打断对话"


def test_long_text_clipped(fresh_db):
    c = eval_intake.record_case(question="问" * 5000, answer="答" * 9000,
                                reason="user_reject", source="turn")
    assert len(c["question"]) == eval_intake.MAX_QUESTION_CHARS
    assert len(c["answer"]) == eval_intake.MAX_ANSWER_CHARS, \
        "素材截断有上限：全文证据在 span_blobs，这里只留可标注的最小上下文"


def test_promote_script_merges_with_dedup(fresh_db, tmp_path, monkeypatch):
    _seed_messages("s6", "要提升的题", "要提升的答")
    tid = turn_facts.record(session_id="s6", turn=1, reply="要提升的答")
    turn_facts.set_feedback(tid, -1)
    # scripts/ 不是包，按文件路径加载；路径常量指到临时目录，不写仓库 evals/
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "promote_eval_cases",
        Path(__file__).resolve().parent.parent / "scripts" / "promote_eval_cases.py")
    promo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(promo)
    dst = tmp_path / "feedback_cases.jsonl"
    monkeypatch.setattr(promo, "SRC", eval_intake.path())
    monkeypatch.setattr(promo, "DST", dst)
    assert promo.main() == 0
    first = dst.read_text(encoding="utf-8").strip().splitlines()
    assert len(first) == 1
    # 再跑一遍：同 key 去重，不堆版本
    assert promo.main() == 0
    assert len(dst.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert json.loads(first[0])["key"]
