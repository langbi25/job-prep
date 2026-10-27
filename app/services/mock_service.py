"""模拟面试服务（阶段 3 MVP）。

组卷策略：题目池 freq 加权 + 方向匹配（题目 topic 对齐 kb_cards 中该方向的 topic 优先）
+ 题池不足时 LLM 临场生成补充题（生成的题也入池，freq_count=0 不污染真实统计）。
逐题 LLM 四维评分（正确性/深度/结构化表达/亮点），结束后 LLM 总评报告归档 reports/。
回写掌握度闭环属阶段 4，本阶段只落 mock_sessions / mock_answers。
"""
import datetime
import json
import random
import sqlite3

from ..core import config, llm
from ..core.db import get_conn
from . import context_builder, review_service


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_session_id() -> str:
    return f"mock-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(1000, 9999)}"


def _direction_topics(conn: sqlite3.Connection, direction: str) -> set[str]:
    """该方向知识库覆盖的主题集合，用于题池的方向匹配。"""
    rows = conn.execute("SELECT DISTINCT topic FROM kb_cards WHERE domain=?", (direction,)).fetchall()
    return {r["topic"] for r in rows}


def compose_session(direction: str, persona: str = "", jd_text: str = "", question_count: int = 8,
                    company: str = "") -> dict:
    conn = get_conn()
    try:
        # 薄弱主题（阶段 4 闭环）：低掌握度卡片 + 近 14 天未解决错题的主题，组卷加权
        weak: set[str] = set()
        for r in conn.execute(
            "SELECT DISTINCT topic FROM kb_cards WHERE status='active' AND review_count>0 AND mastery<=2 AND topic IS NOT NULL"
        ):
            weak.add(r["topic"])
        since = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
        for r in conn.execute(
            "SELECT DISTINCT q.topic FROM mistakes m JOIN question_pool q ON q.id=m.question_id "
            "WHERE m.resolved=0 AND m.created_at>=? AND q.topic IS NOT NULL", (since,),
        ):
            weak.add(r["topic"])

        pool = [dict(q) for q in conn.execute("SELECT * FROM question_pool WHERE merged_into IS NULL").fetchall()]
        pool.sort(key=lambda q: q["freq_count"] + (2 if q["topic"] in weak else 0), reverse=True)
        topics = _direction_topics(conn, direction)
        preferred = [q for q in pool if q["topic"] in topics]
        others = [q for q in pool if q["topic"] not in topics]

        # 阶段 5 冲刺模式：指定公司时，该公司面经中出现过的题优先
        picked: list[dict] = []
        if company:
            crows = conn.execute(
                "SELECT q.* FROM question_pool q JOIN question_occurrences o ON o.question_id=q.id "
                "JOIN feed_interviews iv ON iv.id=o.interview_id "
                "WHERE iv.company LIKE ? AND q.merged_into IS NULL "
                "GROUP BY q.id ORDER BY q.freq_count DESC", (f"%{company}%",)).fetchall()
            picked = [dict(q) for q in crows]
        picked += [q for q in preferred if q not in picked]
        need = question_count - len(picked)

        # 题池不足 -> LLM 生成补充题（同时入池，freq_count=0）
        if need > 0:
            generated = llm.generate_questions(
                direction, jd_text, need,
                avoid=[q["text"] for q in picked] + [q["text"] for q in others[:10]],
            )
            today = _now()[:10]
            for g in generated:
                qid = f"q-{conn.execute('SELECT COUNT(*) AS n FROM question_pool').fetchone()['n'] + 1:04d}"
                conn.execute(
                    "INSERT INTO question_pool (id,text,aliases_json,qtype,topic,freq_count,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,0,?,?)",
                    (qid, g["text"], "[]", g.get("qtype", "开放题"), g.get("topic", ""), today, today),
                )
                picked.append({"id": qid, "text": g["text"], "qtype": g.get("qtype", "开放题"), "topic": g.get("topic", ""), "freq_count": 0})

        picked = picked[:question_count]
        questions = [{"qid": q["id"], "text": q["text"], "qtype": q["qtype"] or "", "topic": q["topic"] or ""} for q in picked]

        sid = _new_session_id()
        conn.execute(
            "INSERT INTO mock_sessions (id,direction,persona,jd_text,config_json,started_at) VALUES (?,?,?,?,?,?)",
            (sid, direction, persona, jd_text, json.dumps(questions, ensure_ascii=False), _now()),
        )
        conn.commit()
        return {"session_id": sid, "direction": direction, "persona": persona,
                "question_count": len(questions), "questions": questions}
    finally:
        conn.close()


def get_session(session_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mock_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["questions"] = json.loads(d["config_json"])
        answers = conn.execute("SELECT * FROM mock_answers WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        by_text = {}
        for a in answers:
            by_text[a["question_text"]] = dict(a) | {
                "scores": json.loads(a["scores_json"]) if a["scores_json"] else None,
                "followups": json.loads(a["followups_json"] or "null") if a["followups_json"] else None,
            }
        d["answers"] = [by_text.get(q["text"]) for q in d["questions"]]
        if d.get("overall_json"):
            d["overall"] = json.loads(d["overall_json"])
        return d
    finally:
        conn.close()


def submit_answer(session_id: str, index: int, answer_text: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mock_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("会话不存在")
        if row["finished_at"]:
            raise ValueError("该场面试已结束")
        questions = json.loads(row["config_json"])
        if not 0 <= index < len(questions):
            raise ValueError(f"题号越界：{index}")
        q = questions[index]
        persona = row["persona"] or ""

        if answer_text.strip():
            # 上下文工程：检索知识库要点 + 历史错题作为评分校准材料
            ctx = context_builder.build_eval_context(q.get("topic"), q["text"])
            ev = llm.evaluate_answer(q, answer_text, persona, ctx)
        else:
            ev = {"scores": {"correctness": 0, "depth": 0, "structure": 0, "highlight": 0},
                  "feedback": "（本题跳过，未作答不计分）", "followup": None}

        conn.execute("DELETE FROM mock_answers WHERE session_id=? AND question_text=?", (session_id, q["text"]))
        conn.execute(
            "INSERT INTO mock_answers (session_id,question_id,question_text,answer_text,scores_json,feedback,followups_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_id, q["qid"], q["text"], answer_text,
             json.dumps(ev["scores"], ensure_ascii=False), ev.get("feedback", ""),
             json.dumps(ev.get("followup"), ensure_ascii=False), _now()),
        )
        conn.commit()
        return {"index": index, "question": q["text"], "evaluation": ev}
    finally:
        conn.close()


def finish_session(session_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mock_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("会话不存在")
        if row["finished_at"]:
            return json.loads(row["overall_json"])

        questions = json.loads(row["config_json"])
        answers = {a["question_text"]: dict(a) for a in conn.execute(
            "SELECT * FROM mock_answers WHERE session_id=?", (session_id,)).fetchall()}
        conn.execute("DELETE FROM mock_answers WHERE session_id=? AND question_text NOT IN (%s)"
                     % ",".join("?" * len(questions)), (session_id, *[q["text"] for q in questions]))

        records = []
        for q in questions:
            a = answers.get(q["text"], {})
            records.append({
                "qid": q["qid"], "text": q["text"], "qtype": q.get("qtype", ""), "topic": q.get("topic", ""),
                "answer": a.get("answer_text"), "scores": json.loads(a["scores_json"]) if a.get("scores_json") else None,
                "feedback": a.get("feedback"),
            })

        scored = [r for r in records if r["scores"]]
        dims = ["correctness", "depth", "structure", "highlight"]
        avg = {d: round(sum(r["scores"][d] for r in scored) / len(scored), 1) if scored else 0 for d in dims}
        overall_avg = round(sum(avg.values()) / 4, 1)

        report = llm.overall_report(row["direction"], row["persona"] or "", records)

        report_rel = f"{session_id}.md"
        (config.REPORTS_DIR / report_rel).write_text(
            f"# 模拟面试报告 {session_id}\n\n"
            f"- 方向：{row['direction']}　persona：{row['persona'] or '默认'}\n"
            f"- 时间：{_now()}\n- 四维均分：{avg}（总均分 {overall_avg}）\n\n"
            f"## 总评\n\n{report.get('summary', '')}\n\n"
            f"## 通过评估\n\n{report.get('pass_estimate', '')}\n\n"
            f"## 亮点\n\n" + "\n".join(f"- {x}" for x in report.get("strengths", [])) +
            "\n\n## 短板\n\n" + "\n".join(f"- {x}" for x in report.get("weaknesses", [])) +
            "\n\n## 改进建议\n\n" + "\n".join(f"- {x}" for x in report.get("advice", [])) +
            "\n\n## 逐题明细\n\n" + "\n\n".join(
                f"### {i+1}. {r['text']}（{r['topic']}）\n\n"
                f"**回答**：{r['answer'] or '（跳过）'}\n\n**得分**：{r['scores']}\n\n**点评**：{r['feedback'] or ''}\n"
                for i, r in enumerate(records)
            ),
            encoding="utf-8",
        )

        overall = {"avg": avg, "overall_avg": overall_avg, "answered": len(scored),
                   "skipped": len(records) - len(scored), "report_path": f"reports/{report_rel}",
                   "report": report}
        conn.execute(
            "UPDATE mock_sessions SET finished_at=?, overall_json=?, report_path=? WHERE id=?",
            (_now(), json.dumps(overall, ensure_ascii=False), report_rel, session_id),
        )
        conn.commit()

        # 阶段 4 闭环：弱项回写错题本/掌握度/自动建缺口卡片（失败不影响报告）
        try:
            wb = review_service.writeback_after_session(session_id, records, row["direction"])
            overall["writeback"] = wb
            conn.execute(
                "UPDATE mock_sessions SET overall_json=? WHERE id=?",
                (json.dumps(overall, ensure_ascii=False), session_id),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            overall["writeback_error"] = str(e)
        return overall
    finally:
        conn.close()


def list_sessions(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, direction, persona, started_at, finished_at, overall_json FROM mock_sessions "
            "ORDER BY started_at DESC LIMIT ?", (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["overall_json"]:
                d["overall"] = json.loads(d["overall_json"])
            out.append(d)
        return out
    finally:
        conn.close()
