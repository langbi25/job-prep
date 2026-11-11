"""复盘中心（P6）：复盘文档 → 考点提炼 → 建题入池 → 建卡走审批。

产品逻辑：把"和 AI 协作改造项目的过程"变成面试资产。对话总结、建设复盘、
踩坑笔记这类 MD 文档里埋着大量真实素材（设计决策、权衡、量化指标），
正是面试官最爱深挖的东西——本模块把"人读完 → 人想出题 → 人写卡"这条手工链
自动化为三步流水线，且每步的产物都能在 UI 看到、可中断、可重跑。

不变量（与全项目一致）：
- 题目可直入池——题池本来就是待办池，面经抽题同款；
- 卡片绝不直写：一律经 multi_agent.draft_cards 产出 create_card(pending)，
  人在审批中心确认后才成为事实源。
- Agent 编排入口 run_pipeline 与 REST 三步接口走同一份实现（工具是 service 薄壳）。
"""
import datetime
import json

from ..core import llm, prompts
from ..core.db import get_conn
from . import multi_agent

VALID_QTYPES = {"八股", "项目深挖", "场景设计", "系统设计", "算法", "开放题"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_retro_id() -> str:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM retrospectives").fetchone()
        n = int(row["n"]) + 1
    finally:
        conn.close()
    return f"retro-{datetime.datetime.now():%Y%m%d}-{n:04d}"


def _get(rid: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM retrospectives WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _update(rid: str, **fields) -> None:
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE retrospectives SET {sets} WHERE id=?",
                     (*fields.values(), rid))
        conn.commit()
    finally:
        conn.close()


# ---------- 1. 导入 ----------

def ingest(content: str, title: str = "", source_path: str | None = None) -> dict:
    """导入一份复盘文档。content 太短直接拒绝——提炼不出考点的材料不值得进流水线。"""
    content = (content or "").strip()
    if len(content) < 200:
        raise ValueError(f"复盘内容太短（{len(content)} 字 < 200）：至少写清楚背景、做了什么、关键决策")
    rid = _new_retro_id()
    title = (title or "").strip() or (content.strip().splitlines()[0].lstrip("# ")[:60])
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO retrospectives (id,title,source_path,content,status,created_at,updated_at) "
            "VALUES (?,?,?,?, 'ingested', ?,?)",
            (rid, title[:120], source_path, content, _now(), _now()))
        conn.commit()
    finally:
        conn.close()
    return {"retro_id": rid, "title": title[:120], "chars": len(content)}


# ---------- 2. 考点提炼 ----------

def extract_points(rid: str) -> dict:
    """LLM 提炼面试考点。同一份文档可重跑（覆盖旧考点，状态回退重走）。"""
    retro = _get(rid)
    if not retro:
        raise ValueError(f"复盘不存在: {rid}")
    system, user, _ver = prompts.render("retro_extract", title=retro["title"],
                                        content=retro["content"][:12000])
    data = llm.chat_parsed(system, user)
    points = []
    for p in (data.get("points") or []):
        if not isinstance(p, dict):
            continue
        point = str(p.get("point") or "").strip()
        if not point:
            continue
        points.append({"point": point[:120],
                       "why": str(p.get("why") or "").strip()[:200],
                       "ask": str(p.get("ask") or "").strip()[:200],
                       "topic": str(p.get("topic") or "复盘").strip()[:20]})
    if not points:
        raise RuntimeError("考点提炼结果为空：文档可能全是流水账，没有可面试的素材")
    _update(rid, points_json=json.dumps(points, ensure_ascii=False), status="extracted",
            error=None)
    return {"retro_id": rid, "points": points}


# ---------- 3. 考点 → 题目 → 入池 ----------

def build_questions(rid: str, per_point: int = 1) -> dict:
    """考点清单出题并入题池。与池中已有题（含别名）完全相同的跳过。"""
    from .ingest_service import _next_question_id

    retro = _get(rid)
    if not retro:
        raise ValueError(f"复盘不存在: {rid}")
    points = json.loads(retro["points_json"] or "[]")
    if not points:
        raise ValueError("还没有考点，先调 extract_points")
    per_point = max(1, min(int(per_point), 3))

    system, user, _ver = prompts.render("retro_questions",
                                        points_json=json.dumps(points, ensure_ascii=False))
    data = llm.chat_parsed(system, user)

    today = _now()[:10]
    conn = get_conn()
    try:
        # 已有题面集合（含别名），精确去重
        existing = set()
        for r in conn.execute("SELECT text, aliases_json FROM question_pool"):
            existing.add(r["text"].strip())
            try:
                existing.update(a.strip() for a in json.loads(r["aliases_json"] or "[]"))
            except Exception:  # noqa: BLE001
                pass
        inserted, skipped = [], []
        for q in (data.get("questions") or []):
            if not isinstance(q, dict):
                continue
            text = str(q.get("text") or "").strip()
            if not text or len(text) < 6:
                continue
            if text in existing:
                skipped.append(text[:50])
                continue
            qtype = str(q.get("qtype") or "项目深挖")
            if qtype not in VALID_QTYPES:
                qtype = "项目深挖"
            qid = _next_question_id(conn)
            conn.execute(
                "INSERT INTO question_pool (id,text,aliases_json,qtype,topic,freq_count,"
                "first_seen_at,last_seen_at) VALUES (?,?,?,?,?,1,?,?)",
                (qid, text, json.dumps([], ensure_ascii=False), qtype,
                 str(q.get("topic") or "复盘")[:20], today, today))
            existing.add(text)
            inserted.append({"question_id": qid, "text": text,
                             "qtype": qtype, "topic": q.get("topic") or "复盘",
                             "point": str(q.get("point") or "")[:120]})
        conn.commit()
    finally:
        conn.close()

    if not inserted:
        raise RuntimeError("没有新题可入池（全部与题池重复）")
    _update(rid, questions_json=json.dumps(inserted, ensure_ascii=False),
            question_ids_json=json.dumps([x["question_id"] for x in inserted],
                                         ensure_ascii=False),
            status="built", error=None)
    return {"retro_id": rid, "inserted": inserted, "skipped": len(skipped)}


# ---------- 4. 题目 → 卡草稿（走审批） ----------

def build_cards(rid: str, limit: int = 3) -> dict:
    """为本次复盘入池的题起草知识卡。复用 multi_agent.draft_cards——
    卡内容生成与审批闸门不在这里分叉，产物是 create_card(pending)。"""
    retro = _get(rid)
    if not retro:
        raise ValueError(f"复盘不存在: {rid}")
    qids = json.loads(retro["question_ids_json"] or "[]")
    if not qids:
        raise ValueError("还没有入池的题目，先调 build_questions")
    limit = max(1, min(int(limit), len(qids)))
    d = multi_agent.draft_cards(qids[:limit], limit=limit, only_uncarded=True,
                                low_risk=True)
    drafted = [{"action_id": x.get("action_id"), "title": x.get("title", "")[:60]}
               for x in (d.get("drafted") or [])]
    cards_json = json.loads(retro["cards_json"] or "[]")
    cards_json.extend(drafted)
    _update(rid, cards_json=json.dumps(cards_json, ensure_ascii=False),
            status="cards_done" if drafted else retro["status"], error=None)
    return {"retro_id": rid, "drafted": drafted,
            "failed": d.get("failed") or [], "inserted": d.get("inserted", 0),
            "next": "草稿在审批中心（create_card/pending），批准后才成为知识卡"}


# ---------- 5. Agent 编排入口（REST 三步接口的串行版） ----------

def run_pipeline(content: str, title: str = "", source_path: str | None = None,
                 build_cards_flag: bool = True, card_limit: int = 2) -> dict:
    """一条龙：导入 → 提炼 → 建题 → 建卡（审批）。Agent 工具 ingest_retrospective 调这个。

    中途失败不回滚已完成的步骤（题目入池是好事，卡草稿失败下次重跑即可），
    失败原因记入 retrospectives.error，前端可见。
    """
    ing = ingest(content, title=title, source_path=source_path)
    rid = ing["retro_id"]
    out = {"retro_id": rid, "title": ing["title"], "chars": ing["chars"]}
    try:
        ext = extract_points(rid)
        out["points"] = len(ext["points"])
        bq = build_questions(rid)
        out["questions"] = len(bq["inserted"])
        out["skipped_dup"] = bq["skipped"]
        if build_cards_flag:
            bc = build_cards(rid, limit=card_limit)
            out["cards_drafted"] = len(bc["drafted"])
            out["cards_failed"] = len(bc["failed"] or [])
            out["next"] = bc["next"]
        else:
            out["next"] = "题已入池；要建卡可在复盘中心点「起草知识卡」或调 build_cards"
        return out
    except Exception as e:  # noqa: BLE001
        _update(rid, error=f"{type(e).__name__}: {e}")
        out["error"] = str(e)
        out["done_steps"] = "导入完成；后续步骤失败见 error（可按步重跑）"
        return out


# ---------- 查询 ----------

def list_docs() -> list[dict]:
    """docs/ 下的 MD 文档清单（复盘中心「从项目文档导入」的数据源）。"""
    from ..core import config
    out = []
    for p in sorted((config.BASE_DIR / "docs").rglob("*.md")):
        out.append({"path": p.relative_to(config.BASE_DIR).as_posix(),
                    "title": p.stem, "chars": p.stat().st_size})
    return out


def ingest_from_doc(path: str) -> dict:
    """按相对路径读项目内文档并导入（前端选文件 / Agent 工具共用）。

    安全面：只允许项目根目录内的 .md，拒绝越界路径。
    """
    from pathlib import Path

    from ..core import config
    p = Path(path)
    if not p.is_absolute():
        p = config.BASE_DIR / p
    p = p.resolve()
    if config.BASE_DIR.resolve() not in p.parents:
        raise ValueError(f"只允许导入项目目录内的文档: {p}")
    if not p.exists() or p.suffix.lower() != ".md":
        raise ValueError(f"文档不存在或不是 .md: {p}")
    return ingest(p.read_text(encoding="utf-8"), title=p.stem,
                  source_path=p.relative_to(config.BASE_DIR).as_posix())

def list_retrospectives(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,title,source_path,status,length(content) AS chars,created_at,updated_at "
            "FROM retrospectives ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_retrospective(rid: str) -> dict:
    retro = _get(rid)
    if not retro:
        raise ValueError(f"复盘不存在: {rid}")
    retro["points"] = json.loads(retro.pop("points_json") or "[]")
    retro["questions"] = json.loads(retro.pop("questions_json") or "[]")
    retro["question_ids"] = json.loads(retro.pop("question_ids_json") or "[]")
    retro["cards"] = json.loads(retro.pop("cards_json") or "[]")
    return retro
