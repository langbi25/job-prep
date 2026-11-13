"""LLM 客户端（OpenAI 兼容协议）。

所有 prompt 统一经 prompts.py 注册表加载（YAML 版本化），
本模块只负责：渲染 → 调用 → JSON 解析。prompt 内容看 prompts/*.yaml。
未配置 LLM_API_KEY 时依赖 LLM 的功能抛 RuntimeError，其余功能不受影响。
"""
import json
import os
import re
import threading
import time
from contextlib import contextmanager

import httpx

from . import budget, config, pricing, prompts

_call_count = 0  # 进程级统计（评测脚本用）
_last_usage: dict | None = None   # 最近一次调用的真实 token 用量，用来校准本地估算


class LLMError(RuntimeError):
    """LLM 调用失败。带状态码、响应体片段与请求摘要，便于区分「该重试」和「该修请求」。

    retryable 只对 408/409/429/5xx 为真：400 是请求体本身不合法（上下文超限、
    messages 结构错），用同一个 body 重试必然同样失败，白花一次钱。
    """

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMConcurrencyError(LLMError):
    """拿不到并发额度（排队超时）。

    retryable=True：排队失败通常是"现在正忙"，稍等重试往往就好——它和 429 的
    语义一致，调用方的重试逻辑不用为它单独写分支。
    """

    def __init__(self, message: str):
        super().__init__(message, retryable=True)


# ---- 上游并发池 ----
# 编排三步并行 + 记忆抽取 + 用户主对话 + 后台起草，全部走 _chat_json 这个咽喉。
# 没有池的时候，"编排某步循环重试"或"批量建卡跑飞"会把上游并发打满，
# 表现为所有请求一起变慢、一起超时——故障面是全局的。有池之后，超出的调用
# 排队等待，等不到的快速失败并明确报"并发已满"，故障被限制在超出者自己身上。
_pool_lock = threading.Lock()
_pool: threading.BoundedSemaphore | None = None
_pool_size = 0


def _get_pool() -> tuple[threading.BoundedSemaphore, int]:
    """按 env 取池。env 变了（测试/热调）就重建——旧池里还挂着的调用方
    release 到旧池上，不影响新池的正确性，只是少了一个计数。"""
    global _pool, _pool_size
    size = max(int(os.getenv("LLM_MAX_CONCURRENCY", "4") or 4), 1)
    with _pool_lock:
        if _pool is None or _pool_size != size:
            _pool = threading.BoundedSemaphore(size)
            _pool_size = size
    return _pool, size


def _acquire_pool() -> tuple[threading.BoundedSemaphore, float]:
    """拿一个并发额度，返回 (池, 等待毫秒)。等不到就抛 LLMConcurrencyError。

    为什么用 acquire(timeout=) 而不是无限等：无限等会把"上游已经过载"变成
    "请求挂着不响应"——用户看到的不是报错而是转圈，比失败更难诊断。
    排队上限由 LLM_ACQUIRE_TIMEOUT 控制，超时快速失败。
    """
    pool, size = _get_pool()
    timeout = max(float(os.getenv("LLM_ACQUIRE_TIMEOUT", "30") or 30), 0)
    t0 = time.perf_counter()
    if not pool.acquire(timeout=timeout):
        waited = (time.perf_counter() - t0) * 1000
        err = LLMConcurrencyError(
            f"LLM 并发池已满（上限 {size}），等待 {timeout:g}s 未拿到额度。"
            "大概率有编排/批量任务占满上游；稍后重试，或调大 LLM_MAX_CONCURRENCY。")
        # 排队失败也要落失败账：它没花 token，但"这次调用尝试发生过、没发出去"
        # 必须可见——否则上游过载时台账一片平静，用户端却全在报错。
        _usage_failed(err, waited)
        raise err
    return pool, (time.perf_counter() - t0) * 1000


@contextmanager
def _pool_slot():
    """占一个并发额度，离开 with 自动归还——即使里面抛异常。

    归还必须是 finally 语义：失败调用（网络错、解析错）最容易漏 release，
    漏一次池就永久少一格，几次之后表现成"莫名其妙全部排队超时"。
    """
    pool, wait_ms = _acquire_pool()
    try:
        yield wait_ms
    finally:
        pool.release()


def stats() -> dict:
    return {"calls": _call_count, "last_usage": _last_usage}


def last_usage() -> dict | None:
    """最近一次 API 返回的 usage（prompt_tokens 等），用于校准 `context.count_tokens` 的估算。"""
    return _last_usage


def extract_json(text: str) -> dict:
    """健壮 JSON 提取：剥 markdown 围栏 → 整体解析 → 截取首个完整对象。

    模型即使被 response_format=json_object 约束，仍会偶发两种走样：
    输出 ```json 围栏，或把两个 JSON 对象拼在一起（"Extra data: line 1 column N"）。
    这类失败会直接打崩 json_validity，所以解析必须容错且只在一处实现。
    """
    if not isinstance(text, str):
        return text
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start >= 0:
        # raw_decode 只消费第一个完整对象，天然忽略后面的冗余内容
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"无法从模型输出中解析出 JSON：{raw[:200]}")


def _brief(body: dict) -> str:
    """请求体摘要，用于 400 诊断。

    roles 序列是最有信息量的一项：如果出现连续 user、或 tool 消息前面没有
    对应的 assistant.tool_calls，一眼就能看出来。只报状态码的话全是猜。
    """
    msgs = body.get("messages") or []
    chars = sum(len(str(m.get("content") or "")) for m in msgs)
    roles = "".join((m.get("role") or "?")[0] for m in msgs)
    return (f"messages={len(msgs)} 条/{chars} 字符, "
            f"tools={len(body.get('tools') or [])}, roles={roles[:80]}")


def _fail(resp, body: dict) -> None:
    """把 HTTP 错误的响应体裁进异常，而不是只留一个状态码。

    raise_for_status() 丢掉的正是最有用的部分：DeepSeek 的 400 body 里写着
    到底哪一项不合法。线上 60% 的编排失败率曾经只能看到「400 Bad Request」，
    既定位不了也判断不了该不该重试——那等于没有错误处理。
    """
    if resp.status_code < 400:
        return
    try:
        # 流式响应此时还没被读过，resp.text 会直接抛——错误原因恰恰全在这里。
        # 先 read() 再解码；错误响应体很小，读完不心疼。
        detail = (resp.read() or b"").decode("utf-8", "replace").strip()[:800]
        if not detail:
            detail = "<空响应体>"
    except Exception:  # noqa: BLE001  流式响应读不出 body 时不能掩盖原始错误
        detail = "<响应体不可读>"
    raise LLMError(f"LLM {resp.status_code}: {detail}\n请求摘要: {_brief(body)}",
                   status=resp.status_code,
                   retryable=resp.status_code in (408, 409, 429, 500, 502, 503, 504))


def _consume_stream(body: dict, on_delta=None):
    """流式消费一次 chat/completions。返回 (msg, usage, ttft_ms)。

    为什么流式：非流式拿不到首 token 时间（TTFT），而 TTFT 是 LLM 可观测的
    核心体验指标——用户感知的"卡"取决于 TTFT + 生成速度，不是总耗时。
    stream_options.include_usage 让最后一个 chunk 带真实 usage（DeepSeek/OpenAI 均支持）。
    tool_calls 的 delta 按 index 聚合：首个 delta 带 id/name，后续 chunk 只补 arguments 片段。
    on_delta(content_text)：内容增量回调，边收边转发给调用方（SSE 流式回复）。
    只转发 content；tool_calls 的参数碎片对用户没有可读性，不转发。
    """
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    t0 = time.perf_counter()
    ttft = None
    finish = None
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    usage = None
    with httpx.stream(
        "POST", f"{config.LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        json=body, timeout=90,
    ) as resp:
        _fail(resp, body)
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    if ttft is None:
                        ttft = round((time.perf_counter() - t0) * 1000, 1)
                    content_parts.append(delta["content"])
                    if on_delta is not None:
                        on_delta(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index") or 0
                    acc = tool_calls.setdefault(
                        idx, {"id": "", "type": "function",
                              "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        if ttft is None:
                            ttft = round((time.perf_counter() - t0) * 1000, 1)
                        acc["function"]["arguments"] += fn["arguments"]
    msg = {"content": "".join(content_parts) or None}
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    # finish_reason 说明"为什么停"：length=被 max_tokens 截断（答案不完整），
    # tool_calls=它在等工具，stop=正常说完。这是判断"回复太短是不是模型的问题"
    # 的唯一依据，不记就永远只能猜。
    msg["_finish"] = finish
    return msg, usage, ttft


def _usage_of(usage: dict | None, latency_ms: float | None = None,
              finish_reason: str | None = None, retried: bool = False) -> dict:
    """从 API usage 提取计费要素并落台账。

    缓存命中 token 两个来源都要兜：DeepSeek 顶层给 prompt_cache_hit_tokens，
    OpenAI 风格藏在 prompt_tokens_details.cached_tokens。
    返回的 dict 会挂在 msg["_usage"] 上——**调用方必须从这里取用量**，
    不能用 llm.last_usage()：那是进程级全局变量，编排三步并行时会串账。
    """
    u = usage or {}
    pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    cached = int(u.get("prompt_cache_hit_tokens") or 0)
    if not cached:
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    from ..observability import tracing as tr  # 惰性导入：otel 重依赖不拖累普通调用
    model = config.LLM_MODEL
    latency = round(latency_ms) if latency_ms is not None else None
    row = pricing.record_usage(model, prompt_tokens=pt, completion_tokens=ct,
                               cached_tokens=cached,
                               scene=tr.current_scene(),
                               session_id=tr.current_session() or None,
                               turn=tr.current_turn(),
                               trace_id=tr.current_trace_id(),
                               latency_ms=latency, finish_reason=finish_reason,
                               retried=retried)
    return {"model": model, "prompt_tokens": pt, "completion_tokens": ct,
            "cached_tokens": cached, "cost_cny": row["cost_cny"],
            "price_estimated": bool(row["price_estimated"]),
            "latency_ms": latency, "finish_reason": finish_reason,
            "retried": bool(retried)}


def _usage_failed(err: BaseException, latency_ms: float | None = None) -> None:
    """失败调用也落一行。**只记事实，不记推测**：token 记 0（API 没返回 usage），
    费用记 0（真的没花），但"调过、失败了、错了什么"必须留下。

    没有这一行，"这轮调了 5 次" 在台账里可能只有 3 行，稳定性问题查不出来。
    """
    from ..observability import tracing as tr
    try:
        pricing.record_usage(config.LLM_MODEL, scene=tr.current_scene(),
                             session_id=tr.current_session() or None,
                             turn=tr.current_turn(), trace_id=tr.current_trace_id(),
                             latency_ms=round(latency_ms) if latency_ms is not None else None,
                             status="error", error=f"{type(err).__name__}: {err}")
    except Exception:  # noqa: BLE001  失败记账再失败，只能放弃
        pass


def _ensure_json_hint(messages: list[dict]) -> None:
    """DeepSeek 用 response_format=json_object 时，要求 prompt 里出现 "json"，否则 400。

    这不是格式洁癖，是硬性前置条件。编排里各步骤的 prompt 由模型生成，有的步骤
    提到「输出 JSON」，有的只写「给出结论」——于是同一份代码在不同步骤随机 400，
    表现为「5 个计划里 3 个失败，失败步骤各不相同」。加一句声明比重试有用得多：
    重试改变不了 prompt 里没有这个词的事实。
    """
    for m in messages:
        if "json" in str(m.get("content") or "").lower():
            return
    for m in reversed(messages):
        if m.get("role") == "user":
            m["content"] = (m.get("content") or "") + "\n\n请只输出 JSON。"
            return
    messages.append({"role": "user", "content": "请只输出 JSON。"})


def _chat_json(system: str | None, user: str | None, tools: list | None = None,
               messages: list | None = None, temperature: float = 0.1,
               json_mode: bool = True, on_delta=None) -> dict:
    """发起对话补全。tools 模式返回原始 message（可能含 tool_calls），否则返回解析后的 JSON message。

    json_mode=False 用于"我要的是一段话，不是 JSON"的场景（如摘要压缩）：
    强制 json_object 时模型会把整段文字塞进 {"content": "..."} 里，
    下游拿到的是 JSON 字符串而不是摘要——这个坑真的踩过。
    on_delta(content_text)：流式内容增量回调。注意调用方拿到的增量**不保证是
    最终回复**——带工具调用的轮次也可能先吐一段文字；调用方必须在轮次结束后
    依据是否带 tool_calls 决定保留还是丢弃（console 流式协议就是这么做的）。
    非流式路径（fallback / LLM_STREAM=0）结束时整段补发一次，两条路径对调用方等价。
    """
    global _call_count, _last_usage
    if not config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY 未配置（见 .env），无法调用 LLM")
    # 预算闸门放在这个咽喉上：所有 LLM 调用都从这里过，包括编排 worker、
    # 后台起草、记忆抽取——放在各业务入口一定会漏掉某条旁路。
    # 它抛的是 BudgetExceeded，不该被当成网络错误重试。
    budget.check()
    if messages is None:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    # 浅拷贝一层：下面可能往消息里补「请只输出 JSON」，不能改到调用方传进来的列表
    # （编排层会把同一份 messages 反复用于多轮，被改一次就永久多一句）
    messages = [dict(m) for m in messages]
    body = {"model": config.LLM_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    elif json_mode:
        body["response_format"] = {"type": "json_object"}
        _ensure_json_hint(messages)
    # LLM span：这是此前完全没留痕的一段——prompt、返回、token 数、耗时、TTFT，
    # 是排查"模型当时看到了什么"的第一现场。属性对齐 GenAI/OpenInference 语义约定。
    from ..observability import tracing as tr
    stream = os.getenv("LLM_STREAM", "1") != "0"
    # 并发额度在 span 之外拿：排队时间不属于"这次调用"的耗时，混进 duration
    # 会把上游过载误读成模型变慢；等待时长单独记进 span 属性，过载一眼可见。
    with _pool_slot() as pool_wait_ms, tr.span("llm.chat", tr.KIND_LLM, {
        tr.ATTR_MODEL: config.LLM_MODEL,
        "llm.scene": tr.current_scene() or "unknown",
        "llm.n_messages": len(messages),
        "llm.tools": len(tools) if tools else 0,
        "input.value": json.dumps(messages, ensure_ascii=False),
        "input.mime_type": "application/json",
        "llm.temperature": temperature,
        "llm.stream": stream,
    }) as s:
        if s is not None and pool_wait_ms > 50:
            s.set_attribute("llm.pool_wait_ms", round(pool_wait_ms, 1))
        usage = None
        fell_back = False
        t_req = time.perf_counter()
        try:
            if stream:
                msg, usage, ttft = _consume_stream(body, on_delta=on_delta)
                if s is not None and ttft is not None:
                    s.set_attribute("llm.ttft_ms", ttft)
                    tr.add_event(s, "first_token", ttft_ms=ttft)
            else:
                resp = httpx.post(
                    f"{config.LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                    json=body, timeout=90,
                )
                _call_count += 1
                _fail(resp, body)
                data = resp.json()
                usage = data.get("usage")
                msg = data["choices"][0]["message"]
                msg["_finish"] = data["choices"][0].get("finish_reason")
                if on_delta is not None and msg.get("content"):
                    on_delta(msg["content"])
        except Exception as e_first:
            # 先落失败账再看要不要降级：一次失败就是一次真实发生过的调用，
            # 不记的话"这轮很慢"永远查不出是模型在重试。
            _usage_failed(e_first, (time.perf_counter() - t_req) * 1000)
            if not stream:
                raise
            # 流式失败降级非流式重试一次：可用性优先于观测细节。
            # 但两次都失败时必须把「首次」的错误一起报出来——只报第二次，
            # 会让人以为是非流式的问题，而根因通常在第一次（同一个 body，
            # 换个传输方式救不了非法的请求体）。
            tr.add_event(s, "stream_fallback", reason="stream failed, retry non-stream")
            fell_back = True
            try:
                resp = httpx.post(
                    f"{config.LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                    json=body, timeout=90,
                )
                _call_count += 1
                _fail(resp, body)
                data = resp.json()
                usage = data.get("usage")
                msg = data["choices"][0]["message"]
                msg["_finish"] = data["choices"][0].get("finish_reason")
                if on_delta is not None and msg.get("content"):
                    on_delta(msg["content"])
            except Exception as e_second:  # noqa: BLE001
                raise LLMError(
                    f"流式与非流式均失败（同一请求体）。\n首次: {e_first}\n重试: {e_second}",
                    status=getattr(e_first, "status", None),
                    retryable=getattr(e_first, "retryable", False),
                ) from e_second
        _call_count += 1
        _last_usage = usage
        # 成本核算：每次调用落一行台账（scene/session/turn 从 tracing 上下文取），
        # 并把 usage 挂回 msg["_usage"]——调用方从这取，不用进程级 last_usage
        # （编排三步并行时会互相覆盖，步费用会串账）。
        finish = msg.pop("_finish", None)
        u = _usage_of(usage, (time.perf_counter() - t_req) * 1000,
                      finish_reason=finish, retried=fell_back)
        msg["_usage"] = u
        if s is not None:
            pt, ct = u["prompt_tokens"], u["completion_tokens"]
            # 双写：gen_ai.* 是 OTel GenAI 语义约定；llm.token_count.* 是 OpenInference
            # 约定（Phoenix 的 token 统计列只认这一套——只写 gen_ai 时面板永远显示 0）。
            s.set_attribute(tr.ATTR_PROMPT_TOKENS, pt)
            s.set_attribute(tr.ATTR_COMPLETION_TOKENS, ct)
            s.set_attribute("llm.token_count.prompt", pt)
            s.set_attribute("llm.token_count.completion", ct)
            s.set_attribute("llm.token_count.total", pt + ct)
            s.set_attribute("llm.cost_cny", u["cost_cny"])
            # 为什么停：length=撞了 max_tokens（改预算）/ stop=模型主动收尾（改 prompt）
            # / tool_calls=还在查（等它）。这个字段是"答案为什么短"的唯一直接答案。
            if finish:
                s.set_attribute("llm.finish_reason", finish)
            if fell_back:
                s.set_attribute("llm.stream_fallback", True)
            tr.add_event(s, "gen_ai.usage", prompt_tokens=pt, completion_tokens=ct,
                         cached_tokens=u["cached_tokens"], cost_cny=u["cost_cny"])
            s.set_attribute("output.value", json.dumps(msg.get("content") or "", ensure_ascii=False))
            s.set_attribute("output.mime_type", "application/json")
        return msg


def chat_parsed(system: str, user: str, **kw) -> dict:
    """非 tools 场景的统一入口：调用 → 解析 → 解析失败重试一次（附纠错指令）。

    重试不是免费的：两次调用的 token/费用都真实发生，所以把 _usage 做成
    累计值挂在解析结果上——编排的 plan 级费用从这里取，漏记重试就少算钱。
    """
    total: dict = {}

    def merge(u: dict) -> None:
        for k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            total[k] = total.get(k, 0) + int(u.get(k) or 0)
        total["cost_cny"] = round(total.get("cost_cny", 0.0) + (u.get("cost_cny") or 0.0), 6)
        total["model"] = u.get("model") or total.get("model")

    msg = _chat_json(system, user, **kw)
    merge(msg.get("_usage") or {})
    try:
        data = extract_json(msg["content"])
    except (ValueError, json.JSONDecodeError):
        repair = [{"role": "system", "content": system},
                  {"role": "user", "content": user},
                  {"role": "assistant", "content": (msg.get("content") or "")[:4000]},
                  {"role": "user", "content": "上面的输出不是合法 JSON（可能有多余内容或围栏）。"
                                              "请只输出一个 JSON 对象，不要任何解释、不要代码块。"}]
        msg2 = _chat_json(None, None, messages=repair, temperature=0)
        merge(msg2.get("_usage") or {})
        data = extract_json(msg2["content"])
    data["_usage"] = dict(total)
    return data


# ============ 面经采集（阶段 1） ============

_COMPLETENESS = ("none", "partial", "complete")
_DOC_TYPES = ("interview", "single_question", "answer_snippet")


def _as_text(v) -> str:
    """答案字段可能是 str / list / dict，统一成一段可直接写库的纯文本。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return "\n".join(f"- {str(x).strip()}" for x in v if str(x).strip())
    if isinstance(v, dict):
        return "；".join(f"{k}：{x}" for k, x in v.items() if str(x).strip())
    return str(v).strip()


def extract_interview(raw_text: str, source_url: str = "") -> dict:
    """抽取 + 判定（doc_type / 有无答案 / 答案完整度）。

    **为什么判定必须在这里做**：它决定后面走哪条路——答案完整 → 用原文规范化成卡
    （低风险，仍走审批但可批量通过）；没有答案 → LLM 从零起草（必须逐条人审）。
    判定错 = 数据流向错，所以下面逐项校正，不信任模型自报：
      - doc_type 用题目条数硬校正（两道以上不可能是"单题摘录"）；
      - has_answer 由 answer_completeness 派生，不单独采信；
      - 声称有答案但 answer 抽不出来 → 降级 none（没有正文就不算有答案）；
      - 有正文却被判 none → 升到 partial（有证据就留，交人审，不静默丢内容）；
      - 自称 complete 却列出了 gaps → 自相矛盾，降级 partial；
      - 非面经材料里公司/轮次若在原文中找不到字面依据 → 清空（防幻觉）。
    """
    system, user, version = prompts.render(
        "extract_interview", source_url=source_url or "未知", raw_text=raw_text[:12000])
    data = chat_parsed(system, user)

    raw_qs = data.get("questions")
    if not isinstance(raw_qs, list):
        raw_qs = []
    questions = []
    for q in raw_qs:
        if not isinstance(q, dict):
            continue
        text = _as_text(q.get("text"))
        if not text:
            continue
        answer = _as_text(q.get("answer"))
        comp = str(q.get("answer_completeness") or "").strip().lower()
        if comp not in _COMPLETENESS:
            comp = "partial" if answer else "none"
        if not answer:
            comp = "none"
        elif comp == "none":
            comp = "partial"
        gaps = q.get("answer_gaps")
        gaps = [str(g).strip() for g in gaps if str(g).strip()] if isinstance(gaps, list) else []
        if comp == "complete" and gaps:
            comp = "partial"
        questions.append({
            "order": len(questions) + 1,
            "text": text,
            "qtype": (q.get("qtype") or "").strip() or "八股",
            "topic_hint": (q.get("topic_hint") or "").strip() or None,
            "has_answer": comp != "none",
            "answer_completeness": comp,
            "answer": answer or None,
            "answer_gaps": gaps,
            # True = 需要模型从零起草（没有现成答案可规范化）；只有 complete 才是 false。
            # 注意这不等于"不建卡"：留用的题都建卡，这个字段只用来区分走哪条 prompt。
            "suggest_build_card": comp != "complete",
        })
    data["questions"] = questions

    dt = str(data.get("doc_type") or "").strip().lower()
    if dt not in _DOC_TYPES:
        dt = "interview" if len(questions) >= 2 else "single_question"
    if len(questions) >= 2:
        dt = "interview"
    data["doc_type"] = dt
    if dt != "interview":
        for k in ("company", "position", "round", "interview_date"):
            v = data.get(k)
            if v and str(v).strip() not in raw_text:
                data[k] = None
        data["result"] = "unknown"

    data["judgement"] = {
        "doc_type": dt,
        "n_questions": len(questions),
        "n_bare": sum(1 for q in questions if q["answer_completeness"] == "none"),
        "n_partial": sum(1 for q in questions if q["answer_completeness"] == "partial"),
        "n_complete": sum(1 for q in questions if q["answer_completeness"] == "complete"),
        "suggest_build": [q["order"] for q in questions if q["suggest_build_card"]],
    }
    data["_prompt"] = {"name": "extract_interview", "version": version}
    return data


def tag_card(title: str, content: str) -> dict:
    system, user, version = prompts.render("tag_card", title=title, content=content[:6000])
    data = chat_parsed(system, user)
    data["_prompt"] = {"name": "tag_card", "version": version}
    return data


# ============ 模拟面试（阶段 3~5） ============

def generate_questions(direction: str, jd_text: str, n: int, avoid: list[str]) -> list[dict]:
    system, user, _ = prompts.render(
        "generate_questions", direction=direction, jd_text=jd_text or "无", n=n,
        avoid=json.dumps(avoid[:20], ensure_ascii=False))
    data = chat_parsed(system, user)
    return [q for q in data.get("questions", []) if q.get("text")][:n]


def evaluate_answer(question: dict, answer: str, persona: str, context: str = "") -> dict:
    """逐题评分。context 由 context_builder 组装（知识库校准材料，上下文工程）。"""
    system, user, version = prompts.render(
        "evaluate_answer", persona=persona or "互联网大厂资深面试官",
        qtype=question.get("qtype", ""), question_text=question["text"],
        answer=answer[:6000], context=context)
    data = chat_parsed(system, user)
    data.setdefault("scores", {"correctness": 0, "depth": 0, "structure": 0, "highlight": 0})
    data["_prompt"] = {"name": "evaluate_answer", "version": version}
    return data


# ============ 上下文压缩（阶段 8） ============

def summarize_context(prev_summary: str, transcript: str) -> str:
    """把被窗口挤出去的旧对话压成三段式要点。

    注意这里**不能**用 chat_parsed：那条路会强制 response_format=json_object，
    而摘要要的是给人/模型看的三段短文，压成 JSON 反而更难注入。
    """
    system, user, _ = prompts.render(
        "ctx_summarize", prev=(prev_summary or "（无）")[:1500], transcript=transcript[:12000])
    msg = _chat_json(system, user, temperature=0.2, json_mode=False)
    return (msg.get("content") or "").strip()


def overall_report(direction: str, persona: str, qa_records: list[dict]) -> dict:
    system, user, _ = prompts.render(
        "overall_report", persona=persona or "互联网大厂资深面试官", direction=direction,
        qa_records="\n\n".join(
            f"题：{r['text']}（均分{round(sum((r.get('scores') or {'a': 0}).values()) / 4, 1)}）\n"
            f"回答摘要：{(r.get('answer') or '(跳过)')[:300]}\n点评：{(r.get('feedback') or '')[:200]}"
            for r in qa_records))
    return chat_parsed(system, user)
