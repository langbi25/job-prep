"""项目进化环：面试题 → 项目改造方案 → 真实实施 → 实战复盘回灌知识库。

为什么要有这一环：
- 建卡只是"知道答案"，而面试官考的是"你做过没有"；
- 所以把高频无卡题变成一份**贴着自己项目**的改造方案，去真实动手做，
  做完把经历写成实战卡 + 方案文档，卡片由此获得深度，简历也有了新素材。

三段分离（避免互相污染）：
- 卡片 points：这道题的通用正确答案（可复用于任何项目）
- 改造方案：针对本项目的一次性落地计划（不进知识库，存 improvement_plans）
- 实战卡：做完之后的复盘（进知识库，与原卡 related 互链）
"""
import datetime
import json
import random

from ..core import config, llm, prompts
from ..core.db import get_conn

PROFILE_PATH = config.DATA_DIR / "project_profile.md"
PLANS_DIR = config.BASE_DIR / "docs" / "plans"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_plan_id() -> str:
    return f"plan-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(100, 999)}"


# ---------- 项目档案 ----------

def get_profile() -> dict:
    text = PROFILE_PATH.read_text(encoding="utf-8") if PROFILE_PATH.exists() else ""
    return {"profile": text, "chars": len(text), "path": str(PROFILE_PATH.relative_to(config.BASE_DIR))}


def save_profile(text: str) -> dict:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(text or "", encoding="utf-8")
    return get_profile()


# ---------- 方案生成 ----------

def draft_plan(question_id: str, focus: str = "") -> dict:
    """为一个题目生成项目改造方案。方案正文同时落 docs/plans/*.md，可直接拿去实施。"""
    profile = (PROFILE_PATH.read_text(encoding="utf-8") if PROFILE_PATH.exists() else "").strip()
    if not profile:
        raise ValueError("项目档案为空：先在页面里填写你的项目（技术栈/模块/数据量/痛点），方案才有针对性")

    conn = get_conn()
    try:
        q = conn.execute(
            "SELECT * FROM question_pool WHERE id=? AND merged_into IS NULL", (question_id,)).fetchone()
        if not q:
            raise ValueError(f"题目不存在: {question_id}")
        from .coverage_service import _match_card

        card = _match_card(conn, q["text"], q["topic"])
        card_points = "（无相关卡片）"
        if card:
            row = conn.execute("SELECT points_json FROM kb_cards WHERE id=?", (card["id"],)).fetchone()
            pts = json.loads(row["points_json"])[:9] if row else []
            card_points = f"{card['title']}（掌握度 {card.get('mastery')}）\n" + "\n".join(f"- {p}" for p in pts)
    finally:
        conn.close()

    system, user, version = prompts.render(
        "project_plan", question=q["text"], topic=q["topic"] or "未标注", qtype=q["qtype"] or "未标注",
        profile=profile[:4000], card_points=card_points[:2500],
        focus_hint=(f"【用户补充的侧重点】{focus}" if focus else ""))
    data = llm.chat_parsed(system, user)

    steps = [s for s in (data.get("steps") or []) if isinstance(s, dict) and s.get("title")]
    if len(steps) < 2:
        raise RuntimeError(f"方案步骤过少（{len(steps)} 步），请重试或补充项目档案")

    # 模型输出字段会漂移：outline 的键可能叫 chapter/name/title、what/desc/content，
    # 也可能整条就是个字符串（"1. 改造背景：..."），都要能接住
    outline = []
    for o in (data.get("doc_outline") or []):
        if isinstance(o, str):
            head, _, tail = o.partition("：") if "：" in o else o.partition(":")
            outline.append({"chapter": head.strip("0123456789. 、)")[:40], "what": tail.strip()[:120]})
            continue
        if not isinstance(o, dict):
            continue
        chapter = o.get("chapter") or o.get("name") or o.get("title") or ""
        what = o.get("what") or o.get("desc") or o.get("content") or o.get("detail") or ""
        if chapter:
            outline.append({"chapter": str(chapter)[:40], "what": str(what)[:120]})
    if not outline:
        outline = [
            {"chapter": "改造背景", "what": "这个能力缺口的来源（面试题 + 项目痛点）"},
            {"chapter": "方案选型", "what": "为什么这么改，与其他做法对比"},
            {"chapter": "实施过程", "what": "按步骤记录改了什么、遇到什么"},
            {"chapter": "踩坑记录", "what": "真实故障与修复过程"},
            {"chapter": "效果对比", "what": "改造前后的量化指标"},
            {"chapter": "后续规划", "what": "下一轮要补的能力"},
        ]

    payload = {
        "question": q["text"],
        "topic": q["topic"],
        "card_id": card["id"] if card else None,
        "goal": (data.get("goal") or "").strip()
        or f"在项目里落地「{q['topic'] or q['text'][:20]}」能力：{steps[0]['title']}",
        "current_state": [str(x) for x in (data.get("current_state") or [])][:6],
        "steps": steps[:8],
        "acceptance": [str(x) for x in (data.get("acceptance") or [])][:6],
        "metrics": [str(x) for x in (data.get("metrics") or [])][:5],
        "resume_bullets": [str(x) for x in (data.get("resume_bullets") or [])][:4],
        "interview_narrative": data.get("interview_narrative") or "",
        "doc_outline": outline,
        "tradeoffs": [str(x) for x in (data.get("tradeoffs") or [])][:5],
        "prompt_version": version,
    }

    pid = _new_plan_id()
    doc_rel = f"docs/plans/{pid}-{question_id}.md"
    doc_abs = config.BASE_DIR / doc_rel
    doc_abs.parent.mkdir(parents=True, exist_ok=True)
    doc_abs.write_text(_render_doc(payload, q, pid), encoding="utf-8")

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO improvement_plans (id,question_id,card_id,status,payload_json,doc_path,created_at) "
            "VALUES (?,?,?,'pending',?,?,?)",
            (pid, question_id, payload["card_id"], json.dumps(payload, ensure_ascii=False), doc_rel, _now()))
        conn.commit()
    finally:
        conn.close()
    return {"id": pid, "doc_path": doc_rel, "payload": payload}


def _render_doc(p: dict, q, pid: str) -> str:
    """方案 Markdown：留出「实施记录」「效果对比」「总结」空档，供用户边做边填。"""
    steps = "\n".join(
        f"### {i}. {s.get('title', '')}\n"
        f"- 改什么：{s.get('change', '')}\n"
        f"- 为什么：{s.get('why', '')}\n"
        f"- 工作量：{s.get('effort', '')}｜风险：{s.get('risk', '')}\n"
        for i, s in enumerate(p.get("steps", []), 1))
    outline = "\n".join(f"{i}. **{o.get('chapter', '')}** —— {o.get('what', '')}"
                        for i, o in enumerate(p.get("doc_outline", []), 1))
    return f"""# 改造方案：{q['text'][:60]}

> 计划 ID：{pid}｜题目：{q['id']}｜生成时间：{_now()}
> 本文件是"实施方案"，边做边补「实施记录」；做完后填「效果对比」与「总结」，
> 再回到系统里用「回灌实战经验」把这轮经历沉淀成实战卡片。

## 一、目标

{p.get('goal', '')}

## 二、项目现状推断

""" + "\n".join(f"- {x}" for x in p.get("current_state", [])) + f"""

## 三、实施步骤

{steps}

## 四、验收标准

""" + "\n".join(f"- [ ] {x}" for x in p.get("acceptance", [])) + f"""

## 五、可量化指标

""" + "\n".join(f"- {x}" for x in p.get("metrics", [])) + f"""

## 六、取舍与风险

""" + "\n".join(f"- {x}" for x in p.get("tradeoffs", [])) + f"""

## 七、复盘文档大纲

{outline}

## 八、实施记录（边做边填）

- 实际改动：
- 遇到的问题：
- 与方案的偏差：

## 九、效果对比（改前 → 改后）

| 指标 | 改前 | 改后 | 说明 |
|---|---|---|---|
|  |  |  |  |

## 十、总结

（这段写完后，回系统点「回灌实战经验」，AI 会据此生成实战卡片）
"""


def list_plans(status: str | None = None) -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM improvement_plans"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at DESC LIMIT 50"
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in conn.execute(sql, args)]
    finally:
        conn.close()


def get_plan(plan_id: str) -> dict:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM improvement_plans WHERE id=?", (plan_id,)).fetchone()
        if not r:
            raise ValueError("方案不存在")
        d = dict(r) | {"payload": json.loads(r["payload_json"])}
        doc = config.BASE_DIR / d["doc_path"] if d.get("doc_path") else None
        d["doc_text"] = doc.read_text(encoding="utf-8") if doc and doc.exists() else ""
        return d
    finally:
        conn.close()


def adopt_plan(plan_id: str) -> dict:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE improvement_plans SET status='adopted', adopted_at=? WHERE id=? AND status='pending'",
            (_now(), plan_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("方案不存在或已处理")
        return {"id": plan_id, "status": "adopted"}
    finally:
        conn.close()


# ---------- 回灌：实施笔记 → 实战卡草稿 ----------

def draft_experience(plan_id: str, notes: str) -> dict:
    """把"方案 + 实施笔记"变成实战卡草稿（pending 动作，人工审批后入库并与原卡互链）。"""
    from .multi_agent import _new_action_id

    notes = (notes or "").strip()
    if len(notes) < 30:
        raise ValueError("实施笔记太短（<30 字）：至少写下改了什么、遇到什么、效果如何")
    plan = get_plan(plan_id)
    payload = plan["payload"]

    system, user, version = prompts.render(
        "experience_card", question=payload.get("question", ""),
        plan=json.dumps({k: payload.get(k) for k in ("goal", "steps", "metrics", "acceptance")}, ensure_ascii=False)[:3000],
        notes=notes[:3000])
    data = llm.chat_parsed(system, user)

    points = [str(p).strip() for p in (data.get("points") or []) if str(p).strip()]
    if len(points) < 3:
        raise RuntimeError(f"要点不足以成卡（{len(points)} 条），请补充实施细节")

    q = payload.get("question", "")
    topic = payload.get("topic") or "实战复盘"
    card_payload = {
        "question_id": plan["question_id"],
        "title": (data.get("title") or f"【实战】{q[:40]}")[:200],
        "domain": "project",
        "topic": topic,
        "question": q,
        "points": points[:9],
        "pitfalls": [str(x) for x in (data.get("pitfalls") or [])][:4],
        "tags": [str(t)[:20] for t in (data.get("tags") or [])][:6] + ["实战复盘"],
        "interview_narrative": data.get("interview_narrative") or "",
        "resume_bullets": [str(x) for x in (data.get("resume_bullets") or [])][:3],
        "link_card_id": plan.get("card_id"),
        "plan_id": plan_id,
        "prompt_version": version,
    }
    aid = _new_action_id()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_actions (id,run_id,session_id,kind,payload_json,reason,status,created_at) "
            "VALUES (?,?,?,?,?,?,'pending',?)",
            (aid, None, None, "create_card", json.dumps(card_payload, ensure_ascii=False),
             (data.get("reason") or "实战复盘回灌")[:300], _now()))
        conn.execute("UPDATE improvement_plans SET notes=? WHERE id=?", (notes[:4000], plan_id))
        conn.commit()
    finally:
        conn.close()
    return {"action_id": aid, "title": card_payload["title"], "points": len(points),
            "link_card_id": card_payload["link_card_id"]}
