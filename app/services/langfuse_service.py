"""Langfuse 数据源适配：读 v2 observations，自己重建 trace 树与会话。

为什么不走 `/api/public/traces` 和 `/api/public/sessions`：
这两个 v1 接口已被标记废弃（Cloud 服务至 2026-11-16），v4 上 sessions 接口直接 404。
新模型只有 `GET /api/public/v2/observations` —— 返回扁平的 observation 列表，
trace 的 input/output 要从根 observation（parentObservationId 为空）重建，
会话只是「共享同一 sessionId 的 observation 集合」。

这条思路和 Phoenix 那套一致（都是拿扁平 span 列表自己组树），
所以翻译、属性解析、瀑布坐标全部复用 obs_common。

字段名容错说明：
v2 只返回请求的 fields 组，未请求的字段是「缺失」而非 null；不同小版本的
usage 结构也有差异（input/output vs promptTokens/completionTokens）。所以取值
一律走 _pick 多候选兜底，拿不到就退化成 0/空，不让面板因为一个字段名崩掉。
真实实例起来后跑 `python -m app.services.langfuse_service` 打印原始结构校准。
"""
import base64
import json
import os

import requests

from .obs_common import SPAN_KIND_ZH, extract_attrs, trunc, ts_ms, zh_name

LANGFUSE_URL = os.environ.get("LANGFUSE_URL", "http://127.0.0.1:3000").rstrip("/")
PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")

# v2 一次最多 1000，默认 50；面板按需拉，留余量
_PAGE_LIMIT = 200
_FIELDS = "core,basic,usage,io,model,trace_context"

_session = requests.Session()
_session.trust_env = False  # 与 Phoenix 侧同款：系统 HTTP_PROXY 会劫持本地请求

# Langfuse 的 observation.type → 我们的 span kind 语汇（面板翻译表是按后者建的）
_TYPE_TO_KIND = {
    "GENERATION": "LLM", "SPAN": "CHAIN", "EVENT": "CHAIN",
    "AGENT": "AGENT", "TOOL": "TOOL", "CHAIN": "CHAIN",
    "RETRIEVER": "RETRIEVER", "EMBEDDING": "EMBEDDING",
    "EVALUATOR": "GUARDRAIL", "GUARDRAIL": "GUARDRAIL",
}


class LangfuseUnavailable(RuntimeError):
    pass


def available(timeout: float = 3.0) -> bool:
    """探活：Langfuse 有 /api/public/health。"""
    try:
        r = _session.get(f"{LANGFUSE_URL}/api/public/health", timeout=timeout)
        return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def _auth_headers() -> dict:
    if not (PUBLIC_KEY and SECRET_KEY):
        raise LangfuseUnavailable(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 未配置（Langfuse UI → Settings → API Keys）"
        )
    token = base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(path: str, params: dict | None = None) -> dict:
    try:
        resp = _session.get(f"{LANGFUSE_URL}/api/public{path}",
                            params=params or {}, headers=_auth_headers(), timeout=15)
        resp.raise_for_status()
    except LangfuseUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise LangfuseUnavailable(f"Langfuse 连接失败：{e}") from e
    try:
        return resp.json()
    except ValueError as e:
        raise LangfuseUnavailable(f"Langfuse 返回非 JSON：{resp.text[:200]}") from e


def _pick(d: dict, *names, default=None):
    """多候选字段名兜底。"""
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default


def _usage_tokens(o: dict) -> tuple[int, int]:
    """从 observation 抽取 (prompt_tokens, completion_tokens)，兼容多种结构。"""
    u = o.get("usage") or o.get("usageDetails") or {}
    if not isinstance(u, dict):
        return 0, 0
    p = _pick(u, "input", "promptTokens", "prompt_tokens", "inputTokens", default=0) or 0
    c = _pick(u, "output", "completionTokens", "completion_tokens", "outputTokens", default=0) or 0
    try:
        return int(p), int(c)
    except (TypeError, ValueError):
        return 0, 0


def _latency_ms(o: dict) -> float:
    """observation 耗时。Langfuse 的 latency 单位是秒；缺了就用 end-start 算。"""
    lat = _pick(o, "latency", "latencyMs")
    if isinstance(lat, (int, float)) and lat:
        return float(lat) * 1000 if lat < 10000 else float(lat)  # 秒 → 毫秒
    st, et = _pick(o, "startTime"), _pick(o, "endTime")
    if st and et:
        try:
            return round(ts_ms(et) - ts_ms(st), 2)
        except (ValueError, TypeError):
            pass
    return 0.0


def _kind(o: dict) -> str:
    raw = str(_pick(o, "type", "observationType", default="") or "").upper()
    if raw in _TYPE_TO_KIND:
        return _TYPE_TO_KIND[raw]
    name = str(o.get("name") or "")
    if name.startswith("agent."):
        return "AGENT"
    if name.startswith("llm."):
        return "LLM"
    if name.startswith("tool."):
        return "TOOL"
    if name.startswith("retriever."):
        return "RETRIEVER"
    return "CHAIN"


def _fetch_observations(limit: int = _PAGE_LIMIT, session_id: str | None = None,
                        trace_id: str | None = None) -> list[dict]:
    """v2 observations 分页拉取（游标），返回扁平列表。"""
    out: list[dict] = []
    cursor = None
    while len(out) < limit:
        params = {"limit": min(_PAGE_LIMIT, limit - len(out)), "fields": _FIELDS}
        if session_id:
            params["filter"] = json.dumps(
                [{"type": "string", "column": "sessionId", "operator": "=", "value": session_id}])
        if trace_id:
            params["filter"] = json.dumps(
                [{"type": "string", "column": "traceId", "operator": "=", "value": trace_id}])
        if cursor:
            params["cursor"] = cursor
        page = _get("/v2/observations", params)
        batch = page.get("data") or []
        out.extend(batch)
        cursor = (page.get("meta") or {}).get("cursor")
        if not batch or not cursor:
            break
    return out[:limit]


def _start_key(o: dict) -> float:
    """排序键必须是解析后的时间戳。

    直接按 ISO 字符串排序会错：'...00Z' 与 '...00.5Z' 逐字符比时 'Z'(0x5A) > '.'(0x2E)，
    秒级时间反而排在小数秒之后——根 span 落到子 span 后面，组树时就会断成多个根。
    Langfuse 的时间精度不统一（有的带毫秒有的不带），Phoenix 那边恰好统一才没暴露。
    """
    st = _pick(o, "startTime", default="")
    try:
        return ts_ms(str(st))
    except (ValueError, TypeError):
        return 0.0


def _group_by_trace(obs: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for o in obs:
        tid = o.get("traceId") or o.get("trace_id")
        if tid:
            groups.setdefault(tid, []).append(o)
    return groups


def _root_of(group: list[dict]) -> dict:
    """根 observation = parentObservationId 为空的那条；兜底取最早的。"""
    for o in group:
        if not _pick(o, "parentObservationId", "parent_observation_id"):
            return o
    return min(group, key=_start_key) if group else {}


def _is_agent_trace(group: list[dict]) -> bool:
    """Agent 链路判定：根 observation 是 AGENT 类型，或名字带 agent. 前缀。"""
    root = _root_of(group)
    return _kind(root) == "AGENT" or str(root.get("name") or "").startswith("agent.")


# ── 对外接口（与 obs_service 的 Phoenix 实现同签名，api 层可直接换）──────────

def overview(scope: str = "agent") -> dict:
    obs = _fetch_observations(limit=1000)
    groups = _group_by_trace(obs)
    total = len(groups)
    agent_ids = [t for t, g in groups.items() if _is_agent_trace(g)]
    picked = agent_ids if scope == "agent" else list(groups)
    tok = prompt = completion = 0
    costs = []
    for tid in picked:
        g = groups[tid]
        for o in g:
            p, c = _usage_tokens(o)
            prompt += p
            completion += c
            tok += p + c
        costs.append(sum((_pick(o, "totalCost", "calculatedTotalCost", default=0) or 0) for o in g))
    costs.sort()
    n = len(costs)

    def q(pct: float) -> float:
        return round(costs[min(n - 1, int(n * pct))], 4) if n else 0.0

    return {
        "available": True,
        "backend": "langfuse",
        "project": "langfuse",
        "phoenix_url": LANGFUSE_URL,  # 复用字段名：前端用它做「去原生界面」跳转
        "trace_count": len(picked),
        "trace_count_agent": len(agent_ids),
        "trace_count_all": total,
        "tokens_total": tok,
        "tokens_prompt": prompt,
        "tokens_completion": completion,
        "cost_total": round(sum(costs), 4),
        "latency_p50_ms": q(0.5),
        "latency_p95_ms": q(0.95),
    }


def list_traces(limit: int = 50, scope: str = "agent") -> dict:
    obs = _fetch_observations(limit=max(400, limit * 8))
    groups = _group_by_trace(obs)
    rows = []
    for tid, g in groups.items():
        if scope == "agent" and not _is_agent_trace(g):
            continue
        root = _root_of(g)
        p, c = _usage_tokens(root)
        if not (p or c):
            p = sum(_usage_tokens(o)[0] for o in g)
            c = sum(_usage_tokens(o)[1] for o in g)
        rows.append({
            "trace_id": tid,
            "span_id": _pick(root, "id", default=""),
            "name": root.get("name") or "",
            "name_zh": zh_name(root.get("name") or ""),
            "kind": _kind(root),
            "kind_zh": SPAN_KIND_ZH.get(_kind(root), _kind(root)),
            "start": _pick(root, "startTime", default=""),
            "latency_ms": _latency_ms(root),
            "status": "ERROR" if str(_pick(root, "level", default="")).upper() == "ERROR" else "OK",
            "error": str(_pick(root, "level", default="")).upper() == "ERROR",
            "tokens": p + c,
            "prompt_tokens": p,
            "completion_tokens": c,
            "cost": round(sum((_pick(o, "totalCost", "calculatedTotalCost", default=0) or 0) for o in g), 5),
            "span_count": len(g),
            "session_id": _pick(root, "sessionId", default="") or "",
        })
    rows.sort(key=lambda r: (ts_ms(r["start"]) if r["start"] else 0.0), reverse=True)
    return {"available": True, "backend": "langfuse", "items": rows[:limit]}


def trace_detail(trace_id: str) -> dict:
    obs = _fetch_observations(limit=1000, trace_id=trace_id)
    if not obs:
        raise LangfuseUnavailable(f"trace {trace_id} 不存在（或尚未入库）")
    t0 = min(ts_ms(_pick(o, "startTime", default="1970-01-01T00:00:00Z")) for o in obs)

    def build(o: dict) -> dict:
        st = _pick(o, "startTime", default="")
        p, c = _usage_tokens(o)
        try:
            offset = round(max(0.0, ts_ms(st) - t0), 2) if st else 0.0
        except (ValueError, TypeError):
            offset = 0.0
        kind = _kind(o)
        return {
            "span_id": _pick(o, "id", default=""),
            "parent_id": _pick(o, "parentObservationId", "parent_observation_id"),
            "name": o.get("name") or "",
            "name_zh": zh_name(o.get("name") or ""),
            "kind": kind,
            "kind_zh": SPAN_KIND_ZH.get(kind, kind),
            "start": st,
            "offset_ms": offset,
            "latency_ms": _latency_ms(o),
            "status": "ERROR" if str(_pick(o, "level", default="")).upper() == "ERROR" else "OK",
            "error": str(_pick(o, "level", default="")).upper() == "ERROR",
            "error_message": _pick(o, "statusMessage", "level", default="") or "",
            "tokens": p + c,
            "prompt_tokens": p,
            "completion_tokens": c,
            "cost": _pick(o, "totalCost", "calculatedTotalCost", default=0) or 0,
            # v2 的 input/output 是原始字符串，需要时前端自己截断展示
            "attrs": extract_attrs({"input": {"value": _pick(o, "input", default="")},
                                    "output": {"value": _pick(o, "output", default="")},
                                    "llm": {"model_name": _pick(o, "modelId", "model", default="")},
                                    "tool": {"name": o.get("name") if kind == "TOOL" else None}}),
            "events": [],
            "children": [],
        }

    by_id, roots = {}, []
    for o in sorted(obs, key=_start_key):
        node = build(o)
        by_id[node["span_id"]] = node
        parent = by_id.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)

    total = max((n["offset_ms"] + n["latency_ms"] for n in by_id.values()), default=0.0)
    return {
        "available": True,
        "backend": "langfuse",
        "trace_id": trace_id,
        "start": min(by_id.values(), key=lambda n: n["offset_ms"])["start"] if by_id else "",
        "latency_ms": round(total, 2),
        "num_spans": len(obs),
        "total_latency_ms": round(total, 2),
        "tree": roots,
    }


def list_sessions(limit: int = 20) -> dict:
    """会话 = 共享同一 sessionId 的 observation 集合（v4 没有独立会话实体）。"""
    obs = _fetch_observations(limit=1000)
    groups: dict[str, list[dict]] = {}
    for o in obs:
        sid = _pick(o, "sessionId", "session_id")
        if sid:
            groups.setdefault(sid, []).append(o)
    items = []
    for sid, g in groups.items():
        traces: dict[str, list[dict]] = {}
        for o in g:
            traces.setdefault(_pick(o, "traceId", default=""), []).append(o)
        starts = [str(_pick(o, "startTime", default="")) for o in sorted(g, key=_start_key)]
        first_root = _root_of(traces[min(traces, key=lambda t: _start_key(_root_of(traces[t])))]) if traces else {}
        tok = sum(sum(_usage_tokens(o)) for o in g)
        items.append({
            "session_id": sid,
            "turns": len(traces),
            "start": starts[0] if starts else "",
            "end": starts[-1] if starts else "",
            "tokens": tok,
            "cost": round(sum((_pick(o, "totalCost", "calculatedTotalCost", default=0) or 0) for o in g), 5),
            "errors": sum(1 for o in g if str(_pick(o, "level", default="")).upper() == "ERROR"),
            "first_input": trunc(_pick(first_root, "input", default="") or "", 80),
        })
    items.sort(key=lambda s: s["start"], reverse=True)
    return {"available": True, "backend": "langfuse", "items": items[:limit]}


def session_detail(session_id: str) -> dict:
    obs = _fetch_observations(limit=1000, session_id=session_id)
    if not obs:
        raise LangfuseUnavailable(f"会话 {session_id} 不存在（或尚未入库）")
    traces: dict[str, list[dict]] = {}
    for o in obs:
        traces.setdefault(_pick(o, "traceId", default=""), []).append(o)

    turns = []
    for tid, g in traces.items():
        root = _root_of(g)
        p = sum(_usage_tokens(o)[0] for o in g)
        c = sum(_usage_tokens(o)[1] for o in g)
        turns.append({
            "turn": 0,  # 排序后按序号补，打点侧没有 turn 属性时以时间序为准
            "trace_id": tid,
            "start": _pick(root, "startTime", default=""),
            "latency_ms": _latency_ms(root),
            "num_spans": len(g),
            "errors": sum(1 for o in g if str(_pick(o, "level", default="")).upper() == "ERROR"),
            "name": root.get("name") or "",
            "name_zh": zh_name(root.get("name") or ""),
            "latency_root_ms": _latency_ms(root),
            "tokens": p + c,
            "cost": round(sum((_pick(o, "totalCost", "calculatedTotalCost", default=0) or 0) for o in g), 5),
            "_sk": _start_key(root),
        })
    turns.sort(key=lambda t: t["_sk"])
    for i, t in enumerate(turns, start=1):
        t["turn"] = i
        t.pop("_sk", None)
    return {"available": True, "backend": "langfuse", "session_id": session_id, "turns": turns}


if __name__ == "__main__":  # 联调探针：打印真实响应结构，用来校准字段名
    print("endpoint:", LANGFUSE_URL, "| keys:", "set" if PUBLIC_KEY else "missing")
    print("health:", available())
    try:
        page = _get("/v2/observations", {"limit": 2, "fields": _FIELDS})
        print("meta:", page.get("meta"))
        for o in (page.get("data") or [])[:1]:
            print("keys:", sorted(o.keys()))
            print(json.dumps(o, ensure_ascii=False, indent=1)[:1200])
    except LangfuseUnavailable as e:
        print("probe failed:", e)
