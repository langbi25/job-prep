"""MCP 看板 API：把「谁调了哪个工具、多慢、成不成、选得对不对」变成一屏。

三个接口：
- GET  /api/mcp/board   服务状态 + 工具目录 + 调用统计 + 最近调用 + 最新评测
- POST /api/mcp/eval    现场跑评测（复用 scripts/mcp_eval.py，CLI 与页面共用一份逻辑）
- POST /api/mcp/try     页面「试跑」：按参数执行一次读工具，顺便产一条可观测记录

设计取舍：
1. 统计数据从 `mcp_calls` 现算而不是缓存：几万行以内的聚合是毫秒级，缓存反而引入失效问题。
2. 写工具不允许在页面上试跑：`draft_cards` / `ingest_text` 会写审批队列，看板的试跑按钮
   不该成为第二条生产写入口——要试写操作就去审批中心那条正常流程。
3. 评测结果读文件：选择档要 24 次 LLM 调用，看板必须秒开；重评是显式动作。
"""
import asyncio
import importlib.util
import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.db import get_conn
from ..mcp import gateway
from ..mcp.server import mcp

ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = ROOT / "evals"
_eval_lock = threading.Lock()

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _load_eval_mod():
    spec = importlib.util.spec_from_file_location("mcp_eval", ROOT / "scripts" / "mcp_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def _stats() -> dict:
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM mcp_calls WHERE ts >= date('now')").fetchone()[0]
        ok = conn.execute("SELECT COUNT(*) FROM mcp_calls WHERE status='ok'").fetchone()[0]
        by_tool = []
        for r in conn.execute(
                "SELECT tool, COUNT(*) AS n, SUM(status='ok') AS ok_n, "
                "SUM(latency_ms) AS sum_ms, MAX(latency_ms) AS max_ms, read_only "
                "FROM mcp_calls GROUP BY tool ORDER BY n DESC"):
            lats = [x[0] for x in conn.execute(
                "SELECT latency_ms FROM mcp_calls WHERE tool=?", (r["tool"],))]
            by_tool.append({
                "tool": r["tool"], "calls": r["n"],
                "ok_rate": round((r["ok_n"] or 0) / r["n"], 3) if r["n"] else None,
                "avg_ms": int((r["sum_ms"] or 0) / r["n"]) if r["n"] else 0,
                "p95_ms": _pct(lats, 0.95), "max_ms": r["max_ms"] or 0,
                "read_only": bool(r["read_only"]),
            })
        all_lats = [x[0] for x in conn.execute("SELECT latency_ms FROM mcp_calls")]
        clients = {r["client"]: r["n"] for r in conn.execute(
            "SELECT client, COUNT(*) AS n FROM mcp_calls GROUP BY client")}
        calls = [dict(r) for r in conn.execute(
            "SELECT id, ts, tool, client, status, error, latency_ms, result_chars, "
            "args_json, result_preview, read_only FROM mcp_calls ORDER BY id DESC LIMIT 60")]
        return {"total": total, "today": today, "ok": ok,
                "ok_rate": round(ok / total, 3) if total else None,
                "p50_ms": _pct(all_lats, 0.5), "p95_ms": _pct(all_lats, 0.95),
                "by_tool": by_tool, "clients": clients, "calls": calls}
    finally:
        conn.close()


def _latest_eval() -> dict | None:
    files = sorted(EVALS_DIR.glob("mcp-results-*.json"))
    if not files:
        return None
    f = files[-1]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    data["file"] = f.name
    return data


async def _surface() -> dict:
    tools = []
    for t in await mcp.list_tools():
        spec = gateway.SPECS[t.name]
        tools.append({"name": t.name, "summary": spec.summary, "read_only": spec.read_only,
                      "description": t.description, "schema": t.input_schema,
                      "example": spec.example})
    # 静态资源 + 资源模板分开取：kb://card/{card_id} 是模板（参数化），
    # 不会出现在 list_resources 里——只看前者会以为「卡详情」没暴露。
    resources = [str(r.uri) for r in await mcp.list_resources()]
    try:
        resources += [str(t.uri_template) for t in await mcp.list_resource_templates()]
    except Exception:  # noqa: BLE001
        pass
    return {"tools": tools, "resources": resources,
            "prompts": [p.name for p in await mcp.list_prompts()]}


@router.get("/board")
def mcp_board():
    surface = asyncio.run(_surface())
    return {
        "status": {
            "name": mcp.name, "version": mcp.version,
            "tools": len(surface["tools"]),
            "read_only": sum(1 for t in surface["tools"] if t["read_only"]),
            "write": sum(1 for t in surface["tools"] if not t["read_only"]),
            "resources": surface["resources"], "prompts": surface["prompts"],
            "transports": {
                "stdio": "python -m app.mcp.server",
                "http": "python -m app.mcp.server --http --port 8766",
            },
        },
        "tools": surface["tools"],
        "stats": _stats(),
        "eval": _latest_eval(),
    }


class TryReq(BaseModel):
    tool: str
    args: dict = {}


@router.post("/try")
def mcp_try(req: TryReq):
    spec = gateway.SPECS.get(req.tool)
    if not spec:
        raise HTTPException(404, f"未知工具：{req.tool}")
    if not spec.read_only:
        raise HTTPException(400, "写工具不允许在页面上试跑：会写审批队列，请走正常流程")
    out = gateway.invoke(req.tool, req.args, client="ui_try")
    return {"tool": req.tool, "ok": out.ok, "error": out.error,
            "latency_ms": out.latency_ms, "chars": out.chars,
            "payload": out.payload[:6000], "stats": _stats()}


class EvalReq(BaseModel):
    with_llm: bool = False


@router.post("/eval")
def mcp_run_eval(req: EvalReq):
    if not _eval_lock.acquire(blocking=False):
        raise HTTPException(409, "已有一次 MCP 评测在跑，等它结束")
    t0 = time.perf_counter()
    try:
        report = _load_eval_mod().run(with_llm=req.with_llm)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"评测失败：{type(e).__name__}: {e}")
    finally:
        _eval_lock.release()
    return {"eval": report, "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "stats": _stats()}
