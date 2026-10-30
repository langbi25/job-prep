"""采集确认环节冒烟（零 LLM）：验证查重不吞题、空 payload 能兜底、人工取消勾选生效。

背景（真实 bug）：FTS 2-gram 命中即判重 + INSERT OR IGNORE 静默丢弃，
导致 18 道题只有 10 道入库，8 道永久丢失。
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import config  # noqa: E402
from app.core.db import get_conn, init_db  # noqa: E402
from app.services import ingest_service  # noqa: E402

# 三道题刻意共享大量 bigram（"Agent/怎么/任务"），旧实现会把它们判成一题
QUESTIONS = [
    {"order": 1, "text": "多 Agent 协作时任务怎么拆分，状态怎么共享", "qtype": "架构", "topic_hint": "多Agent协作"},
    {"order": 2, "text": "Agent 任务执行到一半服务重启，现场怎么恢复", "qtype": "可用性", "topic_hint": "checkpoint"},
    {"order": 3, "text": "Agent 任务失败后怎么重试、熔断并保证幂等", "qtype": "稳定性", "topic_hint": "幂等熔断"},
]


def _mk_ingest(tag: str, questions: list[dict], direction: str = "agent") -> str:
    iid = f"ing-smoke-{tag}"
    raw_rel = f"pending/{iid}.md"
    raw_abs = config.INBOX_DIR / raw_rel
    raw_abs.parent.mkdir(parents=True, exist_ok=True)
    raw_abs.write_text("冒烟测试原文\n" + "\n".join(f"- {q['text']}" for q in questions), encoding="utf-8")
    extracted = {"company": "冒烟公司", "position": "Agent", "direction": direction,
                 "round": "一面", "interview_date": "", "result": "unknown", "questions": questions}
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ingests (id,source_type,raw_path,status,extracted_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (iid, "paste", raw_rel, "extracted", json.dumps(extracted, ensure_ascii=False),
             datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return iid


def _cleanup(interview_ids: list[str], ingest_ids: list[str]) -> None:
    conn = get_conn()
    try:
        for iv in interview_ids:
            qids = [r["question_id"] for r in conn.execute(
                "SELECT question_id FROM question_occurrences WHERE interview_id=?", (iv,))]
            conn.execute("DELETE FROM question_occurrences WHERE interview_id=?", (iv,))
            conn.execute("DELETE FROM coverage_reports WHERE interview_id=?", (iv,))
            conn.execute("DELETE FROM feed_interviews WHERE id=?", (iv,))
            for q in qids:
                if not conn.execute("SELECT 1 FROM question_occurrences WHERE question_id=?", (q,)).fetchone():
                    conn.execute("DELETE FROM question_pool WHERE id=?", (q,))
        for iid in ingest_ids:
            conn.execute("DELETE FROM ingests WHERE id=?", (iid,))
            for sub in ("pending", "archived"):
                f = config.INBOX_DIR / sub / f"{iid}.md"
                if f.exists():
                    f.unlink()
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    init_db()
    ok = True
    made_iv, made_ing = [], []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail else ""))
        ok = ok and cond

    # 1) 空 payload 兜底 + 查重不吞题
    iid = _mk_ingest("a", QUESTIONS)
    made_ing.append(iid)
    res = ingest_service.confirm_ingest(iid, {"questions": []})
    iv = res["interview_id"]
    made_iv.append(iv)
    check("空 payload 也能确认", res["questions_kept"] == 3, f"kept={res['questions_kept']}")

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT question_id, raw_text FROM question_occurrences WHERE interview_id=?", (iv,)).fetchall()
        qids = [r["question_id"] for r in rows]
        check("三道题全部落库（无静默丢弃）", len(rows) == 3, f"occurrences={len(rows)}")
        check("三道题各自独立题号", len(set(qids)) == 3, f"qids={qids}")
        ivr = conn.execute("SELECT direction, round, question_count FROM feed_interviews WHERE id=?", (iv,)).fetchone()
        check("direction/round 从抽取结果回落", ivr["direction"] == "agent" and ivr["round"] == "一面",
              f"{ivr['direction']}/{ivr['round']}")
        check("question_count 与实际一致", ivr["question_count"] == 3, str(ivr["question_count"]))
    finally:
        conn.close()

    # 2) 同一场面试内同一题号不会重复计数（跨场合并才 +freq）
    iid2 = _mk_ingest("b", [QUESTIONS[0]])
    made_ing.append(iid2)
    res2 = ingest_service.confirm_ingest(iid2, {"questions": []})
    made_iv.append(res2["interview_id"])
    conn = get_conn()
    try:
        a = conn.execute("SELECT question_id FROM question_occurrences WHERE interview_id=? ORDER BY ask_order", (iv,)).fetchone()
        b = conn.execute("SELECT question_id FROM question_occurrences WHERE interview_id=? ORDER BY ask_order",
                         (res2["interview_id"],)).fetchone()
        check("换场面试相同文本归并到同一题", a["question_id"] == b["question_id"], f"{a['question_id']} vs {b['question_id']}")
        fc = conn.execute("SELECT freq_count FROM question_pool WHERE id=?", (a["question_id"],)).fetchone()["freq_count"]
        check("归并后频次累加", fc == 2, f"freq={fc}")
    finally:
        conn.close()

    # 3) 人工取消勾选生效
    iid3 = _mk_ingest("c", QUESTIONS)
    made_ing.append(iid3)
    res3 = ingest_service.confirm_ingest(iid3, {"questions": [
        {"text": QUESTIONS[0]["text"], "keep": True},
        {"text": QUESTIONS[1]["text"], "keep": False},
        {"text": QUESTIONS[2]["text"], "keep": True},
    ]})
    made_iv.append(res3["interview_id"])
    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM question_occurrences WHERE interview_id=?",
                         (res3["interview_id"],)).fetchone()["n"]
        check("取消勾选的题不入池", n == 2 and res3["questions_kept"] == 2, f"kept={res3['questions_kept']}/occ={n}")
    finally:
        conn.close()

    _cleanup(made_iv, made_ing)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
