"""OTel 采集层：OpenInference 语义约定 + 可插拔后端（Phoenix / Langfuse）。

设计判断（都是这次踩过或想清楚的）：

1. **埋点在进程内用 OTel SDK，后端通过 OTLP/HTTP 可插拔**。Phoenix（pip 起）和
   Langfuse v3（docker compose）都吃 OTLP——同一份埋点，换 endpoint 就换后端，
   这是"自建记录、开放格式"和"直接依赖某个平台"的本质差别。

2. **OpenInference 的 span kind 是关键语义**：LLM / RETRIEVER / TOOL / AGENT / CHAIN。
   后端靠它区分瀑布图里的节点类型、算 token 归因。属性名就地硬编码这几个常量，
   不为五个字符串引一个依赖包；完整约定见 openinference-semantic-conventions。

3. **OTel 关闭时零开销、零异常**：没配 TRACING_ENDPOINT 就全部走 no-op 分支，
   业务代码不感知。评测、测试、CI 因此都不需要装后端。

4. **agent_span 永远给业务一个 trace_id**：OTel 开着就是真实 span 的 trace_id（32 hex），
   关着退化成本地 uuid——落库关联逻辑不因观测开关而分叉。
"""
import contextvars
import json
import os
import uuid
from contextlib import contextmanager

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

# ---- OpenInference span kind 属性（语义约定的最小子集） ----
OI_KIND = "openinference.span.kind"
KIND_AGENT = "AGENT"
KIND_CHAIN = "CHAIN"
KIND_LLM = "LLM"
KIND_RETRIEVER = "RETRIEVER"
KIND_TOOL = "TOOL"

# ---- OTel GenAI 语义约定的关键属性 ----
ATTR_MODEL = "gen_ai.request.model"
ATTR_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
ATTR_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"

# 长文本属性（prompt 原文/面经）截断：观测不能变成数据泄露面和上下文炸弹
MAX_ATTR_CHARS = 2000

_provider: TracerProvider | None = None
_tracer = trace.get_tracer("job-prep.observability")
_ensure_done = False


def ensure_configured() -> None:
    """按环境惰性装配：只尝试一次。业务代码/脚本不需要显式调 configure。

    观测是横切关注点——如果在 main.py 里手动装配，CLI 脚本和评测就会漏掉；
    惰性装配保证"配了环境变量就生效，没配就零开销"，且只查一次标记。
    """
    global _ensure_done
    if _ensure_done:
        return
    _ensure_done = True
    configure()


def enabled() -> bool:
    return _provider is not None


def configure(endpoint: str | None = None, exporter=None, service_name: str | None = None) -> bool:
    """初始化 TracerProvider。幂等：重复调用不会叠 processor。

    endpoint 允许写后端基地址：不以 traces 结尾时自动补 /v1/traces——
    Phoenix 基地址 http://localhost:6006，Langfuse 基地址
    http://localhost:3000/api/public/otel（Langfuse 的 OTLP 挂在公开 API 路径下）。
    """
    global _provider, _tracer
    endpoint = (endpoint or os.getenv("TRACING_ENDPOINT", "")).strip()
    if not endpoint:
        return False
    if _provider is not None:
        return True
    if not endpoint.rstrip("/").endswith("traces"):
        endpoint = endpoint.rstrip("/") + "/v1/traces"

    headers = {}
    for pair in os.getenv("TRACING_HEADERS", "").split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()

    provider = TracerProvider(resource=Resource.create(
        {"service.name": service_name or os.getenv("TRACING_SERVICE", "job-prep")}))
    if exporter is not None:
        # 测试注入点：InMemorySpanExporter 直接断言 span 树，不用起后端
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        # 批处理窗口：默认 5s（官方默认），用于压掉高频 span 的网络开销。
        # 调试/演示时用 TRACING_FLUSH_MS=500 换秒级可见性——观测延迟本质是
        # 「批处理窗口 + 导出耗时」，不是采集不到。
        delay_ms = int(os.getenv("TRACING_FLUSH_MS", "5000") or 5000)
        # 关键：绕开系统代理。requests 默认 trust_env=True，localhost 的 OTLP 请求
        # 会被 HTTP_PROXY 劫持，表现成「偶发 404 / span 丢一半」——和 phoenix_sync
        # 里同样的坑。观测链路自己必须是最稳的那一段。
        session = requests.Session()
        session.trust_env = False
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, headers=headers, session=session),
            schedule_delay_millis=delay_ms))
    # 注意：trace.set_tracer_provider 全局只能生效一次（第二次会被 API 忽略），
    # 所以 _tracer 必须直接从自己的 provider 取，否则重复 configure 后 span 会打到旧后端。
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # noqa: BLE001  已有全局 provider 时忽略，不影响本模块埋点
        pass
    _provider = provider
    _tracer = provider.get_tracer("job-prep.observability")
    return True


def _reset_for_test() -> None:
    """仅测试用：清掉 provider，恢复"未启用"状态。"""
    global _provider
    _provider = None


def _set_attrs(s, attrs: dict | None) -> None:
    for k, v in (attrs or {}).items():
        if v is None:
            continue
        if isinstance(v, str) and len(v) > MAX_ATTR_CHARS:
            v = v[:MAX_ATTR_CHARS] + f"…(+{len(v) - MAX_ATTR_CHARS})"
        try:
            s.set_attribute(k, v)
        except Exception:  # noqa: BLE001  观测失败绝不影响业务
            pass


def _with_context(attrs: dict | None) -> dict:
    """把 scene/session/turn 上下文并入 span 属性——调用方只管业务属性。"""
    out = dict(attrs or {})
    if _SCENE.get() and "llm.scene" not in out:
        out.setdefault("llm.scene", _SCENE.get())
    if _SESSION.get():
        out[ATTR_SESSION_ID] = _SESSION.get()
        if _TURN.get():
            out[ATTR_SESSION_TURN] = _TURN.get()
    return out


@contextmanager
def span(name: str, kind: str, attrs: dict | None = None):
    """通用 span。OTel 关闭时 yield None，业务方不需要分支。"""
    ensure_configured()
    if not enabled():
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        s.set_attribute(OI_KIND, kind)
        _set_attrs(s, _with_context(attrs))
        try:
            yield s
        except Exception as e:
            s.record_exception(e)
            s.set_status(trace.Status(trace.StatusCode.ERROR, str(e)[:300]))
            raise


@contextmanager
def agent_span(name: str, attrs: dict | None = None):
    """根 span。yield 出 trace_id 供落库关联；OTel 关闭时是本地 uuid。"""
    ensure_configured()
    if not enabled():
        yield uuid.uuid4().hex
        return
    with _tracer.start_as_current_span(name) as s:
        s.set_attribute(OI_KIND, KIND_AGENT)
        _set_attrs(s, _with_context(attrs))
        try:
            yield format(s.get_span_context().trace_id, "032x")
        except Exception as e:
            s.record_exception(e)
            s.set_status(trace.Status(trace.StatusCode.ERROR, str(e)[:300]))
            raise


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


# ---- 场景标签：观测是横切关注点，走上下文不走参数 ----
# _chat_json 有 7 个调用方，逐个穿参会漏；入口 set 一次，链路内所有 span 自动带上。
_SCENE: contextvars.ContextVar = contextvars.ContextVar("tracing_scene", default="")

# ---- Session / 轮次：多轮对话的客观侧 ----
# 一场会话（session）= 一次连续对话；一轮（turn）= 用户一句话触发的完整 agent 循环
# （含多轮工具调用）。span 自动带上 session.id / session.turn，Phoenix 按 session.id
# 聚合成 Sessions，前端据此做「会话 → 轮次 → trace」三级下钻。
_SESSION: contextvars.ContextVar = contextvars.ContextVar("tracing_session", default="")
_TURN: contextvars.ContextVar = contextvars.ContextVar("tracing_turn", default=0)

ATTR_SESSION_ID = "session.id"
ATTR_SESSION_TURN = "session.turn"


def set_session(session_id: str, turn: int = 0):
    """设置当前会话与轮次。console.chat 每轮入口调用一次；子 agent 自动继承。"""
    return _SESSION.set(session_id or ""), _TURN.set(int(turn or 0))


def current_session() -> str:
    return _SESSION.get()


def current_turn() -> int:
    return _TURN.get()


def add_event(s, name: str, **attrs) -> None:
    """给 span 记一个事件（关键瞬间：首 token、写库、文件落地、降级…）。

    事件和属性的区别：属性描述 span 整体，事件是 span 生命周期内的**时间点**——
    瀑布图上展开能看到「几点几分发生了什么」。OTel 关闭或 span 为 None 时静默跳过。
    """
    if s is None:
        return
    try:
        clean = {}
        for k, v in attrs.items():
            if v is None:
                continue
            if isinstance(v, str) and len(v) > MAX_ATTR_CHARS:
                v = v[:MAX_ATTR_CHARS] + f"…(+{len(v) - MAX_ATTR_CHARS})"
            clean[k] = v
        s.add_event(name, attributes=clean)
    except Exception:  # noqa: BLE001  观测失败绝不影响业务
        pass


def record_tool_action(s, result) -> None:
    """工具动作打点：从工具返回值里识别「改了什么」，记成 span 事件 + 属性。

    业界 agent 轨迹的可观测标配是动作级留痕（改了哪些文件/写了几条记录/动了哪个资源）。
    本项目的写动作统一体现在工具返回值上（inserted/drafted/retro_id/path…），
    这里做通用识别，避免每个工具实现都手动埋。
    """
    if s is None or not isinstance(result, dict):
        return
    try:
        action = {}
        for k in ("inserted", "drafted", "approved", "rejected", "deleted"):
            v = result.get(k)
            if isinstance(v, list) and v:
                action[f"写入 {k}"] = len(v)
            elif isinstance(v, int) and v:
                action[f"写入 {k}"] = v
        for k in ("retro_id", "run_id", "action_id", "question_id"):
            if result.get(k):
                action[k] = str(result[k])[:60]
        files = result.get("path") or result.get("paths") or result.get("file")
        if files:
            action["files"] = files if isinstance(files, str) else json.dumps(files, ensure_ascii=False)[:200]
        if not action:
            return
        parts = "；".join(f"{k}={v}" for k, v in action.items())
        s.set_attribute("tool.action", parts[:300])
        add_event(s, "tool.action", **{str(k): str(v) for k, v in action.items()})
    except Exception:  # noqa: BLE001
        pass


def set_scene(scene: str):
    return _SCENE.set(scene)


def current_scene() -> str:
    return _SCENE.get()
