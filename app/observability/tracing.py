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

5. **trace_id 必须走 contextvar，不能只靠 OTel 上下文**（2026-11-12 修）。
   原来的 current_trace_id() 直接读 OTel span context，OTel 没配 endpoint 时
   永远返回空串——于是 llm_usage.trace_id 全空，194 行里 95 行是空的。
   "串表"是打点能不能用的分水岭：llm_usage / tool_calls / turn_facts 三张表
   如果只能靠 (session_id, turn) 对齐，跨进程、跨线程、子 agent 的调用就全是孤儿。
   所以 agent_span 进入时把 trace_id 塞进 _TRACE，current_trace_id() 先读它。
   OTel 在场时两者是同一个值，不在场时也照样有——落库口径不随观测开关分叉。

6. **span 本地持久化，OTel 降级为"可选的更好看的视图"**（2026-11-12 夜）。
   原来的设计里 span 只走 OTLP，没配 TRACING_ENDPOINT 就彻底 no-op：本地一条
   调用树都没有，出了问题只能看散在四张表里的行，拼不出"谁调了谁、慢在哪一层"。
   更糟的是崩溃场景——观测本来就是为崩溃准备的，而崩溃那一刻 span 还在
   BatchSpanProcessor 的队列里没导出去，最该看的那棵树正好没了。

   现在 span 一律落 spans 表（同步写，span 结束即入库，崩溃不丢已结束的），
   OTel 有配置就顺带导出。两个后果要说清楚：
   - 代价：每个 span 一次 INSERT+commit。实测 WAL 下 0.2~1ms，一轮 30 个 span
     约 10ms，占一轮对话 0.1%。**用 10ms 换"崩了还能查"，值得。**
   - 边界：busy_timeout 500ms，抢不到锁就丢这一行并计数（_STATS["fails"]）。
     宁可少一行观测，不能因为观测把对话卡住——这条优先级不能反。

7. **span() 永远返回 LocalSpan，不再是 None**。
   调用方全都有 `if s is not None` 保护，当年是为了"没起后端就跳过"。
   但那些被跳过的恰恰是最有价值的事实：工具输出长度、检索命中数、首 token 延迟、
   token 用量。让它们只在装了 Phoenix 时才存在，是本末倒置。
   现在 OTel 关着也返回真实对象，属性和事件照记、照落库。
"""
import contextvars
import datetime
import gzip
import hashlib
import itertools
import json
import os
import sqlite3
import threading
import time
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

# 会话与轮次：所有 span 自动带上，前端据此做「会话 → 轮次 → trace」三级下钻
ATTR_SESSION_ID = "session.id"
ATTR_SESSION_TURN = "session.turn"

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
    # 刻意不调用 trace.set_tracer_provider()。原因不是洁癖，是实测：
    # FastAPI 0.162+ 自带 ASGI telemetry，激活条件是「全局 TracerProvider 已被配置」，
    # 一旦注册全局，Docker healthcheck（每 15s 一次 /health）就会给每个请求生成
    # fastapi.endpoint / dependencies / serialization + GET /health 四条 span。
    # 实测 12 小时里 8535 条 span 有 8504 条（99.6%）是这类噪音，把真实业务链路淹没了。
    #
    # 我们的 span 全部由 _tracer 直接从自有 provider 取（见下），不依赖全局注册；
    # 换句话说：只有显式埋的点才出去，第三方库的自动埋点一律不出去。
    _provider = provider
    _tracer = provider.get_tracer("job-prep.observability")
    return True


def _reset_for_test() -> None:
    """仅测试用：清掉 provider，恢复"未启用"状态。"""
    global _provider
    _provider = None


def _set_attrs(s, attrs: dict | None, clip: bool = False) -> None:
    """批量写属性。

    clip 只对 OTel 侧开：那边一个超大 attribute 会让 collector 丢掉整批 span。
    本地 span 不能裁——set_attribute 自己会把超长值转存全文，裁在前面等于
    在全文入库之前就把它切了（曾经就是这样，blob 里存的是 2010 字的截断串）。
    """
    for k, v in (attrs or {}).items():
        if v is None:
            continue
        if clip and isinstance(v, str) and len(v) > MAX_ATTR_CHARS:
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


# ==================== 本地 span 持久化 ====================
# 落库不是"顺手存一份"，它是主存：OTel 是旁路导出。判据见文件顶部第 6 条。

SPAN_MAX_ATTR_CHARS = 800     # 属性表里单值的**展示**上限（超出的全文另存，见下）
SPAN_MAX_ATTR_JSON = 8000     # 整条属性表的预算，超了按长度裁
SPAN_MAX_EVENTS = 24          # 单 span 事件上限（首 token / 降级 / 工具动作）
SPAN_RETENTION_DAYS = 14

# ---- 大文本：全文一律留，但不睡在属性表里 ----
# 判据和采样那条是一回事：**"值不值得留"不能在写入时判断**。一个工具调用当时
# 看着平平无奇，出问题时你要看的恰恰是它的入参。所以全文都留，只把"展示"截短。
SPAN_BLOB_MIN_CHARS = SPAN_MAX_ATTR_CHARS          # 超过展示上限就转存全文
SPAN_BLOB_MAX_CHARS = 400_000                      # 单条熔断：防止把整个文件塞进来
SPAN_MAX_ROWS = 200_000       # 容量保险丝；正常量级（14 天）远到不了
SPAN_KEEP_SLOW_MS = 5000      # 容量清理时"慢调用"的豁免线：慢的就是证据

# 必留属性：它们是查询维度（GROUP BY / 过滤条件），被裁掉就等于这行没法归组。
# 大字段（input.value 的 prompt 全文、output.value 的工具返回）才是该让位的。
SPAN_KEEP_KEYS = frozenset({
    OI_KIND, "tool.name", "tool.source", "tool.skill", "tool.read_only",
    "llm.scene", "llm.stream", "agent.mode", "agent.skill", "skill.name",
    ATTR_SESSION_ID, ATTR_SESSION_TURN,
})

_SEQ = itertools.count(1)          # 同 trace 内开始顺序，毫秒撞车时靠它稳定排序
_span_local = threading.local()    # 每个线程自己的写库连接
# 观测自己的健康度：写不进去必须能被看见。blob 单独计数——
# 属性写成功而全文没写成功，是"看着有树、点开没证据"，比整行丢了更隐蔽。
_STATS = {"writes": 0, "fails": 0, "blob_writes": 0, "blob_fails": 0,
          "blob_dedup": 0, "clipped": 0, "pruned": 0}


def persist_enabled() -> bool:
    """SPAN_PERSIST=0 可关。压测/评测里关掉，避免观测写库拉长基准。"""
    return os.getenv("SPAN_PERSIST", "1").strip().lower() not in ("0", "false", "off", "no")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _mark(n: int) -> str:
    """全文标记。前端靠它认出"这行被截了、能点开看全文"，所以格式必须稳定。
    ⟦⟧ 这对括号在日志/prompt 里几乎不会自然出现，不会误判。"""
    return f"…⟦全文 {n} 字符⟧"


def _clip(v):
    """属性值的展示裁剪。**裁剪不等于丢弃**——超长值的全文由 _put 转存 span_blobs。"""
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if not isinstance(v, str):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            v = str(v)
    return v if len(v) <= SPAN_MAX_ATTR_CHARS else \
        v[:SPAN_MAX_ATTR_CHARS - 40] + _mark(len(v))


def _preview(text: str) -> str:
    """截断后的展示串。预留 40 字符给标记，保证"预览 + 标记"仍 ≤ 上限——
    否则 _shrink_attrs 会二次裁剪，正好把标记切掉，前端就再也点不开全文了。"""
    return text[:SPAN_MAX_ATTR_CHARS - 40] + _mark(len(text))


def _blob_hash(text: str) -> str:
    """正文指纹。32 位十六进制（sha256 前 16 字节）——128 位碰撞概率在"个人项目
    几百万条文本"的量级上可以忽略，而比 64 位短一半，索引更小。

    哈希算在**存下来的**文本上（可能已被单条上限截断），不是原文：
    两个都被截到 40 万字的超长文档会得到同一个指纹——这正确，因为存的确实是同一段。
    "这一份原本多长"另记在 span_blobs.text_len，两者不一致就说明存的时候截过。
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _shrink_attrs(attrs: dict) -> dict:
    """把属性表压进预算：先保维度键，再按体积从小到大填。

    为什么按"从小到大"填而不是"按重要性"：重要性判断不了，但体积能。
    小字段通常是维度（条数、布尔、模型名），大字段通常是内容（prompt 全文）。
    填到最后没位置的，必然是那几个大块头——正确。

    注意大块头此时已经只剩 800 字预览（全文在 span_blobs），所以这条预算
    现在裁的是"属性条数太多"，不再会裁掉证据本身。
    """
    # 下划线开头的是本模块自己的元数据（_blob_keys / _dropped_keys），不参与裁剪，
    # 也不该被当成"调用方打的属性"去排序。
    meta = {k: v for k, v in attrs.items() if k.startswith("_")}
    body = {k: v for k, v in attrs.items() if not k.startswith("_")}
    clipped = {k: _clip(v) for k, v in body.items() if v is not None}
    keep = {k: v for k, v in clipped.items()
            if k in SPAN_KEEP_KEYS or k.startswith("session.")}
    rest = {k: v for k, v in clipped.items() if k not in keep}
    out = dict(keep)
    used = len(json.dumps({**out, **meta}, ensure_ascii=False))
    dropped = []
    for k, v in sorted(rest.items(), key=lambda kv: len(str(kv[1]))):
        cost = len(k) + len(str(v)) + 6
        if used + cost > SPAN_MAX_ATTR_JSON:
            dropped.append(k)
            continue
        out[k] = v
        used += cost
    out.update(meta)
    if dropped:
        # 裁掉了什么必须写进 span 自己，否则"属性少"会被误读成"没打这个点"。
        # 被裁的键若在 _blobs 里有全文，前端照样能点开——裁的只是预览。
        out["_dropped_keys"] = ",".join(sorted(dropped))[:300]
        _STATS["clipped"] += 1
    return out


def _span_conn() -> sqlite3.Connection | None:
    """独立连接，不复用 db.get_conn()。

    复用会撞上业务事务：get_conn 借出时发现 in_transaction 就 rollback（那是防腐设计），
    而 span 写入是"随手插一行就 commit"，会把业务攒了一半的写事务提前结束掉。
    观测绝不能改变被观测对象的行为，所以宁可自己开一条连接。

    busy_timeout 只给 500ms：抢不到锁就丢这行。观测不能成为对话的阻塞源。
    """
    from ..core import db as _db

    # 读 db.DB_PATH 而不是 from ... import DB_PATH：测试会换库，
    # 导入时抓下来的常量会一直指向旧路径，写进一个已经删掉的库文件。
    key = str(_db.DB_PATH)
    conn = getattr(_span_local, "conn", None)
    if conn is not None and getattr(_span_local, "key", "") == key:
        return conn
    if conn is not None:                       # 换库了（测试），旧连接作废
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        conn = sqlite3.connect(key, timeout=0.5)
        conn.execute("PRAGMA busy_timeout = 500")
        conn.execute("SELECT 1 FROM spans LIMIT 1")
    except Exception:  # noqa: BLE001  表不存在（老库没迁移）就整体关闭，别每 span 报一次
        try:
            conn.close()                      # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        _span_local.conn = None
        _span_local.key = key
        return None
    _span_local.conn = conn
    _span_local.key = key
    return conn


def _persist(sp: "LocalSpan") -> None:
    # 只由 _end() 调用一次；幂等由 _end 的 _done 保证，这里不再判 _done
    # （判了会把"结束"和"已写"混成同一个状态，结果一行都写不进去）。
    if not persist_enabled():
        return
    conn = _span_conn()
    if conn is None:
        _STATS["fails"] += 1
        return
    try:
        # 全文和属性行在**同一个事务**里：只成功一半会出现"能点开但点开是空的"，
        # 那比整行丢了更难判断。所以宁可整行回滚，也不留半条记录。
        if sp._blobs:
            for key, (text, orig_len) in sp._blobs.items():
                raw = text.encode("utf-8", "replace")
                gz = gzip.compress(raw, 6)
                h = _blob_hash(text)
                # 正文按内容收敛：同一段文本被两层各记一次时只存一份。
                # 当前埋点下重复很少（实测 43 条引用里 1 条），价值在"防"不在"省"——
                # 一旦有 span 开始记同一段 prompt，重复会成倍长，而那时再改存储格式
                # 要搬几十 MB 的 blob。代价只是一次 INSERT OR IGNORE。
                cur = conn.execute(
                    "INSERT OR IGNORE INTO blob_texts "
                    "(hash,text_len,gz_len,gz,created_at) VALUES (?,?,?,?,?)",
                    (h, len(text), len(gz), gz, sp._end_ts))
                if cur.rowcount == 0:      # 已存在：这次只加引用，不再占磁盘
                    _STATS["blob_dedup"] += 1
                # 新行的 gz 留空，正文只在 blob_texts 里存一份；老行（hash IS NULL）
                # 仍然自带 gz，读路径两种都认。
                conn.execute(
                    "INSERT OR REPLACE INTO span_blobs "
                    "(span_id,key,text_len,gz_len,gz,hash,created_at) VALUES (?,?,?,?,?,?,?)",
                    (sp.span_id, key, orig_len, len(gz), b"", h, sp._end_ts))
                _STATS["blob_writes"] += 1
            # 属性表里标明哪些键有全文可点：前端不靠猜正则，直接读这份清单。
            sp.attrs["_blob_keys"] = ",".join(sorted(sp._blobs))[:300]
        conn.execute(
            "INSERT OR REPLACE INTO spans (span_id,trace_id,parent_id,name,kind,"
            "session_id,turn,seq,start_ts,end_ts,duration_ms,status,error,"
            "attrs_json,events_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sp.span_id, sp.trace_id, sp.parent_id, sp.name, sp.kind,
             sp.session_id, sp.turn, sp.seq, sp._start_ts, sp._end_ts,
             sp.duration_ms, sp.status, (sp.error or "")[:500] or None,
             json.dumps(sp.attrs, ensure_ascii=False),
             json.dumps(sp.events, ensure_ascii=False), sp._end_ts))
        conn.commit()
        _STATS["writes"] += 1
    except Exception:  # noqa: BLE001  写不进去也不能打断业务
        _STATS["fails"] += 1
        if sp._blobs:
            _STATS["blob_fails"] += len(sp._blobs)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


class SpanContext:
    """get_span_context() 的最小返回：OTel 那边是个对象，这边给同样的两个 id。"""

    __slots__ = ("trace_id", "span_id")

    def __init__(self, trace_id: str, span_id: str):
        self.trace_id = int(trace_id, 16) if trace_id else 0
        self.span_id = int(span_id, 16) if span_id else 0


class LocalSpan:
    """本地 span：OTel Span 的最小子集（set_attribute/add_event/record_exception）。

    为什么不让它继承或者包装 OTel Span：OTel 的 span 只在 enabled() 时存在，
    而我们要的恰恰是"没有 OTel 时也有完整对象"。所以它自己就是事实，
    OTel span 只是额外挂一个 _otel 引用，属性双写。
    """

    __slots__ = ("name", "kind", "span_id", "trace_id", "parent_id", "seq",
                 "session_id", "turn", "attrs", "events", "status", "error",
                 "duration_ms", "_start_ts", "_end_ts", "_perf0", "_otel",
                 "_done", "_tok", "_blobs")

    def __init__(self, name: str, kind: str, attrs: dict | None = None, *,
                 trace_id: str = "", parent_id: str | None = None):
        self.name = name
        self.kind = kind
        self.span_id = uuid.uuid4().hex[:16]
        self.trace_id = trace_id or uuid.uuid4().hex
        self.parent_id = parent_id
        self.seq = next(_SEQ)
        self.session_id = _SESSION.get() or None
        self.turn = int(_TURN.get() or 0)
        self._blobs: dict[str, str] = {}
        raw = {}
        for k, v in (attrs or {}).items():
            self._ingest(k, v, raw)
        self.attrs = _shrink_attrs(raw)
        self.events: list[dict] = []
        self.status = "ok"
        self.error = None
        self.duration_ms = 0
        self._start_ts = _now_iso()
        self._end_ts = None
        self._perf0 = time.perf_counter()
        self._otel = None
        self._done = False
        self._tok = None

    # --- OTel Span 的最小兼容面 ---
    def _ingest(self, key: str, value, into: dict) -> None:
        """属性值的归一化入口：定长值进 into，超长值的全文进 _blobs、预览进 into。

        所有写属性的路径（构造入参 / set_attribute / set_attributes）都走这里，
        否则"从入参来的 prompt 没全文、从 set_attribute 来的有全文"这种不一致，
        排查时会让人怀疑到底是没打点还是没存。
        """
        if value is None:
            return
        if isinstance(value, (bool, int, float)):
            into[key] = value
            self._blobs.pop(key, None)
            return
        if not isinstance(value, str):
            try:
                value = json.dumps(value, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                value = str(value)
        if len(value) > SPAN_BLOB_MIN_CHARS:
            # 熔断：大于上限的直接截到上限存全文，并留痕说明"原文更长"。
            # 不熔断的话，一次 write_file 传进来整篇文档就能让这行 blob 到几十 MB。
            # 同时记下**原文**长度：只记截断后的长度，事后就分不清"原文就这么长"
            # 和"我们只存了一半"——这个区别决定了要不要去别处找完整数据。
            self._blobs[key] = (value[:SPAN_BLOB_MAX_CHARS], len(value))
            into[key] = _preview(value)
        else:
            self._blobs.pop(key, None)     # 同 key 后来被更短的值覆盖，旧全文作废
            into[key] = value

    def set_attribute(self, key: str, value) -> None:
        if value is None:
            return
        self._ingest(key, value, self.attrs)
        if self._otel is not None:
            try:
                # OTLP 侧只送预览：后端是给人看的视图，不需要吃 40KB 的 prompt，
                # 而一个超大 attribute 会让导出的整批 span 被 collector 丢掉。
                self._otel.set_attribute(key, self.attrs.get(key))
            except Exception:  # noqa: BLE001
                pass

    def set_attributes(self, attrs: dict) -> None:
        for k, v in (attrs or {}).items():
            self.set_attribute(k, v)

    def add_event(self, name: str, attributes: dict | None = None, **kw) -> None:
        """事件是 span 生命周期里的**时间点**，属性是整体描述。

        带相对毫秒 t：瀑布图上要能看出"第 800ms 才拿到首 token"，
        只记"有一次首 token"等于没记。

        事件属性同样有"全文"：工具动作的 args、降级原因里的原始报错、首 token
        时带的思考片段，都可能上千字。和 span 属性用两套处理是不行的——
        排查时最想看的往往正是事件里那段被截掉的东西。

        blob key 用 `e{事件序号}.{属性名}`：序号在 events 数组里天然稳定
        （只 append 不重排），前端渲染时手上就有下标，能直接拼出同一个 key。
        不能用事件名，同一个 span 里 "retry" 会出现好几次。
        """
        if len(self.events) >= SPAN_MAX_EVENTS:
            return
        idx = len(self.events)
        a = dict(attributes or {})
        a.update(kw)
        slim: dict = {}
        for k, v in a.items():
            if v is None:
                continue
            if isinstance(v, (bool, int, float)):
                slim[k] = v
                continue
            if not isinstance(v, str):
                try:
                    v = json.dumps(v, ensure_ascii=False)
                except Exception:  # noqa: BLE001
                    v = str(v)
            if len(v) > SPAN_BLOB_MIN_CHARS:
                bkey = f"e{idx}.{k}"
                self._blobs[bkey] = (v[:SPAN_BLOB_MAX_CHARS], len(v))
                slim[k] = _preview(v)
            else:
                slim[k] = v
        self.events.append({
            "t": int((time.perf_counter() - self._perf0) * 1000),
            "n": name,
            "a": slim,
        })
        if self._otel is not None:
            try:
                # OTel 的 add_event 只吃 None/bool/int/float/str，别的一律转字符串；
                # 但**数值不要转**——转了之后后端里的 ttft_ms 变成 "321.5"，
                # 聚合时就无法排序、无法求平均。
                self._otel.add_event(name, attributes={
                    k: (v if v is None or isinstance(v, (bool, int, float, str)) else str(v))
                    for k, v in slim.items()})
            except Exception:  # noqa: BLE001
                pass

    def record_exception(self, exc: BaseException) -> None:
        self.status = "error"
        self.error = f"{type(exc).__name__}: {exc}"
        if self._otel is not None:
            try:
                self._otel.record_exception(exc)
            except Exception:  # noqa: BLE001
                pass

    def set_status(self, status, message: str = "") -> None:
        # 兼容 OTel 的 Status 对象与本地字符串两种写法
        code = getattr(status, "status_code", status)
        self.status = "error" if str(code).upper().endswith("ERROR") or code == "error" else "ok"
        if message:
            self.error = str(message)[:500]
        if self._otel is not None:
            try:
                self._otel.set_status(status, message)
            except Exception:  # noqa: BLE001
                pass

    def get_span_context(self) -> SpanContext:
        return SpanContext(self.trace_id, self.span_id)

    def __repr__(self) -> str:  # 调试时能直接看出是哪一层
        return f"<LocalSpan {self.name} {self.span_id[:8]} {self.duration_ms}ms>"


def _begin(name: str, kind: str, attrs: dict | None, *, trace_id: str = "") -> LocalSpan:
    """开一个本地 span 并把它设为当前——父子关系靠 contextvar，不靠 OTel。"""
    sp = LocalSpan(name, kind, _with_context(attrs),
                   trace_id=trace_id or _TRACE.get(),
                   # 空串必须变 None：根 span 用 `parent_id IS NULL` 判定，
                   # 存成 '' 的话"找根"永远查不到，整棵树会显示成没有根。
                   parent_id=_CUR_SPAN.get() or None)
    sp._tok = _CUR_SPAN.set(sp.span_id)
    return sp


def _end(sp: LocalSpan) -> None:
    if sp._done:
        return
    sp._done = True
    sp._end_ts = _now_iso()
    sp.duration_ms = int((time.perf_counter() - sp._perf0) * 1000)
    # 结束前按最终属性重算一次预算：__init__ 时只有入参，执行过程中
    # set_attribute 又加了 output.value / token 用量，那时不重算就会超预算。
    sp.attrs = _shrink_attrs(sp.attrs)
    # session/turn 回填：根 span 是在 chat() 里开的，而 session_id 和轮次号要等
    # _chat_inner 数完历史消息才知道——开 span 时抓不到。不回填的话，
    # 调用树里最顶上那一行 session_id 永远是空，按会话查树直接查不到自己那一轮。
    if not sp.session_id:
        sp.session_id = _SESSION.get() or None
    if not sp.turn:
        sp.turn = int(_TURN.get() or 0)
    if sp._tok is not None:
        try:
            _CUR_SPAN.reset(sp._tok)
        except Exception:  # noqa: BLE001  跨线程 reset 会失败，忽略即可
            pass
    _persist(sp)


@contextmanager
def span(name: str, kind: str, attrs: dict | None = None):
    """通用 span。**永远** yield 一个 LocalSpan（OTel 关着也是），见顶部第 7 条。"""
    ensure_configured()
    cm = _tracer.start_as_current_span(name) if enabled() else None
    otel = None
    if cm is not None:
        otel = cm.__enter__()
        otel.set_attribute(OI_KIND, kind)
        _set_attrs(otel, _with_context(attrs), clip=True)
    sp = _begin(name, kind, attrs)
    _set_attrs(sp, _with_context(attrs))
    sp._otel = otel
    try:
        yield sp
    except Exception as e:
        sp.record_exception(e)
        if otel is not None:
            try:
                otel.set_status(trace.Status(trace.StatusCode.ERROR, str(e)[:300]))
            except Exception:  # noqa: BLE001
                pass
        _end(sp)
        if cm is not None:
            try:
                cm.__exit__(type(e), e, e.__traceback__)
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        _end(sp)
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


@contextmanager
def agent_span(name: str, attrs: dict | None = None):
    """根 span。yield 出 trace_id 供落库关联。

    trace_id 三个来源，优先级从高到低：
    1. OTel 在场 → 用 OTel span 的 trace_id（这样本地库和 Phoenix 里能对上号）；
    2. 已在某条链路里（子 agent / 编排步骤）→ 继承，不另起一棵树；
    3. 都没有 → 自己生成。**绝不返回空串**——空 trace_id 等于这条链路没有身份。
    """
    ensure_configured()
    cm = _tracer.start_as_current_span(name) if enabled() else None
    otel = None
    tid = ""
    if cm is not None:
        otel = cm.__enter__()
        otel.set_attribute(OI_KIND, KIND_AGENT)
        _set_attrs(otel, _with_context(attrs), clip=True)
        tid = format(otel.get_span_context().trace_id, "032x")

    sp = _begin(name, KIND_AGENT, attrs, trace_id=tid)
    _set_attrs(sp, _with_context(attrs))
    sp._otel = otel
    tok = _TRACE.set(sp.trace_id)
    try:
        yield sp.trace_id
    except Exception as e:
        sp.record_exception(e)
        _end(sp)
        _TRACE.reset(tok)
        if cm is not None:
            try:
                cm.__exit__(type(e), e, e.__traceback__)
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        _end(sp)
        _TRACE.reset(tok)
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def capture() -> dict:
    """抓一份链路上下文，交给线程池/新线程继承。

    contextvars 不进 ThreadPoolExecutor：工具在并发线程里跑，如果不管，
    那批 tool span 的 parent 全是 None，瀑布图上它们会飘在根旁边变成平行根——
    "并行执行"被读成"三次独立调用"，是观测在撒谎。
    """
    return {"trace_id": _TRACE.get(), "span_id": _CUR_SPAN.get(),
            "scene": _SCENE.get(), "session_id": _SESSION.get(), "turn": _TURN.get()}


def adopt(ctx: dict | None) -> None:
    """在子线程开头恢复 capture() 的结果。"""
    if not ctx:
        return
    _TRACE.set(ctx.get("trace_id") or "")
    _CUR_SPAN.set(ctx.get("span_id") or "")
    _SCENE.set(ctx.get("scene") or "")
    _SESSION.set(ctx.get("session_id") or "")
    _TURN.set(int(ctx.get("turn") or 0))


def span_stats() -> dict:
    """落库健康度。写失败率高说明观测本身在丢数据，这比任何指标都优先。"""
    return dict(_STATS, enabled=persist_enabled())


def close_span_conn() -> None:
    """关掉本线程的 span 写库连接。

    只有两个调用场景，和 db.close_thread_conns 一样：测试隔离（换库前不关，
    Windows 上临时目录因句柄占用删不掉）和进程退出。
    """
    conn = getattr(_span_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _span_local.conn = None
    _span_local.key = ""


def prune_spans(days: int = SPAN_RETENTION_DAYS, max_rows: int = SPAN_MAX_ROWS) -> int:
    """两级清理，保留单位是**一条 trace**，不是一行 span。

    这不是"采样"，恰恰是不采样的代价与补偿：
    - 按天清理：太老的整条链路留不住（留了也没人看），但**只要这条 trace 里有
      一个 error 或一个慢 span，整条都留到 3 倍期**。
    - 按容量清理：只在行数超过保险丝时触发，从最老的开始删，跳过受保护的 trace，
      也跳过 error 与 duration_ms >= SPAN_KEEP_SLOW_MS 的散行。

    为什么从"按 span 保留"改成"按 trace 保留"：只留那个慢 span，瀑布图上它周围
    全是空的——"慢在哪一层、谁等的谁"恰恰要靠上下文回答，而上下文被一起删了。
    留一行没有上下游的证据，等于留了个孤证。

    为什么不直接 head-based 采样（按比例随机留）：span 开始时**还不知道**会不会
    失败、会不会慢，随机采样必然按比例丢掉一部分错误和慢调用。观测的价值集中在
    长尾，按比例丢长尾等于把最有用的部分丢掉。要采只能 tail-based（结束时按结果
    决定留不留），而 tail-based 的实现就是"先全存、后按规则删"——也就是这个函数。
    """
    from ..core.db import get_conn

    d = max(int(days), 1)
    # 两档保留期：普通 trace 到点就删；含 error 或慢 span 的留 3 倍时间。
    # 不是永久——永久保留等于把"证据"变成"债务"，表无界增长后查询会先垮，
    # 那时候连新问题都查不了。3 倍是"够看清一个问题的来龙去脉"和"不撑爆库"的折中。
    soft = (datetime.datetime.now() - datetime.timedelta(days=d)
            ).isoformat(timespec="milliseconds")
    hard = (datetime.datetime.now() - datetime.timedelta(days=d * 3)
            ).isoformat(timespec="milliseconds")
    conn = get_conn()
    try:
        # 受保护 trace 先钉进临时表，而不是写成相关子查询：同一条 DELETE 里
        # 边删边查自己，结果取决于行的处理顺序——先删掉那个慢 span，兄弟行就
        # 会被判成"不需要保护"跟着删，整条链路被连坐清空。
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _keep_traces (trace_id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _keep_traces")
        conn.execute(
            "INSERT OR IGNORE INTO _keep_traces (trace_id) "
            "SELECT DISTINCT trace_id FROM spans WHERE status='error' OR duration_ms >= ?",
            (SPAN_KEEP_SLOW_MS,))
        cur = conn.execute(
            "DELETE FROM spans WHERE start_ts < ? "
            "AND trace_id NOT IN (SELECT trace_id FROM _keep_traces)", (soft,))
        gone = cur.rowcount or 0
        # 超过 3 倍期一律删（含受保护的 trace）。两档的意义是"普通 14 天 / 证据 42 天"，
        # 不是"证据永久"；到点连证据也放掉，才叫有界。
        cur = conn.execute("DELETE FROM spans WHERE start_ts < ?", (hard,))
        gone += cur.rowcount or 0
        conn.commit()
        _purge_blobs(conn)

        caps = int(max_rows or 0)
        if caps > 0:
            total = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
            over = total - caps
            if over > 0:
                victims = conn.execute(
                    "SELECT span_id FROM spans WHERE status='ok' AND duration_ms < ? "
                    "AND trace_id NOT IN (SELECT trace_id FROM _keep_traces) "
                    "ORDER BY start_ts ASC LIMIT ?",
                    (SPAN_KEEP_SLOW_MS, over)).fetchall()
                if victims:
                    ids = [r["span_id"] for r in victims]
                    marks = ",".join("?" for _ in ids)
                    conn.execute(f"DELETE FROM spans WHERE span_id IN ({marks})", ids)
                    conn.execute(f"DELETE FROM span_blobs WHERE span_id IN ({marks})", ids)
                    conn.commit()
                    gone += len(ids)
                    # 容量清理会删掉**还能看**的调用树，必须让它可被察觉：
                    # 某天发现 trace 少了，得能区分"没采"和"被容量挤掉了"。
                    _STATS["pruned"] += len(ids)
        return gone
    finally:
        conn.close()


def _purge_blobs(conn: sqlite3.Connection) -> int:
    """清掉孤儿 blob（span 已删）。按 span_id 反查而不是按时间：blob 的时间
    跟 span 一致，但 span 可能因为容量被单独删掉，光看时间会漏。

    正文（blob_texts）是多个 span 共享的，**不能跟着引用一起删**：同一个 hash
    可能还被别的 trace 引着，删早了就是"点开是空的"——这种损坏不会报错，
    只会在某天查历史时突然发现证据没了。所以只回收"已经没有任何引用的"。
    """
    cur = conn.execute(
        "DELETE FROM span_blobs WHERE span_id NOT IN (SELECT span_id FROM spans)")
    gone = cur.rowcount or 0
    conn.execute(
        "DELETE FROM blob_texts WHERE hash NOT IN "
        "(SELECT hash FROM span_blobs WHERE hash IS NOT NULL)")
    conn.commit()
    return gone


def current_trace_id() -> str:
    """当前链路的 trace_id。

    先读 contextvar 而不是先读 OTel：OTel 关闭时 span context 无效，
    只读它会返回空串，把"观测没开"变成"这条调用没有身份"——两者不是一回事。
    """
    local = _TRACE.get()
    if local:
        return local
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


def set_trace(trace_id: str):
    """后台线程接手一次调用时用：contextvar 不跨线程，必须显式继承。"""
    return _TRACE.set(trace_id or "")


# ---- 场景标签：观测是横切关注点，走上下文不走参数 ----
# _chat_json 有 7 个调用方，逐个穿参会漏；入口 set 一次，链路内所有 span 自动带上。
_SCENE: contextvars.ContextVar = contextvars.ContextVar("tracing_scene", default="")

# ---- Session / 轮次：多轮对话的客观侧 ----
# 一场会话（session）= 一次连续对话；一轮（turn）= 用户一句话触发的完整 agent 循环
# （含多轮工具调用）。span 自动带上 session.id / session.turn，Phoenix 按 session.id
# 聚合成 Sessions，前端据此做「会话 → 轮次 → trace」三级下钻。
_SESSION: contextvars.ContextVar = contextvars.ContextVar("tracing_session", default="")
_TURN: contextvars.ContextVar = contextvars.ContextVar("tracing_turn", default=0)
# 当前链路的 trace_id。OTel 在场时与 span context 同值，不在场时是本地 uuid——
# 落库关联不能依赖"用户有没有起观测后端"（definition 在 agent_span 之前被引用，
# 靠模块级延迟解析，见文件顶部说明第 5 条）。
_TRACE: contextvars.ContextVar = contextvars.ContextVar("tracing_trace", default="")
# 当前 span 的 id。父子关系不靠 OTel 的 context，靠自己这个栈——
# 否则"OTel 没开就没有父子关系"会变成事实，而本地调用树恰恰是 OTel 关着时最需要的。
_CUR_SPAN: contextvars.ContextVar = contextvars.ContextVar("tracing_span", default="")


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
