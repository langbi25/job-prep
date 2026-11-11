"""可观测中心服务：把 Phoenix 的英文 trace 数据翻译成中文面板。

数据源：Phoenix GraphQL（默认 http://127.0.0.1:6006/graphql，PHOENIX_URL 可覆盖）。
本服务只做「读取 + 翻译 + 组树」，不写任何数据；Phoenix 不可用时优雅降级（返回 available=False）。

历史坑（务必保留）：
- requests 必须 trust_env=False：系统 HTTP_PROXY 会劫持本地请求（OTel 导出曾因此 404）。
- Phoenix GraphQL 解析器要求参数右括号后必须有空格：`(first: 1) {` 合法，`(first:1){` 直接语法错误。
- 根 span 过滤条件是 Python 风格的 `parent_id is None`（`== null` 匹配不到）。
"""
import json
import os
import re
from datetime import datetime

import requests

PHOENIX_URL = os.environ.get("PHOENIX_URL", "http://127.0.0.1:6006")
PROJECT_NAME = os.environ.get("PHOENIX_PROJECT", "default")

_session = requests.Session()
_session.trust_env = False  # 不读系统代理，本地直连

# ── 中英翻译 ────────────────────────────────────────────────────────────────
SPAN_KIND_ZH = {
    "AGENT": "智能体", "LLM": "模型调用", "TOOL": "工具调用",
    "RETRIEVER": "知识检索", "CHAIN": "链路编排", "EMBEDDING": "向量化",
    "GUARDRAIL": "护栏", "RERANKER": "重排序", "unknown": "服务",
}

_NAME_RULES = [
    (re.compile(r"^agent\.console$"), "Agent 对话"),
    (re.compile(r"^agent\.(?P<t>.+)$"), "子Agent·{t}"),
    (re.compile(r"^llm\.chat$"), "LLM 生成回答"),
    (re.compile(r"^retriever\."), "知识检索"),
    (re.compile(r"^chain\.assemble$"), "回答组装"),
    (re.compile(r"^tool\.(?P<t>.+)$"), "工具·{t}"),
    (re.compile(r"^fastapi\.endpoint$"), "请求处理"),
    (re.compile(r"^fastapi\.serialization$"), "响应序列化"),
    (re.compile(r"^fastapi\.dependencies$"), "依赖注入"),
]


def zh_name(name: str) -> str:
    """内部 span 名 → 中文名；HTTP 路由等本就可读的名字原样返回。"""
    for pat, tpl in _NAME_RULES:
        m = pat.match(name)
        if m:
            return tpl.format(**m.groupdict()) if "{" in tpl else tpl
    return name


# ── GraphQL 基础 ────────────────────────────────────────────────────────────
class PhoenixUnavailable(RuntimeError):
    pass


def _gql(query: str) -> dict:
    try:
        resp = _session.post(f"{PHOENIX_URL}/graphql",
                             json={"query": query}, timeout=10)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise PhoenixUnavailable(f"Phoenix 连接失败：{e}") from e
    data = resp.json()
    if data.get("errors"):
        raise PhoenixUnavailable(f"Phoenix 查询出错：{data['errors'][0].get('message')}")
    return data["data"]


def _pick_project(edges: list) -> dict:
    """选目标项目：优先 default，兜底第一个非 Experiment 项目。"""
    for e in edges:
        if e["node"]["name"] == PROJECT_NAME:
            return e["node"]
    for e in edges:
        if not e["node"]["name"].startswith("Experiment"):
            return e["node"]
    raise PhoenixUnavailable("Phoenix 中没有可用项目")


# ── 属性提取（只挑面板要用的，prompt 全文不进面板）─────────────────────────
def _trunc(v, n: int = 200) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s[:n] + ("…" if len(s) > n else "")


def _extract_attrs(attrs) -> dict:
    """提取面板要展示的关键属性。

    兼容两种格式：
    - 嵌套式（本项目 tracing 实际格式）：llm.model_name / tool.name / input.value / output.value
    - 扁平式（openinference 标准）：llm.model_name / tool.name / llm.input_messages / input.value
    """
    out = {}
    if not attrs:
        return out
    if isinstance(attrs, str):  # 部分 Phoenix 版本把 attributes 返回成 JSON 字符串
        try:
            attrs = json.loads(attrs)
        except (ValueError, TypeError):
            return out
    if not isinstance(attrs, dict):
        return out

    llm = attrs.get("llm")
    if isinstance(llm, dict):
        for k, key in (("model_name", "model"), ("scene", "scene"), ("temperature", "temperature")):
            if llm.get(k) is not None:
                out[key] = llm[k]
    tool = attrs.get("tool")
    if isinstance(tool, dict) and tool.get("name"):
        out["tool_name"] = tool["name"]
    ret = attrs.get("retriever")
    if isinstance(ret, dict):
        out["retriever"] = (f"top_k={ret.get('top_k')} 命中 {ret.get('n_hits')} 条"
                            + (f" · 范围 {ret['scope']}" if ret.get("scope") else ""))
    chain = attrs.get("chain")
    if isinstance(chain, dict) and chain.get("total_tokens"):
        out["chain_tokens"] = f"组装上下文 {chain['total_tokens']} tokens"

    for src in ("input", "output"):
        v = attrs.get(src)
        if isinstance(v, dict) and v.get("value"):
            out[src] = _trunc(str(v["value"]), 160)
        elif isinstance(v, str):
            out[src] = _trunc(v, 160)

    # 扁平格式兜底
    if not out.get("model") and attrs.get("llm.model_name"):
        out["model"] = attrs["llm.model_name"]
    if not out.get("tool_name") and attrs.get("tool.name"):
        out["tool_name"] = attrs["tool.name"]
    if not out.get("input") and attrs.get("input.value"):
        out["input"] = _trunc(attrs["input.value"])
    if not out.get("output") and attrs.get("output.value"):
        out["output"] = _trunc(attrs["output.value"])
    for k, key in (("llm.input_messages", "llm_in"), ("llm.output_messages", "llm_out")):
        msgs = attrs.get(k)
        if isinstance(msgs, list) and msgs:
            last = msgs[-1] if isinstance(msgs[-1], dict) else {}
            content = (last.get("message") or {}).get("content")
            if content:
                out[key] = _trunc(content, 160)
    return out


def _ts_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000


# ── 对外三接口 ──────────────────────────────────────────────────────────────
_AGENT_FILTER = "span_kind == 'AGENT'"


def overview(scope: str = "agent") -> dict:
    """项目级统计。scope=agent 时只统计 Agent 链路（过滤掉 HTTP 探针噪声）。"""
    scoped = scope == "agent"
    cond = _AGENT_FILTER if scoped else None
    q = '{ projects(first: 20) { edges { node { name '
    q += f'totalCount: traceCount agentCount: traceCount(filterCondition: "{_AGENT_FILTER}") '
    q += 'tokenCountTotal tokenCountPrompt tokenCountCompletion sessionCount '
    if scoped:
        q += (f'p50: latencyMsQuantile(probability: 0.5, filterCondition: "{cond}") '
              f'p95: latencyMsQuantile(probability: 0.95, filterCondition: "{cond}") ')
    else:
        q += 'p50: latencyMsQuantile(probability: 0.5) p95: latencyMsQuantile(probability: 0.95) '
    q += '} } } }'
    data = _gql(q)
    node = _pick_project(data["projects"]["edges"])
    return {
        "available": True,
        "project": node["name"],
        "phoenix_url": PHOENIX_URL,
        "scope": scope,
        "trace_count": (node.get("agentCount") if scoped else node.get("totalCount")) or 0,
        "trace_count_agent": node.get("agentCount") or 0,
        "trace_count_all": node.get("totalCount") or 0,
        "session_count": node.get("sessionCount") or 0,
        "tokens_total": node.get("tokenCountTotal") or 0,
        "tokens_prompt": node.get("tokenCountPrompt") or 0,
        "tokens_completion": node.get("tokenCountCompletion") or 0,
        "latency_p50_ms": node.get("p50") or 0,
        "latency_p95_ms": node.get("p95") or 0,
    }


def list_traces(limit: int = 50, scope: str = "agent") -> dict:
    """最近的链路列表（根 span 视角）。

    scope="agent"：只看 Agent 链路（agent.*，OTel 打点的本体）；
    scope="all"：包含 FastAPI 自动探针产生的 HTTP 请求 span（探活/路由噪声）。
    """
    limit = max(1, min(limit, 100))
    cond = "parent_id is None"
    if scope == "agent":
        cond += f" and {_AGENT_FILTER}"
    q = (
        '{ projects(first: 20) { edges { node { name '
        f'spans(first: {limit}, sort: {{col: startTime, dir: desc}}, '
        f'filterCondition: "{cond}") {{ edges {{ node {{ '
        'name spanKind statusCode startTime latencyMs '
        'context { traceId spanId } tokenCountTotal numChildSpans '
        'trace { numSpans } } } } } } } }'
    )
    data = _gql(q)
    node = _pick_project(data["projects"]["edges"])
    items = []
    for e in (node or {}).get("spans", {}).get("edges", []):
        n = e["node"]
        tr = n.get("trace") or {}
        items.append({
            "trace_id": n["context"]["traceId"],
            "name": n["name"],
            "name_zh": zh_name(n["name"]),
            "kind": n["spanKind"],
            "kind_zh": SPAN_KIND_ZH.get(n["spanKind"], n["spanKind"]),
            "start": n["startTime"],
            "latency_ms": n["latencyMs"],
            "status": n["statusCode"],
            "error": n["statusCode"] == "ERROR",
            "tokens": n["tokenCountTotal"] or 0,
            "span_count": tr.get("numSpans") or 1,
        })
    return {"available": True, "items": items}


def trace_detail(trace_id: str) -> dict:
    """单条 trace 的 span 树 + 瀑布图坐标（offset_ms / latency_ms）+ span events。"""
    q = (
        '{ projects(first: 20) { edges { node { name '
        f'trace(traceId: "{trace_id}") {{ '
        'traceId startTime latencyMs numSpans '
        'spans(first: 300) { edges { node { '
        'name spanKind statusCode statusMessage parentId startTime endTime latencyMs '
        'context { spanId } '
        'tokenCountTotal tokenCountPrompt tokenCountCompletion attributes '
        'events { name timestamp attributes } '
        '} } } } } } } }'
    )
    data = _gql(q)
    node = _pick_project(data["projects"]["edges"])
    tr = (node or {}).get("trace")
    if not tr:
        raise PhoenixUnavailable(f"trace {trace_id} 不存在（可能已被 Phoenix 清理）")

    raw = [e["node"] for e in tr["spans"]["edges"]]
    t0 = min(_ts_ms(s["startTime"]) for s in raw) if raw else 0.0

    def _events(s) -> list:
        out = []
        for ev in s.get("events") or []:
            attrs = ev.get("attributes") or {}
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except (ValueError, TypeError):
                    attrs = {}
            offset = None
            try:
                offset = round((_ts_ms(ev["timestamp"]) - _ts_ms(s["startTime"])), 1)
            except (KeyError, ValueError, TypeError):
                pass
            flat = {k: (v if not isinstance(v, str) else _trunc(v, 120))
                    for k, v in (attrs or {}).items()}
            out.append({"name": ev.get("name"), "offset_ms": offset, "attrs": flat})
        return out

    def build(s):
        return {
            "span_id": s["context"]["spanId"],
            "parent_id": s.get("parentId"),
            "name": s["name"],
            "name_zh": zh_name(s["name"]),
            "kind": s["spanKind"],
            "kind_zh": SPAN_KIND_ZH.get(s["spanKind"], s["spanKind"]),
            "start": s["startTime"],
            "offset_ms": round(max(0.0, _ts_ms(s["startTime"]) - t0), 2),
            "latency_ms": s["latencyMs"],
            "status": s["statusCode"],
            "error": s["statusCode"] == "ERROR",
            "error_message": s.get("statusMessage") or "",
            "tokens": s["tokenCountTotal"] or 0,
            "prompt_tokens": s["tokenCountPrompt"] or 0,
            "completion_tokens": s["tokenCountCompletion"] or 0,
            "attrs": _extract_attrs(s.get("attributes")),
            "events": _events(s),
            "children": [],
        }

    by_id = {}
    roots = []
    for s in sorted(raw, key=lambda x: x["startTime"]):
        n = build(s)
        by_id[n["span_id"]] = n
        parent = by_id.get(n["parent_id"])
        if parent is not None:
            parent["children"].append(n)
        else:
            roots.append(n)
    return {
        "available": True,
        "trace_id": tr["traceId"],
        "start": tr["startTime"],
        "latency_ms": tr["latencyMs"],
        "num_spans": tr["numSpans"],
        "total_latency_ms": round(max((_ts_ms(s["endTime"]) for s in raw if s.get("endTime")), default=t0) - t0, 2),
        "tree": roots,
    }


# ── Session / 轮次 ─────────────────────────────────────────────────────────
def list_sessions(limit: int = 20) -> dict:
    """会话列表。一场会话 = 一次连续对话（span 带 session.id 属性被 Phoenix 聚合）。"""
    limit = max(1, min(limit, 50))
    q = (
        '{ projects(first: 20) { edges { node { name '
        f'sessions(first: {limit}, sort: {{col: startTime, dir: desc}}) {{ edges {{ node {{ '
        'sessionId startTime endTime numTraces numTracesWithError '
        'firstInput { value } '
        'tokenUsage { total } '
        '} } } } } } }'
    )
    data = _gql(q)
    node = _pick_project(data["projects"]["edges"])
    items = []
    for e in (node or {}).get("sessions", {}).get("edges", []):
        n = e["node"]
        items.append({
            "session_id": n.get("sessionId"),
            "start": n.get("startTime"),
            "end": n.get("endTime"),
            "turns": n.get("numTraces") or 0,
            "errors": n.get("numTracesWithError") or 0,
            "first_input": _trunc(((n.get("firstInput") or {}).get("value")) or "", 80),
            "tokens": ((n.get("tokenUsage") or {}).get("total")) or 0,
        })
    return {"available": True, "items": items}


def session_detail(session_id: str) -> dict:
    """一场会话的轮次列表：每轮 = 一条 trace（用户一句话触发的完整 agent 循环）。"""
    q = (
        '{ projects(first: 20) { edges { node { name '
        f'sessions(first: 50) {{ edges {{ node {{ '
        f'sessionId traces(first: 100) {{ edges {{ node {{ '
        'traceId startTime latencyMs numSpans errorCount '
        'rootSpan { name latencyMs } '
        'spans(first: 100) { edges { node { name parentId spanKind attributes } } } '
        '} } } } } } } } } }'
    )
    data = _gql(q)
    node = _pick_project(data["projects"]["edges"])
    sess = None
    for e in (node or {}).get("sessions", {}).get("edges", []):
        if e["node"].get("sessionId") == session_id:
            sess = e["node"]
            break
    if sess is None:
        raise PhoenixUnavailable(f"会话 {session_id} 不存在（可能尚未产生带 session.id 的 trace）")
    raw_turns = []
    for e in sess["traces"]["edges"]:
        n = e["node"]
        root = n.get("rootSpan") or {}
        # 展示名优先取链路里的 AGENT 根 span（trace 根偶发挂在 HTTP 请求 span 下）
        span_edges = (n.get("spans") or {}).get("edges") or []
        agent_name = next((se["node"].get("name") for se in span_edges
                           if str(se["node"].get("spanKind") or "").upper() == "AGENT"),
                          None)
        display = agent_name or root.get("name") or ""
        # 轮次号：取 AGENT 根 span 的属性（打点侧记录的 session.turn）。
        # 属性可能是 JSON 字符串（双引号）或 Python repr 字符串（单引号），都要容错。
        agent_attrs = next((se["node"].get("attributes") for se in span_edges
                            if str(se["node"].get("spanKind") or "").upper() == "AGENT"),
                           None)
        attrs = agent_attrs
        recorded_turn = None
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except (ValueError, TypeError):
                try:
                    import ast as _ast
                    attrs = _ast.literal_eval(attrs)
                except (ValueError, SyntaxError):
                    attrs = {}
        if isinstance(attrs, dict):
            sattrs = attrs.get("session")
            if isinstance(sattrs, dict) and sattrs.get("turn"):
                recorded_turn = int(sattrs["turn"])
            elif attrs.get("session.turn"):
                recorded_turn = int(attrs["session.turn"])
        raw_turns.append({
            "_turn": recorded_turn,
            "trace_id": n["traceId"],
            "start": n["startTime"],
            "latency_ms": n["latencyMs"],
            "num_spans": n["numSpans"] or 1,
            "errors": n.get("errorCount") or 0,
            "name": display,
            "name_zh": zh_name(display) if display else "",
            "latency_root_ms": root.get("latencyMs"),
        })
    raw_turns.sort(key=lambda t: t["start"])
    turns = []
    for i, t in enumerate(raw_turns, start=1):
        t["turn"] = t.pop("_turn") or i  # 打点侧没记轮次时按时间序补号
        turns.append(t)
    return {
        "available": True,
        "session_id": session_id,
        "turns": turns,
        "start": sess.get("startTime"),
        "end": sess.get("endTime"),
    }
