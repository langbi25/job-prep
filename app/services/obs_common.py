"""可观测面板的共享层：与具体后端无关的翻译、属性解析、时间换算。

Phoenix（GraphQL）和 Langfuse（REST）拿到的 span 结构不同，但「给人看的中文名」
「要展示的关键属性」「瀑布图坐标」这三件事的逻辑是一样的——抽在这里，避免两套
实现各自漂移。

历史坑（从 obs_service 搬过来，务必保留）：
- attributes 可能是 JSON 字符串（双引号）或 Python repr 字符串（单引号），都要容错。
- 本项目 tracing 写的是嵌套式属性（llm.model_name / tool.name / input.value），
  而 openinference 标准是扁平式（"llm.model_name" 这种带点的单层键），两种都要认。
"""
import json
import re
from datetime import datetime

# ── 中英翻译 ────────────────────────────────────────────────────────────────
SPAN_KIND_ZH = {
    "AGENT": "智能体", "LLM": "模型调用", "TOOL": "工具调用",
    "RETRIEVER": "知识检索", "CHAIN": "链路编排", "EMBEDDING": "向量化",
    "GUARDRAIL": "护栏", "RERANKER": "重排序", "unknown": "服务",
}

NAME_RULES = [
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
    for pat, tpl in NAME_RULES:
        m = pat.match(name)
        if m:
            return tpl.format(**m.groupdict()) if "{" in tpl else tpl
    return name


def trunc(v, n: int = 200) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s[:n] + ("…" if len(s) > n else "")


def _loads_maybe(attrs):
    """attributes 可能是 dict / JSON 字符串 / Python repr 字符串。"""
    if isinstance(attrs, dict):
        return attrs
    if not isinstance(attrs, str) or not attrs.strip():
        return {}
    for loader in (json.loads, __import__("ast").literal_eval):
        try:
            out = loader(attrs)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
    return {}


def extract_attrs(attrs) -> dict:
    """提取面板要展示的关键属性（prompt 全文不进面板，只留摘要）。"""
    out = {}
    attrs = _loads_maybe(attrs)
    if not attrs:
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
    session = attrs.get("session")
    if isinstance(session, dict) and session.get("turn"):
        out["turn"] = session["turn"]

    for src in ("input", "output"):
        v = attrs.get(src)
        if isinstance(v, dict) and v.get("value"):
            out[src] = trunc(str(v["value"]), 160)
        elif isinstance(v, str):
            out[src] = trunc(v, 160)

    # 扁平格式兜底（openinference 标准写法）
    if not out.get("model") and attrs.get("llm.model_name"):
        out["model"] = attrs["llm.model_name"]
    if not out.get("tool_name") and attrs.get("tool.name"):
        out["tool_name"] = attrs["tool.name"]
    if not out.get("input") and attrs.get("input.value"):
        out["input"] = trunc(attrs["input.value"], 160)
    if not out.get("output") and attrs.get("output.value"):
        out["output"] = trunc(attrs["output.value"], 160)
    for k, key in (("llm.input_messages", "llm_in"), ("llm.output_messages", "llm_out")):
        msgs = attrs.get(k)
        if isinstance(msgs, list) and msgs:
            last = msgs[-1] if isinstance(msgs[-1], dict) else {}
            content = (last.get("message") or {}).get("content")
            if content:
                out[key] = trunc(content, 160)
    return out


def ts_ms(iso: str) -> float:
    """ISO8601 → epoch 毫秒。Langfuse 给的是 '2026-11-12T01:16:15.123Z' 这类。"""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso).timestamp() * 1000
