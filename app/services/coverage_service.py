"""飞轮引擎：投喂即分析（作战地图）+ 进化方向报告 + 真实结果校准。

三圈闭环里的第 2、3 圈：
- 作战地图：每份面经逐题对照"我当前的状态"（有没有卡/掌握度/错题史/参考要点/预测档位），
  面试前先看这张地图，缺什么补什么；
- 进化报告：回答"这个项目下一步往哪进化"——高频无卡题是最高优先级的进化信号；
- 真实回录：面试回来录入实际表现，与预测对比，偏差收敛说明系统判断越来越准。

预测用规则基线（无卡=low / mastery>=3=high / 其余=mid），不依赖 LLM：
规则透明、零成本、可被评测；LLM 增强放后续，必须先跑赢规则基线才配替换。
"""
import datetime
import json
import re

from ..core import embedder
from ..core.db import get_conn

SIM_MIN = 0.75          # 同主题匹配下限：实测同主题真实覆盖落在 0.83~0.99
CROSS_TOPIC_MIN = 0.85  # 跨主题匹配下限：更高，防止"都聊技术"的伪相似
WEAK_MASTERY = 1.5      # 有卡但低于此值视为"有卡但弱"
BAND_SCORE = {"high": 8, "mid": 5, "low": 2}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm_topic(s: str | None) -> str:
    """主题归一化：去掉空格/标点/大小写差异。

    "工具链Skill化" 与 "工具链 Skill 化" 是同一个主题，直接 == 会判成跨主题，
    把同主题匹配下限 0.75 抬到 0.85，刚好把 0.82 的真实覆盖挡在门外。
    """
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "").lower()


def _same_topic(a: str | None, b: str | None) -> bool:
    """同一主题的判定：归一化后相等，或一方包含另一方。

    只做 == 太脆：题目 topic 来自面经抽取的 topic_hint（"Memory" / "Spark"），
    卡片 topic 来自建卡 prompt 自由生成（"Agent 记忆" / "Spark Shuffle 实现"），
    粒度天然对不上。对不上就会被当成跨主题，门槛从 0.75 抬到 0.85，
    把 0.80 这种真实覆盖直接挡在门外——这就是"审批通过后仍显示无卡"的主因之一。
    """
    na, nb = _norm_topic(a), _norm_topic(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 2 and short in long_


def _card_row(conn, card_id: str):
    return conn.execute(
        "SELECT id, title, topic, mastery, question FROM kb_cards WHERE id=? AND status='active'",
        (card_id,)).fetchone()


def _linked_card(conn, question_id: str | None):
    """硬关联：建卡时记下的"这张卡属于这道题"，命中即算覆盖，不必看相似度。"""
    if not question_id:
        return None
    row = conn.execute(
        "SELECT related_card_ids_json FROM question_pool WHERE id=?", (question_id,)).fetchone()
    if not row:
        return None
    try:
        ids = json.loads(row["related_card_ids_json"] or "[]")
    except (ValueError, TypeError):
        return None
    for cid in ids if isinstance(ids, list) else []:
        card = _card_row(conn, cid)
        if card:
            return dict(card) | {"similarity": 1.0, "via": "linked"}
    return None


def _match_card(conn, text: str, topic: str | None = None,
                question_id: str | None = None) -> dict | None:
    """题目 ↔ 知识卡匹配，三级判定：

    ① 硬关联（related_card_ids_json）：建卡时记下的确定关系，命中直接算覆盖；
    ② 同主题语义匹配：门槛 SIM_MIN(0.75)，主题用相等或包含判定；
    ③ 跨主题近同句：门槛 CROSS_TOPIC_MIN(0.85)，防止"Agent 死循环"蹭到"Flink 反压"。

    阈值来自实测分布（6 张卡 × 55 题）：同主题真实覆盖 0.83~0.99；
    跨主题噪声集中在 0.50~0.68，单靠余弦会把噪声全判成"有卡"，吃掉缺口信号。
    取 top_k=3 而不是 1：最近邻可能是另一张卡的噪声（q-0029 的 top3 是 0.804/0.799/0.787），
    只看最近邻会因为"第一张恰好跨主题"而漏掉后面真正同主题的那张。
    """
    if not embedder.available():
        return None
    linked = _linked_card(conn, question_id)
    if linked:
        return linked
    try:
        hits = embedder.search_similar(text, "card", top_k=3)
    except Exception:
        return None
    for card_id, sim in hits:
        row = _card_row(conn, card_id)
        if not row:
            continue
        need = SIM_MIN if _same_topic(topic, row["topic"]) else CROSS_TOPIC_MIN
        if sim >= need:
            return dict(row) | {"similarity": round(sim, 3), "via": "semantic"}
    return None


def _predict(card: dict | None) -> tuple[str, str | None]:
    """规则基线预测：返回 (预测档位, 缺口标签)。"""
    if card is None:
        return "low", "无卡"
    if card["mastery"] >= 3:
        return "high", None
    if card["mastery"] < WEAK_MASTERY:
        return "mid", "有卡但弱"
    return "mid", None


def analyze_interview(interview_id: str) -> dict:
    """对一场已确认的面经生成作战地图（重跑覆盖旧结果）。"""
    conn = get_conn()
    try:
        iv = conn.execute("SELECT * FROM feed_interviews WHERE id=?", (interview_id,)).fetchone()
        if not iv:
            raise ValueError(f"面经不存在: {interview_id}")
        questions = conn.execute(
            "SELECT q.id AS qid, q.text, q.qtype, q.topic, q.freq_count "
            "FROM question_occurrences o JOIN question_pool q ON q.id=o.question_id "
            "WHERE o.interview_id=? AND q.merged_into IS NULL ORDER BY q.freq_count DESC",
            (interview_id,)).fetchall()

        items, gaps = [], []
        for q in questions:
            card = _match_card(conn, q["text"], q["topic"], q["id"])
            pred, gap = _predict(card)
            mistake_n = conn.execute(
                "SELECT COUNT(*) AS n FROM mistakes WHERE question_id=? AND resolved=0",
                (q["qid"],)).fetchone()["n"]
            if gap == "无卡" and q["freq_count"] >= 2:
                gap = "无卡·高频"          # 最优先的进化信号
            if gap in ("无卡", "无卡·高频"):
                gaps.append({"text": q["text"], "freq": q["freq_count"], "gap": gap})
            items.append({
                "qid": q["qid"], "text": q["text"], "qtype": q["qtype"], "topic": q["topic"],
                "freq": q["freq_count"], "predicted_band": pred, "gap": gap,
                "card": {"id": card["id"], "title": card["title"], "mastery": card["mastery"],
                         "similarity": card["similarity"]} if card else None,
                "answer_points": [],   # 有卡时由下方循环从 points_json 填充
                "open_mistakes": mistake_n,
            })
    finally:
        conn.close()

    # 参考答案要点：有卡时从卡片要点生成（复用 kb_cards 数据，不花 LLM）
    for it in items:
        if it["card"]:
            c = get_conn()
            try:
                row = c.execute("SELECT points_json FROM kb_cards WHERE id=?", (it["card"]["id"],)).fetchone()
                it["answer_points"] = json.loads(row["points_json"])[:4] if row else []
            finally:
                c.close()

    scored = [BAND_SCORE[it["predicted_band"]] for it in items]
    payload = {
        "interview_id": interview_id,
        "generated_at": _now(),
        "company": iv["company"], "position": iv["position"], "direction": iv["direction"],
        "items": items,
        "summary": {
            "questions": len(items),
            "expected_score": round(sum(scored) / len(scored), 1) if scored else None,
            "band_dist": {b: sum(1 for it in items if it["predicted_band"] == b)
                          for b in ("high", "mid", "low")},
            "gaps": gaps,
            "actions": _suggest_actions(items),
        },
    }

    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO coverage_reports (interview_id, generated_at, payload_json) VALUES (?,?,?)",
            (interview_id, payload["generated_at"], json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return payload


def _suggest_actions(items: list[dict]) -> list[dict]:
    """按收益排序的建议动作：无卡·高频 > 无卡 > 有卡但弱 > 有未解决错题。"""
    actions = []
    prio = {"无卡·高频": 0, "无卡": 1, "有卡但弱": 2}
    for it in sorted(items, key=lambda x: (prio.get(x["gap"], 9), -x["freq"])):
        if it["gap"] in prio:
            actions.append({
                "priority": len(actions) + 1, "type": "create_card" if "无卡" in it["gap"] else "review",
                "target": it["text"][:60], "freq": it["freq"], "gap": it["gap"],
                "why": f"面经题（出现{it['freq']}次）当前{it['gap']}"})
        elif it["open_mistakes"]:
            actions.append({"priority": len(actions) + 1, "type": "review",
                            "target": it["text"][:60], "freq": it["freq"],
                            "why": f"有 {it['open_mistakes']} 道未解决错题"})
    return actions[:8]


def evolution_report() -> dict:
    """项目进化方向：覆盖全景 + 高频无卡题 + 薄弱卡排行 + 建议队列。"""
    conn = get_conn()
    try:
        questions = conn.execute(
            "SELECT id, text, topic, freq_count, qtype FROM question_pool "
            "WHERE merged_into IS NULL ORDER BY freq_count DESC LIMIT 200").fetchall()
        cards = conn.execute(
            "SELECT id, title, topic, mastery FROM kb_cards WHERE status='active'").fetchall()
    finally:
        conn.close()

    matched, uncarded = [], []
    conn = get_conn()
    try:
        for q in questions:
            card = _match_card(conn, q["text"], q["topic"], q["id"])
            rec = {"qid": q["id"], "text": q["text"], "topic": q["topic"],
                   "freq": q["freq_count"], "card_id": card["id"] if card else None}
            (matched if card else uncarded).append(rec)
    finally:
        conn.close()

    total = len(questions)
    by_topic: dict[str, dict] = {}
    for q in questions:
        t = by_topic.setdefault(q["topic"] or "未分类", {"questions": 0, "freq": 0, "covered": 0})
        t["questions"] += 1
        t["freq"] += q["freq_count"]
    for m in matched:
        t = by_topic.get(next(q["topic"] or "未分类" for q in questions if q["id"] == m["qid"]))
        if t:
            t["covered"] += 1

    weak = sorted(
        ({"id": c["id"], "title": c["title"], "topic": c["topic"], "mastery": c["mastery"]}
         for c in cards if c["mastery"] < WEAK_MASTERY),
        key=lambda x: x["mastery"])[:10]

    high_freq_gaps = [u for u in uncarded if u["freq"] >= 2]
    return {
        "generated_at": _now(),
        "totals": {"questions": total, "cards": len(cards),
                   "coverage_rate": round(len(matched) / total, 3) if total else None},
        "high_freq_uncarded": high_freq_gaps[:15],       # 最高优先级的进化信号
        "uncarded_all": uncarded,
        "topic_coverage": sorted(
            ({"topic": k, **v, "rate": round(v["covered"] / v["questions"], 2) if v["questions"] else 0}
             for k, v in by_topic.items()),
            key=lambda x: (-x["freq"], x["rate"])),
        "weak_cards": weak,
        "actions": [
            {"priority": i + 1, "type": "create_card", "target": u["text"][:60],
             "why": f"面经出现 {u['freq']} 次但无知识卡"}
            for i, u in enumerate(high_freq_gaps[:8])
        ],
    }


def flywheel_metrics() -> dict:
    """飞轮四指标：给 Dashboard。预测命中率 = 作战地图预测 vs 模拟面试实际档位。"""
    conn = get_conn()
    try:
        n_interviews = conn.execute("SELECT COUNT(*) n FROM feed_interviews").fetchone()["n"]
        n_questions = conn.execute("SELECT COUNT(*) n FROM question_pool WHERE merged_into IS NULL").fetchone()["n"]
        n_cards = conn.execute("SELECT COUNT(*) n FROM kb_cards WHERE status='active'").fetchone()["n"]
        n_outcomes = conn.execute("SELECT COUNT(*) n FROM interview_outcomes").fetchone()["n"]

        # 覆盖率：用最近的 coverage_reports 统计
        rows = conn.execute("SELECT payload_json FROM coverage_reports").fetchall()
        cov_items = [it for r in rows for it in json.loads(r["payload_json"])["items"]]
        covered = sum(1 for it in cov_items if it["card"])
        coverage_rate = round(covered / len(cov_items), 3) if cov_items else None

        # 预测命中率：预测档位 vs 同题模拟面试实际档位
        hits = total = 0
        for r in rows:
            for it in json.loads(r["payload_json"])["items"]:
                ans = conn.execute(
                    "SELECT a.scores_json FROM mock_answers a JOIN mock_sessions s ON s.id=a.session_id "
                    "WHERE a.question_text=? AND a.scores_json IS NOT NULL ORDER BY a.created_at DESC LIMIT 1",
                    (it["text"],)).fetchone()
                if not ans:
                    continue
                s = json.loads(ans["scores_json"])
                avg = sum(s[d] for d in ("correctness", "depth", "structure", "highlight")) / 4
                actual = "high" if avg >= 7 else ("mid" if avg >= 5 else "low")
                total += 1
                hits += (actual == it["predicted_band"])
    finally:
        conn.close()

    # 高频无卡题数（进化的核心信号）
    evo = evolution_report()
    return {
        "interviews": n_interviews, "questions": n_questions, "cards": n_cards,
        "outcomes": n_outcomes,
        "coverage_rate": coverage_rate,
        "high_freq_uncarded": len(evo["high_freq_uncarded"]),
        "prediction_hit_rate": round(hits / total, 3) if total else None,
        "prediction_samples": total,
    }


def record_outcome(payload: dict) -> dict:
    """回录真实面试：结果 + 逐题自评。created 时自动匹配题目池。"""
    conn = get_conn()
    try:
        qs = []
        for q in payload.get("questions", []):
            text = (q.get("text") or "").strip()
            if not text:
                continue
            matched = conn.execute(
                "SELECT id FROM question_pool WHERE text=? AND merged_into IS NULL", (text,)).fetchone()
            qs.append({"text": text, "self_band": q.get("self_band"),
                       "matched_qid": matched["id"] if matched else None})
        cur = conn.execute(
            "INSERT INTO interview_outcomes (company,occurred_on,result,notes,questions_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (payload.get("company"), payload.get("occurred_on"), payload.get("result", "unknown"),
             payload.get("notes"), json.dumps(qs, ensure_ascii=False), _now()))
        conn.commit()
        return {"id": cur.lastrowid, "questions": len(qs)}
    finally:
        conn.close()


def list_outcomes(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM interview_outcomes ORDER BY occurred_on DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"questions": json.loads(r["questions_json"])} for r in rows]
    finally:
        conn.close()
