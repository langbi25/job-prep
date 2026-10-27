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

ALLOWED_KINDS = {"create_card", "boost_srs"}


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

    system, user, _ = prompts.render(
        "remediation_plan", report_summary=report_summary, weak_data=weak_data, cards=cards_text)
    msg = llm._chat_json(system, user)
    data = json.loads(msg["content"])

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
                    "tags": ["Agent建议"],
                    "source_type": "agent",
                })
                result = {"card_id": card["id"]}
            elif row["kind"] == "boost_srs":
                cid = payload.get("card_id")
                cur = conn.execute("UPDATE kb_cards SET srs_due=? WHERE id=?",
                                   (datetime.date.today().isoformat(), cid))
                if cur.rowcount == 0:
                    raise ValueError(f"卡片不存在: {cid}")
                result = {"boosted": cid}
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
