"""知识库分层视图 API：把 L0~L3 的分层架构变成前端可点进去的树。

    L0 领域 → L1 主题 → L2 知识卡 → L3 题目池 → 面经出处

与 /api/rag/board 的分工：board 回答「检索质量怎么样」，这里回答「每一层都有
什么数据、数据之间怎么连、点进去能看到什么」。

题目归层规则（从强到弱，规则透明可解释，不搞黑盒相似度）：
1. card_question_links 已关联卡 → 跟卡走（关系表是一等公民，2026-11-06 起）；
2. 未关联但题面 topic 与某主题同名 → 归入该主题；
3. 都没有 → 「未归类」桶。这个桶不是垃圾箱，就是覆盖缺口清单本身。

related_card_ids_json 只做兜底镜像（迁移前的老数据），读到的边标记 legacy。
"""
import json

from fastapi import APIRouter

from ..core.db import get_conn
from ..core import spec

router = APIRouter(prefix="/api/kb", tags=["kb"])

MISC_DOMAIN = "_misc"
MISC_TOPIC = "_none"


def _label(domain: str) -> str:
    # 业务线中文名来自 spec；未知域（_misc / cs 等）回退原值。
    return spec.label(domain, fallback=domain)


@router.get("/tree")
def kb_tree():
    conn = get_conn()
    try:
        c = conn.cursor()

        # ---------- L2 卡 ----------
        cards: list[dict] = []
        q_count_by_card: dict[str, int] = {}
        for r in c.execute(
                "SELECT card_id, COUNT(*) AS n FROM card_question_links GROUP BY card_id"):
            q_count_by_card[r["card_id"]] = r["n"]
        for r in c.execute(
                "SELECT id, domain, topic, title, status, mastery, review_count, updated_at, "
                "points_json FROM kb_cards ORDER BY domain, topic, updated_at DESC"):
            cards.append({
                "id": r["id"], "title": r["title"], "domain": r["domain"], "topic": r["topic"],
                "status": r["status"], "mastery": r["mastery"], "review_count": r["review_count"],
                "updated_at": r["updated_at"],
                "hollow": not json.loads(r["points_json"] or "[]"),
                "linked_questions": q_count_by_card.get(r["id"], 0),
            })

        # ---------- L3 题 + 卡↔题边（表为准，json 兜底标 legacy） ----------
        q_links: dict[str, list[dict]] = {}
        for r in c.execute(
                "SELECT l.question_id, l.card_id, l.match_method, k.title AS card_title, "
                "k.status AS card_status, k.domain AS card_domain, k.topic AS card_topic "
                "FROM card_question_links l JOIN kb_cards k ON k.id = l.card_id"):
            q_links.setdefault(r["question_id"], []).append({
                "card_id": r["card_id"], "card_title": r["card_title"],
                "match_method": r["match_method"], "card_status": r["card_status"],
                "domain": r["card_domain"], "topic": r["card_topic"],
            })
        linked_card_ids = {lk["card_id"] for lks in q_links.values() for lk in lks}
        for r in c.execute(
                "SELECT id, related_card_ids_json FROM question_pool "
                "WHERE related_card_ids_json IS NOT NULL AND related_card_ids_json != '[]'"):
            for cid in json.loads(r["related_card_ids_json"]):
                if cid in linked_card_ids:
                    continue  # 关系表已有，json 只是镜像
                row = c.execute(
                    "SELECT id, title, status, domain, topic FROM kb_cards WHERE id=?", (cid,)).fetchone()
                if row:
                    q_links.setdefault(r["id"], []).append({
                        "card_id": row["id"], "card_title": row["title"],
                        "match_method": "legacy", "card_status": row["status"],
                        "domain": row["domain"], "topic": row["topic"],
                    })

        occ = {r["question_id"]: r["n"] for r in c.execute(
            "SELECT question_id, COUNT(*) AS n FROM question_occurrences GROUP BY question_id")}
        interviews_total = c.execute("SELECT COUNT(*) FROM feed_interviews").fetchone()[0]

        questions: list[dict] = []
        for r in c.execute(
                "SELECT id, text, topic, qtype, freq_count, aliases_json, first_seen_at, last_seen_at "
                "FROM question_pool WHERE merged_into IS NULL "
                "ORDER BY freq_count DESC, last_seen_at DESC"):
            links = q_links.get(r["id"], [])
            questions.append({
                "id": r["id"], "text": r["text"], "topic": r["topic"], "qtype": r["qtype"],
                "freq_count": r["freq_count"], "interview_count": occ.get(r["id"], 0),
                "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
                "aliases": json.loads(r["aliases_json"] or "[]"),
                "linked": links, "orphan": not links,
            })

        # ---------- 组树：题目归层（跟卡走 > 同名主题 > 未归类） ----------
        domains: dict[str, dict] = {}

        def _dom(key: str) -> dict:
            if key not in domains:
                domains[key] = {"domain": key, "label": _label(key) if key != MISC_DOMAIN else "未归类",
                                "topics": {}}
            return domains[key]

        def _topic(dkey: str, tname: str) -> dict:
            d = _dom(dkey)
            if tname not in d["topics"]:
                d["topics"][tname] = {"topic": tname, "cards": [], "questions": []}
            return d["topics"][tname]

        # 主题名 -> 域 的借宿映射（题目未关联卡时按同名主题归层）
        topic_home: dict[str, str] = {}
        for card in cards:
            topic_home.setdefault(card["topic"], card["domain"])

        for card in cards:
            _topic(card["domain"], card["topic"])["cards"].append(card)
        placed: set[str] = set()
        for q in questions:
            if q["linked"]:
                active = [lk for lk in q["linked"] if lk["card_status"] == "active"] or q["linked"]
                lk = active[0]
                _topic(lk["domain"], lk["topic"])["questions"].append(q)
            elif q["topic"] and q["topic"] in topic_home:
                _topic(topic_home[q["topic"]], q["topic"])["questions"].append(q)
            else:
                _topic(MISC_DOMAIN, MISC_TOPIC)["questions"].append(q)
            placed.add(q["id"])

        # ---------- 汇总每层计数 ----------
        out_domains = []
        spec_order = list(spec.lines())
        for key in spec_order + [d for d in domains if d not in spec_order and d != MISC_DOMAIN] + [MISC_DOMAIN]:
            if key not in domains:
                continue
            d = domains[key]
            topics = []
            for tname, t in d["topics"].items():
                topics.append({
                    "topic": "（未归类）" if tname == MISC_TOPIC else tname,
                    "raw_topic": tname,
                    "cards": t["cards"], "questions": t["questions"],
                    "card_count": len(t["cards"]),
                    "active_cards": sum(1 for x in t["cards"] if x["status"] == "active"),
                    "question_count": len(t["questions"]),
                    "orphans": sum(1 for x in t["questions"] if x["orphan"]),
                })
            out_domains.append({
                "domain": key, "label": _label(key) if key != MISC_DOMAIN else "未归类",
                "topics": topics,
                "card_count": sum(t["card_count"] for t in topics),
                "active_cards": sum(t["active_cards"] for t in topics),
                "question_count": sum(t["question_count"] for t in topics),
                "orphans": sum(t["orphans"] for t in topics),
            })

        active_n = sum(1 for x in cards if x["status"] == "active")
        archived_n = len(cards) - active_n
        hollow_n = sum(1 for x in cards if x["hollow"] and x["status"] == "active")
        linked_n = sum(1 for q in questions if not q["orphan"])
        by_method: dict[str, int] = {}
        edge_total = 0
        for q in questions:
            for lk in q["linked"]:
                by_method[lk["match_method"]] = by_method.get(lk["match_method"], 0) + 1
                edge_total += 1

        return {
            "layers": {
                "L0_domains": len([d for d in out_domains if d["domain"] != MISC_DOMAIN]),
                "L1_topics": sum(len(d["topics"]) for d in out_domains if d["domain"] != MISC_DOMAIN),
                "L2_cards": {"total": len(cards), "active": active_n, "archived": archived_n,
                             "hollow": hollow_n},
                "L3_questions": {"total": len(questions), "linked": linked_n,
                                 "orphan": len(questions) - linked_n},
                "links": {"total": edge_total, "by_method": by_method},
                "interviews": interviews_total,
            },
            "domains": out_domains,
        }
    finally:
        conn.close()
