"""RAG 看板 API：把「检索质量」和「知识库结构」变成看得见的东西。

两个接口：
- GET  /api/rag/board   知识库构成 + 最新评测结果（读 evals/results-*.json）+ 图谱数据
- POST /api/rag/eval    现场重评（复用 scripts/run_evals.py 的评测函数，不另写一份逻辑）

设计取舍：
1. 评测结果读文件而不是每次现算：检索评测要跑 19×2 次查询（含 full 档还要 LLM），
   看板应该秒开；重评是显式动作。
2. 图谱不是真「知识图谱」——库里没有实体关系抽取，边只有三种真实存在的关系：
   域→主题→卡（层级）、卡↔卡（related_json，目前只有 2 条）、卡→题（题目池的
   related_card_ids_json，16 题）。看板如实渲染这三种，并在页面标注「这不是图谱，
   是覆盖关系图」——把覆盖率伪装成知识图谱是面试里会被戳穿的点。
"""
import datetime
import importlib.util
import json
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.db import get_conn

router = APIRouter(prefix="/api/rag", tags=["rag"])

_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = _ROOT / "evals"

_eval_lock = threading.Lock()  # 重评不可重入：两并发会写两个 results 文件还互相拖慢


def _load_run_evals():
    """动态加载 scripts/run_evals.py——评测逻辑只有一份，CLI 和看板共用。"""
    spec = importlib.util.spec_from_file_location("run_evals", _ROOT / "scripts" / "run_evals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 知识库构成
def _kb_stats() -> dict:
    conn = get_conn()
    try:
        c = conn.cursor()
        active = c.execute("SELECT COUNT(*) FROM kb_cards WHERE status='active'").fetchone()[0]
        archived = c.execute("SELECT COUNT(*) FROM kb_cards WHERE status='archived'").fetchone()[0]
        questions = c.execute("SELECT COUNT(*) FROM question_pool WHERE merged_into IS NULL").fetchone()[0]
        # 卡↔题边：题目池的 related_card_ids_json（建卡/复盘时写入的真实关联）
        linked = 0
        domains: dict[str, int] = {}
        topics: dict[str, int] = {}
        rel_edges = 0
        for r in c.execute("SELECT domain, topic, related_json FROM kb_cards WHERE status='active'"):
            domains[r["domain"]] = domains.get(r["domain"], 0) + 1
            topics[r["topic"]] = topics.get(r["topic"], 0) + 1
            if (r["related_json"] or "[]") != "[]":
                rel_edges += len(json.loads(r["related_json"]))
        q_linked_ids = set()
        for r in c.execute("SELECT id, related_card_ids_json FROM question_pool WHERE merged_into IS NULL"):
            ids = json.loads(r["related_card_ids_json"] or "[]")
            if ids:
                linked += 1
                q_linked_ids.add(r["id"])
        return {
            "active_cards": active, "archived_cards": archived,
            "questions": questions,
            "questions_linked": linked,
            "coverage": round(linked / questions, 3) if questions else None,
            "card_card_edges": rel_edges,
            "domains": domains,
            "topics": topics,
            "q_linked_ids": sorted(q_linked_ids),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 图谱数据
def _build_graph() -> dict:
    """节点 6 类：domain / topic / card / archived_card / question / orphan_question。

    无卡题（orphan）单独一类不是为了好看：它们就是覆盖缺口本身，看板的核心信息。
    """
    conn = get_conn()
    try:
        c = conn.cursor()
        nodes, edges = [], []
        seen_topic: dict[tuple[str, str], str] = {}

        cards = c.execute(
            "SELECT id, domain, topic, title, status, related_json FROM kb_cards").fetchall()
        for card in cards:
            dom, topic = card["domain"], card["topic"]
            dom_id = f"dom:{dom}"
            if dom_id not in seen_topic:
                seen_topic[dom_id] = "1"
                nodes.append({"id": dom_id, "name": dom, "category": 0, "node_type": "domain"})
            key = (dom, topic)
            if key not in seen_topic:
                tid = f"topic:{dom}:{topic}"
                seen_topic[key] = tid
                nodes.append({"id": tid, "name": topic, "category": 1,
                              "node_type": "topic", "domain": dom})
                edges.append({"source": dom_id, "target": tid})
            is_arch = card["status"] != "active"
            label = (card["title"] or card["id"])[:22]
            nodes.append({
                "id": f"card:{card['id']}", "name": label, "title": card["title"],
                "category": 3 if is_arch else 2, "node_type": "archived_card" if is_arch else "card",
                "card_id": card["id"],
            })
            edges.append({"source": seen_topic[key], "target": f"card:{card['id']}"})
            for rid in json.loads(card["related_json"] or "[]"):
                edges.append({"source": f"card:{card['id']}", "target": f"card:{rid}",
                              "rel": "related"})

        qcards = {f"card:{r[0]}" for r in c.execute("SELECT id FROM kb_cards")}
        for r in c.execute(
                "SELECT id, text, topic, related_card_ids_json FROM question_pool "
                "WHERE merged_into IS NULL"):
            rel = [x for x in json.loads(r["related_card_ids_json"] or "[]") if f"card:{x}" in qcards]
            orphan = not rel
            nodes.append({
                "id": f"q:{r['id']}", "name": (r["text"] or "")[:20],
                "text": r["text"], "category": 5 if orphan else 4, "node_type":
                    "orphan_question" if orphan else "question",
                "question_id": r["id"],
            })
            for cid in rel:
                edges.append({"source": f"card:{cid}", "target": f"q:{r['id']}"})

        return {"nodes": nodes, "edges": edges,
                "categories": ["域", "主题", "知识卡", "归档卡", "已关联题", "无卡题"]}
    finally:
        conn.close()


# ---------------------------------------------------------------- 评测结果
def _latest_eval() -> dict | None:
    files = sorted(EVALS_DIR.glob("results-*.json"))
    if not files:
        return None
    f = files[-1]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    ts = data.get("timestamp", "")
    age_h = None
    try:
        t0 = datetime.datetime.fromisoformat(ts)
        age_h = round((datetime.datetime.now() - t0).total_seconds() / 3600, 1)
    except ValueError:
        pass
    data["file"] = f.name
    data["age_hours"] = age_h
    data["stale"] = bool(age_h and age_h > 72)
    return data


def _thresholds() -> dict:
    p = EVALS_DIR / "thresholds.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


@router.get("/board")
def rag_board():
    return {"kb": _kb_stats(), "eval": _latest_eval(), "graph": _build_graph(),
            "thresholds": _thresholds()}


class EvalReq(BaseModel):
    with_llm: bool = False   # True 才跑 full 档（改写+LLM 重排），慢且花 token


@router.post("/eval")
def rag_eval(req: EvalReq):
    if not _eval_lock.acquire(blocking=False):
        raise HTTPException(409, "已有一次评测在跑，等它结束")
    try:
        re_mod = _load_run_evals()
        from ..core.db import ensure_dirs, init_db
        ensure_dirs()
        init_db()
        modes = ("fts", "hybrid", "full") if req.with_llm else ("fts", "hybrid")
        report = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds")}
        from ..core import llm
        before = llm.stats()["calls"]
        report["search_tool"] = re_mod.eval_search_tool()
        report["retrieval"] = re_mod.eval_retrieval(modes)
        report["llm_calls"] = llm.stats()["calls"] - before
        out = EVALS_DIR / f"results-{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"评测失败：{type(e).__name__}: {e}")
    finally:
        _eval_lock.release()
    return {"kb": _kb_stats(), "eval": _latest_eval(), "graph": _build_graph(),
            "thresholds": _thresholds()}
