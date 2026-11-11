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


def _project_node() -> dict:
    q = '{ projects(first: 20) { edges { node { name traceCount tokenCountTotal '
    q += 'tokenCountPrompt tokenCountCompletion '
    q += 'p50: latencyMsQuantile(probability: 0.5) p95: latencyMsQuantile(probability: 0.95) } } } }'
    data = _gql(q)
    edges = data["projects"]["edges"]
    for e in edges:
        if e["node"]["name"] == PROJECT_NAME:
            return e["node"]
    for e in edges:  # 兜底：第一个非 Experiment 项目
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
def overview() -> dict:
    """项目级统计：总请求数、token 用量、延迟分位。"""
    p = _project_node()
    return {
        "available": True,
        "project": p["name"],
        "phoenix_url": PHOENIX_URL,
        "trace_count": p.get("traceCount") or 0,
        "tokens_total": p.get("tokenCountTotal") or 0,
        "tokens_prompt": p.get("tokenCountPrompt") or 0,
        "tokens_completion": p.get("tokenCountCompletion") or 0,
        "latency_p50_ms": p.get("p50") or 0,
        "latency_p95_ms": p.get("p95") or 0,
    }


def list_traces(limit: int = 50) -> dict:
    """最近的请求/对话列表（根 span 视角）。"""
    limit = max(1, min(limit, 100))
    q = (
        '{ projects(first: 20) { edges { node { name '
        f'spans(first: {limit}, sort: {{col: startTime, dir: desc}}, '
        'filterCondition: "parent_id is None") { edges { node { '
        'name spanKind statusCode startTime latencyMs '
        'context { traceId spanId } tokenCountTotal numChildSpans '
        'trace { numSpans } } } } } } } }'
    )
    data = _gql(q)
    node = None
    for e in data["projects"]["edges"]:
        if e["node"]["name"] == PROJECT_NAME:
            node = e["node"]
            break
    if node is None:
        for e in data["projects"]["edges"]:
            if not e["node"]["name"].startswith("Experiment"):
                node = e["node"]
                break
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
    """单条 trace 的 span 树 + 瀑布图坐标（offset_ms / latency_ms）。"""
    q = (
        '{ projects(first: 20) { edges { node { name '
        f'trace(traceId: "{trace_id}") {{ '
        'traceId startTime latencyMs numSpans '
        'spans(first: 300) { edges { node { '
        'name spanKind statusCode statusMessage parentId startTime endTime latencyMs '
        'context { spanId } '
        'tokenCountTotal tokenCountPrompt tokenCountCompletion attributes '
        '} } } } } } } }'
    )
    data = _gql(q)
    node = None
    for e in data["projects"]["edges"]:
        if e["node"]["name"] == PROJECT_NAME:
            node = e["node"]
            break
    if node is None:
        for e in data["projects"]["edges"]:
            if not e["node"]["name"].startswith("Experiment"):
                node = e["node"]
                break
    tr = (node or {}).get("trace")
    if not tr:
        raise PhoenixUnavailable(f"trace {trace_id} 不存在（可能已被 Phoenix 清理）")

    raw = [e["node"] for e in tr["spans"]["edges"]]
    t0 = min(_ts_ms(s["startTime"]) for s in raw) if raw else 0.0

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
