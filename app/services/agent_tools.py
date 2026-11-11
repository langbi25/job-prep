"""Agent 的"动手"工具：入池、跑地图、起草卡、出计划。

和 harness 里那 6 个只读工具的区别：这里的工具会**改数据**，所以多两道约束：

1. **模式闸门**：`MODE` 上下文变量区分 auto / assist。只查不改模式下写工具直接拒绝，
   而不是悄悄执行——用户切到"只查"是想要安全感，不是想要你偷偷写入。
2. **入池硬闸门**（`auto_pool_gate`）：抽题结果达不到质量线时不自动入池，而是把材料
   停在 extracted 状态、把原因交回给模型转述。2026-10-31 那 12 张重复卡就是
   "抽完直接入池、没人看"的产物，这一层是补上那个缺口。

建卡**故意不在这里自动批准**：动作只进 agent_actions(pending)，
审批永远留在审批中心——这是项目从第一天起的不变量。
"""
import contextvars
import json

from ..core.db import get_conn
from ..core import spec
from . import coverage_service, harness, ingest_service, multi_agent

MODE = contextvars.ContextVar("agent_mode", default="auto")

MIN_QUESTIONS = 1
MAX_QUESTIONS = 40          # 超过这个数基本是把整页导航/评论都当题抽了
MIN_TEXT_LEN = 4            # "RAG?" 这种太短的题面抽出来没法建卡


def _writable() -> bool:
    return MODE.get() == "auto"


def _denied(tool: str) -> dict:
    return {"ok": False, "error": f"当前是「只查不改」模式，{tool} 未执行。"
                                  "如需真正执行，请让用户把模式切到「自动执行」。"}


def auto_pool_gate(extracted: dict) -> dict:
    """入池硬闸门：返回 {ok, reasons, n, doc_type}。

    只在"结构性可疑"时拦人——题少、题面极短、题数异常多、缺材料类型判定。
    不靠 LLM 自评分：模型对自己抽得准不准没有可靠判断，这些是可验证的事实。
    """
    qs = [q for q in (extracted.get("questions") or []) if isinstance(q, dict)]
    reasons: list[str] = []
    if len(qs) < MIN_QUESTIONS:
        reasons.append("没抽出任何题目")
    if len(qs) > MAX_QUESTIONS:
        reasons.append(f"抽出 {len(qs)} 道题，疑似把整页噪声当题目")
    short = [q for q in qs if len((q.get("text") or "").strip()) < MIN_TEXT_LEN]
    if short:
        reasons.append(f"{len(short)} 道题面过短（不足 {MIN_TEXT_LEN} 字）")
    judgement = extracted.get("judgement") or {}
    if not judgement.get("doc_type"):
        reasons.append("缺少材料类型判定，抽取可能不完整")
    return {"ok": not reasons, "reasons": reasons, "n": len(qs),
            "doc_type": judgement.get("doc_type"),
            "n_complete": judgement.get("n_complete", 0)}


def _ingest_text(args: dict) -> dict:
    """投喂一段面经原文：存档 → 抽取 → 过闸门 → 自动入池 → 起草卡。"""
    if not _writable():
        return _denied("投喂入池")
    text = str(args.get("text") or "").strip()
    if len(text) < 20:
        return {"ok": False, "error": "原文太短（<20 字），不像一份面经；请确认是否粘贴完整"}

    ing = ingest_service.create_ingest(str(args.get("source_type") or "paste"), text=text)
    ingest_id = ing["id"]
    try:
        extracted = ingest_service.run_extract(ingest_id)
    except Exception as e:  # noqa: BLE001  抽取失败要如实回报，不能当成功
        return {"ok": False, "stage": "extract", "ingest_id": ingest_id,
                "error": f"抽取失败：{e}"}

    gate = auto_pool_gate(extracted)
    head = {
        "ok": True, "ingest_id": ingest_id, "stage": "extracted",
        "doc_type": gate["doc_type"], "n_questions": gate["n"],
        "n_complete": gate["n_complete"],
        "questions": [str(q.get("text") or "")[:40] for q in (extracted.get("questions") or [])][:8],
    }
    if not gate["ok"]:
        return head | {"auto_pooled": False, "hold_reasons": gate["reasons"],
                       "next": "材料已存档并停在 extracted，等人工确认后才能入池"}

    try:
        # 显式把刚抽到的题带过去，不依赖"extracted_json 一定已落库"这个隐含前提：
        # confirm_ingest 的兜底是在 payload 为空时读 extracted_json，
        # 一旦那一步写失败（或抽取走了别的路径），就会安静地建出 0 题的空壳面经。
        payload = {"questions": extracted.get("questions") or []}
        for k in ("company", "position", "round", "interview_date", "direction"):
            if extracted.get(k):
                payload[k] = extracted[k]
        res = ingest_service.confirm_ingest(ingest_id, payload)
    except Exception as e:  # noqa: BLE001
        return head | {"auto_pooled": False, "error": f"入池失败：{e}"}

    if not res.get("questions_kept"):
        return head | {"auto_pooled": False,
                       "error": "入池后题目数为 0，材料可能没解析出可用题目",
                       "interview_id": res.get("interview_id")}

    draft = {"drafted": 0, "failed": 0}
    qids = res.get("draft_question_ids") or []
    if qids and args.get("draft_cards", True):
        cap = max(1, min(int(args.get("max_cards") or 2), 3))
        try:
            d = multi_agent.draft_cards(qids[:cap], limit=cap, low_risk=True)
            draft = {"drafted": d.get("inserted", 0), "failed": len(d.get("failed") or []),
                     "titles": [x["title"][:30] for x in d.get("drafted") or []]}
        except Exception as e:  # noqa: BLE001  卡没起草算部分成功，不影响入池结论
            draft = {"drafted": 0, "failed": 0, "error": str(e)[:120]}

    return head | {
        "auto_pooled": True,
        "interview_id": res.get("interview_id"),
        "questions_kept": res.get("questions_kept"),
        "answers_saved": res.get("answers_saved"),
        "cards_drafted": draft,
        "next": "卡已进审批队列（需要人批），原文已归档到 inbox/archived",
    }


def _confirm_ingest(args: dict) -> dict:
    """人工放行的入池：闸门拦下的材料，用户说"收"之后再走这一步。"""
    if not _writable():
        return _denied("确认入池")
    ingest_id = str(args.get("ingest_id") or "").strip()
    if not ingest_id:
        return {"ok": False, "error": "需要 ingest_id"}
    try:
        res = ingest_service.confirm_ingest(ingest_id, {})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    qids = res.get("draft_question_ids") or []
    drafted = 0
    if qids:
        try:
            d = multi_agent.draft_cards(qids[:2], limit=2, low_risk=True)
            drafted = d.get("inserted", 0)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "interview_id": res.get("interview_id"),
            "questions_kept": res.get("questions_kept"),
            "answers_saved": res.get("answers_saved"),
            "cards_drafted": drafted}


def _analyze_coverage(args: dict) -> dict:
    """跑一次作战地图（逐题覆盖/预测档位/缺口）。不给 id 就用最近一场面经。"""
    if not _writable():
        return _denied("生成作战地图")
    conn = get_conn()
    try:
        iv = str(args.get("interview_id") or "").strip()
        if not iv:
            row = conn.execute(
                "SELECT id FROM feed_interviews ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                return {"ok": False, "error": "还没有面经，先投喂一份"}
            iv = row["id"]
    finally:
        conn.close()
    try:
        payload = coverage_service.analyze_interview(iv)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    s = payload.get("summary") or {}
    return {"ok": True, "interview_id": iv,
            "company": payload.get("company"), "position": payload.get("position"),
            "questions": s.get("questions"),
            "expected_score": s.get("expected_score"),
            "band_dist": s.get("band_dist"),
            "gaps": [g["text"][:40] for g in (s.get("gaps") or [])][:5],
            "n_gaps": len(s.get("gaps") or [])}


def _draft_cards(args: dict) -> dict:
    """为题目池里的题起草知识卡（进审批队列）。"""
    if not _writable():
        return _denied("起草知识卡")
    qids = [str(x) for x in (args.get("question_ids") or []) if str(x).strip()][:3]
    cap = max(1, min(int(args.get("limit") or 2), 3))
    try:
        d = multi_agent.draft_cards(qids or None, limit=cap,
                                    only_uncarded=bool(args.get("only_uncarded", True)),
                                    low_risk=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "drafted": d.get("inserted", 0),
            "failed": len(d.get("failed") or []),
            "titles": [x["title"][:30] for x in d.get("drafted") or []],
            "next": "等人在审批中心批准后才会成为知识卡"}


def _make_study_plan(args: dict) -> dict:
    """让学习教练 Agent 跑一轮（它自己再调只读工具查数据），产出今日计划。"""
    if not _writable():
        return _denied("生成学习计划")
    direction = str(args.get("direction") or spec.default())
    res = harness.run_study_plan_agent(direction)
    if res.get("status") != "done":
        return {"ok": False, "status": res.get("status"),
                "error": res.get("error") or "未产出计划", "run_id": res.get("run_id")}
    plan = res["plan"]
    return {"ok": True, "run_id": res.get("run_id"),
            "summary": plan.get("summary"),
            "total_minutes": plan.get("total_minutes"),
            "items": [{"priority": it.get("priority"), "action": it.get("action"),
                       "minutes": it.get("minutes")} for it in (plan.get("plan") or [])][:6]}


def _list_recent_ingests(args: dict) -> list:
    """最近的采集记录——回答"刚才那份处理到哪了"。"""
    limit = max(1, min(int(args.get("limit") or 5), 10))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, status, source_type, created_at, "
            "CASE WHEN extracted_json IS NULL THEN NULL ELSE json_extract(extracted_json,'$.judgement.doc_type') END AS doc_type "
            "FROM ingests ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _list_pending_actions(args: dict) -> list:
    """待审批动作（Agent 只能提醒，不能代批）。"""
    limit = max(1, min(int(args.get("limit") or 10), 20))
    rows = multi_agent.list_actions("pending")[:limit]
    out = []
    for a in rows:
        p = a.get("payload") or {}
        out.append({"id": a["id"], "kind": a["kind"],
                    "title": p.get("title") or p.get("card_id") or p.get("question_id"),
                    "low_risk": bool(p.get("low_risk")),
                    "reason": (a.get("reason") or "")[:60]})
    return out


SCHEMAS = [
    {"type": "function", "function": {
        "name": "ingest_text",
        "description": "投喂一份面经原文：存档→抽取题目→过质量闸门→自动入池→起草待审卡片。"
                       "用户粘贴面经、说'把这段收进去'时调用。返回 ingest_id / 题目数 / 是否自动入池 / 待审卡数。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "面经原文全文，不要截断、不要总结"},
            "source_type": {"type": "string", "enum": ["paste", "url", "manual"], "description": "来源类型，粘贴用 paste"},
            "max_cards": {"type": "integer", "description": "自动起草几张卡，默认 2，最多 3"},
            "draft_cards": {"type": "boolean", "description": "入池后是否顺手起草卡，默认 true"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "confirm_ingest",
        "description": "对已被质量闸门拦下、停在 extracted 状态的材料执行入池。只有用户明确说'收/确认入池'时才调用。",
        "parameters": {"type": "object", "properties": {
            "ingest_id": {"type": "string", "description": "采集 id，形如 ing-20261104-231500-1234"}},
            "required": ["ingest_id"]}}},
    {"type": "function", "function": {
        "name": "analyze_coverage",
        "description": "生成/刷新一份面经的作战地图：逐题覆盖、预测档位、缺口清单。不给 id 就用最近一场。",
        "parameters": {"type": "object", "properties": {
            "interview_id": {"type": "string", "description": "面经 id，形如 2026-11-0002"}}}}},
    {"type": "function", "function": {
        "name": "draft_cards",
        "description": "为题目池里的题起草知识卡，产出待审批动作（不会直接成为卡片）。",
        "parameters": {"type": "object", "properties": {
            "question_ids": {"type": "array", "items": {"type": "string"}, "description": "题目 id 列表，最多 3 个；不传则按高频无卡题挑"},
            "limit": {"type": "integer", "description": "最多起草几张，默认 2"},
            "only_uncarded": {"type": "boolean", "description": "只挑还没有卡的题，默认 true"}}}}},
    {"type": "function", "function": {
        "name": "make_study_plan",
        "description": "跑一轮学习教练 Agent，产出今天的学习计划（它会自己查到期卡/错题/薄弱主题）。",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": spec.enum()}}}}},
    {"type": "function", "function": {
        "name": "list_recent_ingests",
        "description": "最近几次采集的状态，用于回答'刚才那份处理到哪了'。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "默认 5"}}}}},
    {"type": "function", "function": {
        "name": "list_pending_actions",
        "description": "待审批的 Agent 动作（建卡/合并/归档/提前复习）。用于提醒用户去审批中心。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "默认 10"}}}}},
]

IMPL = {
    "ingest_text": _ingest_text,
    "confirm_ingest": _confirm_ingest,
    "analyze_coverage": _analyze_coverage,
    "draft_cards": _draft_cards,
    "make_study_plan": _make_study_plan,
    "list_recent_ingests": _list_recent_ingests,
    "list_pending_actions": _list_pending_actions,
}

WRITE_TOOLS = {"ingest_text", "confirm_ingest", "analyze_coverage", "draft_cards", "make_study_plan"}


def json_safe(obj, limit: int = 2600) -> str:
    """工具结果转字符串给模型：超长截断并标注，避免上下文被一份大 JSON 撑爆。"""
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + f"…（已截断，原长 {len(s)} 字）"
