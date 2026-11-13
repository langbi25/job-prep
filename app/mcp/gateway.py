"""MCP 工具网关：注册表 + 统一执行 + 调用日志。

四条设计原则（都是这个项目踩过的坑换来的）：

1. **工具是 service 层的薄壳**，不写业务逻辑。REST / 内置 Agent / MCP 共用同一条
   实现路径——否则就像坑 6 那样，同一份数据的多个读路径会悄悄分叉。
2. **每次调用必落 `mcp_calls`**，成功失败都落。stdio 传输下 stdout 就是协议管道，
   不能往里打日志，结构化落库是唯一能不破坏协议的可观测手段。
3. **结果按 token 预算截断**：MCP 的返回值会直接进客户端上下文，列表接口只回摘要，
   详情留给 get_card 二次下钻——和上下文管理是同一条经验。
4. **读写边界在网关统一判**，不指望每个工具自觉：`read_only` 是注册表上的事实，
   写工具的执行在 UI/评测里可被单独审计。
"""
import datetime
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.db import get_conn
from ..services import agent_tools, harness, kb_service
from . import registry

# 单次返回给客户端的字符上限。6000 字≈3k token，足够一份卡详情，又不会撑爆上下文。
MAX_RESULT_CHARS = 6000
ARGS_PREVIEW_CHARS = 900


@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str
    read_only: bool
    handler: Callable[[dict], Any]
    example: dict = field(default_factory=dict)


def _get_card(args: dict) -> dict:
    """卡详情：给 MCP 客户端「点进去看」的能力，比 search_kb 的摘要更全。"""
    cid = str(args.get("card_id") or "").strip()
    if not cid:
        return {"error": "card_id 不能为空"}
    card = kb_service.get_card(cid)
    if not card:
        return {"error": f"卡片不存在: {cid}"}
    conn = get_conn()
    try:
        linked = [dict(r) for r in conn.execute(
            "SELECT l.question_id, l.match_method, q.text FROM card_question_links l "
            "JOIN question_pool q ON q.id = l.question_id WHERE l.card_id = ? "
            "ORDER BY q.freq_count DESC", (cid,))]
    finally:
        conn.close()
    return {
        "card_id": card["id"], "title": card["title"], "domain": card["domain"],
        "topic": card["topic"], "status": card["status"], "mastery": card["mastery"],
        "points": card["points"], "pitfalls": card["pitfalls"],
        "linked_questions": linked,
        "file_path": card.get("file_path"),
    }


# 对外暴露的工具：**元数据全部来自 registry**（唯一事实源）。这里只补两样 registry 不该知道的东西——
# handler 绑定（实现住在 service 层）与 example（面板"试跑"的预填参数）。
_IMPL = {**harness._IMPL, **agent_tools.IMPL}

# get_card 只有 MCP 侧有实现：它是「按 id 下钻全文」，服务的是外部宿主的交互习惯
# （模型先 search 拿摘要、再点进去看全文）。内置循环目前不暴露它，靠 search_kb 回要点。
# 单独放这里而不是塞进 harness._IMPL——那会让「内部可见集」悄悄多一个工具。
_EXTRA_IMPL = {"get_card": _get_card}

_EXAMPLES = {
    "search_kb": {"query": "Flink 反压排查", "top_k": 3},
    "get_card": {"card_id": "kb-agent-memory-001"},
    "get_daily_brief": {},
    "list_due_cards": {"limit": 10},
    "get_weak_topics": {"direction": "backend"},
    "get_top_questions": {"limit": 5},
    "draft_cards": {"topic": "Flink 数据倾斜", "limit": 2},
    "ingest_text": {"text": "面经原文…", "draft_cards": True},
    "ingest_retrospective": {"source_path": "docs/复盘-自进化闭环.md", "build_cards": True},
}

SPECS: dict[str, ToolSpec] = {}
for _d in registry.mcp_tools():
    _h = _EXTRA_IMPL.get(_d.name) or _IMPL.get(_d.name)
    if _h is None:
        # 启动即失败：注册表声明了对外工具却没有实现，等于告诉客户端"我有这个能力"然后必然失败。
        # 这种错误在导入期炸掉，比等模型调用时返回 unknown 好——后者会被模型当成偶发故障重试。
        raise RuntimeError(f"registry 声明了对外工具 {_d.name}，但 harness/agent_tools 里没有实现")
    SPECS[_d.name] = ToolSpec(_d.name, _d.summary, _d.read_only, _h, _EXAMPLES.get(_d.name, {}))

def catalog() -> list[dict]:
    return [{"name": s.name, "summary": s.summary, "read_only": s.read_only,
             "example": s.example} for s in SPECS.values()]


def _compact_args(args: dict) -> str:
    """入参摘要：字符串截到 200 字。面经原文这类长文本不能整份入库。"""
    slim = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 200:
            slim[k] = v[:200] + f"…(+{len(v) - 200})"
        elif isinstance(v, list):
            slim[k] = [x[:80] + "…" if isinstance(x, str) and len(x) > 80 else x for x in v[:5]]
        else:
            slim[k] = v
    s = json.dumps(slim, ensure_ascii=False)
    return s if len(s) <= ARGS_PREVIEW_CHARS else s[:ARGS_PREVIEW_CHARS] + "…"


def _log(*, tool: str, client: str, args_json: str, status: str, error: str | None,
         latency_ms: int, payload: str, read_only: bool) -> None:
    """写审计行。日志失败绝不能影响工具调用本身——观测是副产物，不是前置条件。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO mcp_calls (ts, tool, client, args_json, args_hash, status, error, "
                "latency_ms, result_chars, result_preview, read_only) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.datetime.now().isoformat(timespec="seconds"), tool, client, args_json,
                 hashlib.sha1(args_json.encode("utf-8")).hexdigest()[:12], status,
                 (error or "")[:300] or None, latency_ms, len(payload),
                 payload[:400] if payload else None, 1 if read_only else 0),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Outcome:
    tool: str
    ok: bool
    payload: str
    error: str | None
    latency_ms: int
    chars: int


def invoke(name: str, args: dict | None, *, client: str = "stdio") -> Outcome:
    """执行一个工具并记账。MCP server、评测脚本、页面「试跑」共用这一个入口。"""
    args = args or {}
    spec = SPECS.get(name)
    args_json = _compact_args(args)
    t0 = time.perf_counter()
    if spec is None:
        payload = json.dumps({"error": f"未知工具: {name}，可用: {','.join(SPECS)}"},
                             ensure_ascii=False)
        _log(tool=name, client=client, args_json=args_json, status="error",
             error="unknown_tool", latency_ms=0, payload=payload, read_only=True)
        return Outcome(name, False, payload, "unknown_tool", 0, len(payload))

    error = None
    from ..observability import tracing as tr
    tr.set_scene(f"mcp:{client}")
    with tr.span(f"tool.{name}", tr.KIND_TOOL, {
            "tool.name": name, "tool.source": f"mcp:{client}", "tool.read_only": spec.read_only,
            "input.value": args_json}) as s:
        try:
            result = spec.handler(args)
            if isinstance(result, dict) and result.get("error"):
                error = str(result["error"])[:300]
            payload = agent_tools.json_safe(result, MAX_RESULT_CHARS)
            ok = error is None
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"[:300]
            payload = json.dumps({"error": error, "tool": name}, ensure_ascii=False)
            ok = False
        if s is not None:
            s.set_attribute("output.value", payload)
            s.set_attribute("tool.result_chars", len(payload))
            tr.record_tool_action(s, result if isinstance(result, dict) else None)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    _log(tool=name, client=client, args_json=args_json, status="ok" if ok else "error",
         error=error, latency_ms=latency_ms, payload=payload, read_only=spec.read_only)
    return Outcome(name, ok, payload, error, latency_ms, len(payload))
