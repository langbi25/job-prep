"""Multi-Agent 协作 + Human-in-the-loop（阶段 6）。

角色分工（对应你项目的真实三角色）：
- 评分官 Judge：已在 mock_service 里对每道题打分（四维 + 点评）
- 教练 Coach：基于报告与薄弱数据提出补救动作（本模块）
- 审核人 Human：写操作必须人工审批后才落库（HITL）

为什么写操作要审批：Agent 会改写你的知识库和复习计划，
"提议 → 人审 → 执行"是工业级 Agent 处理不可逆操作的标配。
"""
import datetime
import json
import random

from ..core import llm, prompts
from ..core.db import get_conn
from . import kb_service

ALLOWED_KINDS = {"create_card", "boost_srs", "merge_card", "archive_card"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_action_id() -> str:
    return f"act-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(1000, 9999)}"


def draft_remediation(session_id: str) -> dict:
    """教练 Agent：基于面试报告与薄弱数据，起草待审批的补救动作。"""
    conn = get_conn()
    try:
        s = conn.execute("SELECT * FROM mock_sessions WHERE id=?", (session_id,)).fetchone()
        if not s:
            raise ValueError("会话不存在")
        overall = json.loads(s["overall_json"]) if s["overall_json"] else {}
        cards = conn.execute(
            "SELECT id, title, topic, mastery FROM kb_cards WHERE status='active' ORDER BY mastery LIMIT 10"
        ).fetchall()
        weak_data = json.dumps(overall.get("avg", {}), ensure_ascii=False) + "\n" + json.dumps(
            overall.get("report", {}).get("weaknesses", []), ensure_ascii=False)
        cards_text = "\n".join(f"- {c['id']}｜{c['title']}（{c['topic']}，掌握度 {c['mastery']}）" for c in cards)
        report_summary = overall.get("report", {}).get("summary", "") or "（无报告摘要）"
        persona = s["persona"] or ""
    finally:
        conn.close()

    system, user, version = prompts.render(
        "remediation_plan", report_summary=report_summary, weak_data=weak_data, cards=cards_text)
    data = llm.chat_parsed(system, user)

    created = []
    conn = get_conn()
    try:
        for a in data.get("actions", []):
            if a.get("kind") not in ALLOWED_KINDS:
                continue
            aid = _new_action_id()
            conn.execute(
                "INSERT INTO agent_actions (id,run_id,session_id,kind,payload_json,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (aid, None, session_id, a["kind"],
                 json.dumps(a.get("payload", {}), ensure_ascii=False),
                 (a.get("reason") or "")[:300], _now()),
            )
            created.append({"id": aid, "kind": a["kind"], "payload": a.get("payload", {}),
                            "reason": a.get("reason", ""), "status": "pending"})
        conn.commit()
    finally:
        conn.close()
    return {"session_id": session_id, "persona": persona, "actions": created}


def draft_cards(question_ids: list[str] | None = None, limit: int = 3,
                only_uncarded: bool = True, low_risk: bool = False) -> dict:
    """为题目池里的题起草知识卡（LLM），产出 pending 的 create_card 动作，人工审批后落库。

    设计要点：
    - **不直接写卡片**：LLM 写的要点会进复习材料和评分参考答案，错一条就污染后续所有环节，
      所以一律走 HITL——草稿进审批中心，人确认后才成为事实源；
    - 同一道题已有 pending 草稿时跳过，避免重复烧 token；
    - 校验 points 数量与长度，把"要注意性能"这类空话挡在门外。

    low_risk=True 走的是**另一条 prompt**（card_from_answer）：题目池里已经有面经原文抽出的
    答案时，只做结构化与措辞规范化，禁止补充原文没有的技术点。这类草稿 payload 带
    `low_risk=True`，审批页允许勾选批量通过——但**仍然过审批**：answer_points 会被
    context_builder 塞进模拟面试的评分上下文，错一条污染的是整轮打分。
    """
    from ..core import llm, prompts
    from . import coverage_service

    conn = get_conn()
    try:
        if question_ids:
            marks = ",".join("?" * len(question_ids))
            rows = conn.execute(
                f"SELECT * FROM question_pool WHERE id IN ({marks}) AND merged_into IS NULL",
                question_ids).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM question_pool WHERE merged_into IS NULL "
                "ORDER BY freq_count DESC, last_seen_at DESC LIMIT 50").fetchall()
        if not rows:
            raise ValueError("题目不存在")

        pending_qids = set()
        for a in conn.execute("SELECT payload_json FROM agent_actions WHERE kind='create_card' AND status='pending'"):
            try:
                pid = json.loads(a["payload_json"]).get("question_id")
            except Exception:  # noqa: BLE001
                pid = None
            if pid:
                pending_qids.add(pid)

        todo = []
        for q in rows:
            if q["id"] in pending_qids:
                continue
            if only_uncarded and coverage_service._match_card(conn, q["text"], q["topic"], q["id"]):
                continue
            todo.append(q)
            if len(todo) >= limit:
                break
        if not todo:
            return {"drafted": [], "skipped": "没有待建卡的题（可能都已有卡或已有草稿）"}

        card_rows = conn.execute(
            "SELECT title, topic FROM kb_cards WHERE status='active' ORDER BY updated_at DESC LIMIT 40").fetchall()
        existing = "\n".join(f"- {c['title']}（{c['topic']}）" for c in card_rows) or "（知识库为空）"
    finally:
        conn.close()

    created, failed = [], []
    for q in todo:
        # 有原文答案 → 走"规范化"分支（内容不是我编的）；否则从零起草
        answer = ""
        if low_risk:
            try:
                pts = json.loads(q["answer_points_json"] or "[]")
            except (ValueError, TypeError):
                pts = []
            answer = "\n".join(str(p).strip() for p in pts if str(p).strip())
        from_answer = bool(answer)
        try:
            if from_answer:
                try:
                    gaps = json.loads(q["answer_gaps_json"] or "[]")
                except (ValueError, TypeError):
                    gaps = []
                system, user, version = prompts.render(
                    "card_from_answer", question=q["text"], topic=q["topic"] or "未标注",
                    answer=answer[:4000],
                    gaps="\n".join(f"- {g}" for g in gaps) or "（未标注）",
                    existing=existing)
            else:
                system, user, version = prompts.render(
                    "card_draft", question=q["text"], qtype=q["qtype"] or "未标注",
                    topic=q["topic"] or "未标注", freq=q["freq_count"], existing=existing)
            data = llm.chat_parsed(system, user)
        except Exception as e:  # noqa: BLE001
            failed.append({"question_id": q["id"], "error": str(e)[:200]})
            continue

        points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
        points = [p for p in points if len(p) >= 8]          # 挡空话
        pitfalls = [str(p).strip() for p in (data.get("pitfalls") or []) if str(p).strip()]
        # 原文规范化允许要点少（信息量就那么多，不许为凑数编内容）；从零起草仍要求 ≥3
        min_points = 1 if from_answer else 3
        if len(points) < min_points:
            failed.append({"question_id": q["id"], "error": f"要点不足（{len(points)} 条）"})
            continue

        payload = {
            "question_id": q["id"],
            "title": (data.get("title") or q["text"])[:200],
            "domain": data.get("domain") or "cs",
            "topic": data.get("topic") or q["topic"] or "通用",
            "question": q["text"],
            "points": points[:9],
            "pitfalls": pitfalls[:4],
            "tags": [str(t)[:20] for t in (data.get("tags") or [])][:6],
            "prompt_version": version,
            "origin": "extract" if from_answer else "agent",
            "low_risk": from_answer,
            "answer_gaps": [str(g)[:80] for g in (data.get("gaps") or [])][:4],
            "coverage_note": (data.get("coverage_note") or "")[:200] or None,
        }
        aid = _new_action_id()
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO agent_actions (id,run_id,session_id,kind,payload_json,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (aid, None, None, "create_card", json.dumps(payload, ensure_ascii=False),
                 (data.get("reason") or (
                     f"面经答案规范化（原文抽取）{q['id']}" if from_answer
                     else f"补建高频无卡题 {q['id']}"))[:300], _now()))
            conn.commit()
        finally:
            conn.close()
        created.append({"action_id": aid, "question_id": q["id"], "title": payload["title"],
                        "points": len(points), "topic": payload["topic"],
                        "low_risk": from_answer})

    return {"drafted": created, "failed": failed, "inserted": len(created)}


def draft_card_content(card_ids: list[str] | None = None, limit: int = 3) -> dict:
    """给"空壳卡"补正文（LLM 起草 → pending fill_card → 人工审批后写入）。

    为什么要单独一条路径：这些卡已经存在（覆盖统计算"有卡"），但 points 为空——
    它们是**假覆盖**。不能用 create_card 重来（会丢 mastery/related/卡片 id），
    只能原地补内容，所以动作类型是 fill_card 且 decide 时走 update_card_content。
    """
    from ..core import llm, prompts

    conn = get_conn()
    try:
        if card_ids:
            marks = ",".join("?" * len(card_ids))
            rows = conn.execute(
                f"SELECT * FROM kb_cards WHERE id IN ({marks}) AND status='active'",
                card_ids).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kb_cards WHERE status='active' AND (points_json='[]' OR points_json IS NULL) "
                "ORDER BY updated_at ASC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return {"drafted": [], "skipped": "没有空壳卡（所有卡片都有要点）"}

        pending_ids = set()
        for a in conn.execute("SELECT payload_json FROM agent_actions WHERE kind='fill_card' AND status='pending'"):
            try:
                cid = json.loads(a["payload_json"]).get("card_id")
            except Exception:  # noqa: BLE001
                cid = None
            if cid:
                pending_ids.add(cid)

        todo = [c for c in rows if c["id"] not in pending_ids][:limit]
        if not todo:
            return {"drafted": [], "skipped": "空壳卡都已有 pending 草稿（去审批中心处理）"}

        others = conn.execute(
            "SELECT title, topic FROM kb_cards WHERE status='active' AND id NOT IN "
            f"({','.join('?' * len(todo))}) ORDER BY updated_at DESC LIMIT 40",
            [c["id"] for c in todo]).fetchall()
        existing = "\n".join(f"- {c['title']}（{c['topic']}）" for c in others) or "（知识库为空）"
    finally:
        conn.close()

    created, failed = [], []
    for card in todo:
        question = card["question"] or card["title"]
        try:
            system, user, version = prompts.render(
                "card_draft", question=question, qtype="项目/场景题" if card["domain"] == "project" else "未标注",
                topic=card["topic"] or "未标注", freq=1, existing=existing)
            data = llm.chat_parsed(system, user)
        except Exception as e:  # noqa: BLE001
            failed.append({"card_id": card["id"], "error": str(e)[:200]})
            continue

        points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
        points = [p for p in points if len(p) >= 8]
        pitfalls = [str(p).strip() for p in (data.get("pitfalls") or []) if str(p).strip()]
        if len(points) < 3:
            failed.append({"card_id": card["id"], "error": f"要点不足（{len(points)} 条）"})
            continue

        payload = {
            "card_id": card["id"],
            "title": card["title"],
            "domain": card["domain"],
            "topic": card["topic"],
            "question": question,
            "points": points[:9],
            "pitfalls": pitfalls[:4],
            "tags": [str(t)[:20] for t in (data.get("tags") or [])][:6],
            "prompt_version": version,
        }
        aid = _new_action_id()
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO agent_actions (id,run_id,session_id,kind,payload_json,reason,status,created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (aid, None, None, "fill_card", json.dumps(payload, ensure_ascii=False),
                 (data.get("reason") or f"补空壳卡正文 {card['id']}")[:300], _now()))
            conn.commit()
        finally:
            conn.close()
        created.append({"action_id": aid, "card_id": card["id"], "title": card["title"],
                        "points": len(points), "topic": card["topic"]})

    return {"drafted": created, "failed": failed, "inserted": len(created)}


def list_actions(status: str | None = "pending") -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM agent_actions"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at DESC LIMIT 50"
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _archive_card(conn, card_id: str) -> dict:
    """归档卡片：status 置 archived + 删向量 + 同步 md frontmatter。

    两个容易漏的点：
    1) embeddings 表不随 status 走，只改 status 的话归档卡会继续从 hybrid
       检索的向量路里冒出来（FTS 路有 status 过滤，向量路没有）；
    2) md 是事实源，不同步 frontmatter 的话，空库重新 sync 时归档卡会复活。
    """
    row = conn.execute("SELECT file_path FROM kb_cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise ValueError(f"卡片不存在: {card_id}")
    conn.execute("UPDATE kb_cards SET status='archived' WHERE id=?", (card_id,))
    conn.execute("DELETE FROM embeddings WHERE entity_type='card' AND entity_id=?", (card_id,))
    kb_service.update_md_meta(row["file_path"], {"status": "archived"})
    return {"archived": card_id}


def _merge_card(conn, payload: dict) -> dict:
    """合并重复卡：保留卡留下，重复卡归档并指向保留卡。

    只做归档指向（merged_into），不搬运内容——搬运意味着改写保留卡，风险大于收益，
    真要合并内容应该由人在 Obsidian 里改 md（Markdown 是事实源，库里只是索引）。
    掌握度取两者较高值：复习进度不该因为去重而倒退。
    """
    keeper = payload.get("keeper_card_id") or payload.get("card_id")
    dup = payload.get("duplicate_card_id") or payload.get("duplicate_id")
    if not keeper or not dup:
        raise ValueError("merge_card 需要 keeper_card_id 与 duplicate_card_id")
    if keeper == dup:
        raise ValueError("不能把卡片合并给它自己")
    k = conn.execute("SELECT * FROM kb_cards WHERE id=?", (keeper,)).fetchone()
    d = conn.execute("SELECT * FROM kb_cards WHERE id=?", (dup,)).fetchone()
    if not k or not d:
        raise ValueError(f"卡片不存在: keeper={keeper}, duplicate={dup}")
    if d["status"] != "active":
        raise ValueError(f"重复卡不是 active 状态: {dup}（{d['status']}）")
    if k["status"] != "active":
        raise ValueError(f"保留卡不是 active 状态: {keeper}，请先换一张保留卡")

    result = _archive_card(conn, dup)
    conn.execute("UPDATE kb_cards SET merged_into=? WHERE id=?", (keeper, dup))
    conn.execute(
        "UPDATE kb_cards SET mastery=MAX(mastery,?), review_count=review_count+? WHERE id=?",
        (d["mastery"], d["review_count"], keeper))
    kb_service.update_md_meta(d["file_path"], {"status": "archived", "merged_into": keeper})
    return {"merged": dup, "into": keeper,
            "carried_mastery": d["mastery"], "carried_reviews": d["review_count"]}


def decide(action_id: str, approve: bool) -> dict:
    """人工审批。approve=True 执行动作并把结果写回。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM agent_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise ValueError("动作不存在")
        if row["status"] != "pending":
            raise ValueError(f"该动作已处理：{row['status']}")
        payload = json.loads(row["payload_json"])

        if not approve:
            conn.execute("UPDATE agent_actions SET status='rejected', decided_at=? WHERE id=?", (_now(), action_id))
            conn.commit()
            return {"id": action_id, "status": "rejected"}

        try:
            if row["kind"] == "create_card":
                card = kb_service.create_card({
                    "title": payload.get("title") or "未命名卡片",
                    "domain": payload.get("domain") or "cs",
                    "topic": payload.get("topic") or "通用",
                    "question": payload.get("question") or payload.get("title", ""),
                    "points": payload.get("points") or [],
                    "pitfalls": payload.get("pitfalls") or [],
                    "tags": payload.get("tags") or ["Agent建议"],
                    # 来源可追溯：md frontmatter 里写 source: extract 的卡，
                    # 要点来自面经原文答案；source: agent 的是模型从零写的。
                    "source_type": "extract" if payload.get("low_risk") else "agent",
                })
                result = {"card_id": card["id"]}
                # 记死"这张卡是为哪道题建的"——否则覆盖统计只能靠余弦去猜，
                # 措辞一变就显示"审批通过了但还是无卡"。
                qid = payload.get("question_id")
                if qid:
                    try:
                        if kb_service.link_question_card(qid, card["id"]):
                            result["linked_question"] = qid
                    except Exception as e:  # noqa: BLE001
                        result["link_question_error"] = str(e)[:120]
                # 实战卡回灌：与原卡互链，让"通用答案"和"我真的做过"连起来
                link = payload.get("link_card_id")
                if link:
                    try:
                        if kb_service.link_related(card["id"], link):
                            result["linked_to"] = link
                    except Exception as e:  # noqa: BLE001
                        result["link_error"] = str(e)[:120]
            elif row["kind"] == "fill_card":
                # 空壳卡补正文：原地更新，保住卡片 id / mastery / related
                result = kb_service.update_card_content(
                    payload["card_id"],
                    payload.get("points") or [],
                    payload.get("pitfalls"),
                    payload.get("tags") or None,
                )
            elif row["kind"] == "boost_srs":
                cid = payload.get("card_id")
                cur = conn.execute("UPDATE kb_cards SET srs_due=? WHERE id=?",
                                   (datetime.date.today().isoformat(), cid))
                if cur.rowcount == 0:
                    raise ValueError(f"卡片不存在: {cid}")
                result = {"boosted": cid}
            elif row["kind"] == "merge_card":
                result = _merge_card(conn, payload)
            elif row["kind"] == "archive_card":
                result = _archive_card(conn, payload.get("card_id"))
            else:
                raise ValueError(f"未知动作类型: {row['kind']}")
            conn.execute(
                "UPDATE agent_actions SET status='approved', result_json=?, decided_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), _now(), action_id),
            )
            conn.commit()
            return {"id": action_id, "status": "approved", "result": result}
        except Exception as e:  # noqa: BLE001
            conn.execute(
                "UPDATE agent_actions SET status='failed', result_json=?, decided_at=? WHERE id=?",
                (json.dumps({"error": str(e)}, ensure_ascii=False), _now(), action_id),
            )
            conn.commit()
            return {"id": action_id, "status": "failed", "error": str(e)}
    finally:
        conn.close()
