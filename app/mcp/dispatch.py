"""工具调度：模型说要调工具之后，真正把它安全地执行掉。

原来的循环是"for tc in calls: 直接调函数"，四个漏洞：

1. **参数不校验**：模型给的 args 是自由 JSON，类型错了也照传，工具内部才炸，
   报错信息是 `TypeError: '<' not supported between int and str` —— 模型看不懂，只能瞎改。
2. **全串行**：模型一次给 3 个只读调用（查概况+查弱项+查高频题），串行执行
   白白多花 2 倍时间，而这三者互不依赖。
3. **不重试**：一次网络抖动就让整步失败，模型只能重新决策。
4. **结果硬截断**：`json_safe(result, 2600)` 之后模型再也看不到后半段，
   只能重新调一次同样的工具——花两倍 token 换同一份数据。

这里的顺序策略是"连续只读段并发、写操作单独串行"：
- 不能把写操作并发：同一批里"建卡 A"和"合并 B"可能互相影响；
- 也不能为了并发打乱语义顺序：模型给的顺序就是它推理的顺序。
所以按原序切段，连续只读并发，遇到写就 flush 等待，然后单独执行写。
"""
import concurrent.futures
import hashlib
import json
import time
from dataclasses import dataclass, field, replace

from ..core.db import get_conn

MAX_WORKERS = 3
TOOL_TIMEOUT_S = 90
TOOL_REF_CHARS = 3000      # 超过这个长度落库换 ref
PREVIEW_CHARS = 600
RETRY_READONLY = 1


@dataclass
class ToolOutcome:
    call_id: str
    name: str
    ok: bool
    payload: str
    error: str | None = None
    latency_ms: int = 0
    cached: bool = False
    retried: bool = False
    ref: str | None = None
    args: dict = field(default_factory=dict)
    read_only: bool = True


def _type_ok(value, want: str) -> bool:
    if want == "string":
        return isinstance(value, str)
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "array":
        return isinstance(value, list)
    if want == "object":
        return isinstance(value, dict)
    return True


def validate(params: dict, args: dict) -> list[str]:
    """轻量 JSON Schema 校验。不引依赖，只覆盖模型实际会犯的错。

    校验的是"能不能安全调用"，不做业务校验（那是工具自己的事）：
    类型不符、必填缺失、枚举越界——这三类占模型参数错误的大多数，
    且能在执行前拦下，省掉一次工具内部异常和一轮自我纠错。
    """
    errs: list[str] = []
    args = args if isinstance(args, dict) else {}
    props = (params or {}).get("properties") or {}
    for key in (params or {}).get("required") or []:
        if args.get(key) in (None, ""):
            errs.append(f"缺少必填参数 {key}")
    for key, v in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue                    # 未声明的字段放过：模型多给一个字段不算错
        want = spec.get("type")
        if want and v is not None and not _type_ok(v, want):
            errs.append(f"参数 {key} 类型应为 {want}，实际 {type(v).__name__}")
            continue
        enum = spec.get("enum")
        if enum and v not in enum:
            errs.append(f"参数 {key} 必须是 {enum} 之一，实际 {v!r}")
        max_len = spec.get("maxLength")
        if isinstance(v, str) and max_len and len(v) > max_len:
            errs.append(f"参数 {key} 超长（{len(v)} > {max_len}）")
    return errs


def _args_hash(name: str, args: dict) -> str:
    try:
        blob = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        blob = str(args)
    return hashlib.sha1(f"{name}:{blob}".encode()).hexdigest()[:16]


def _call_key(call: dict) -> str:
    """批内去重键。解析不了的参数各自成组（宁可多跑一次，也不能把两个不同调用合并）。"""
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (ValueError, TypeError):
        args = {"__unparsable__": fn.get("arguments")}
    return _args_hash(fn.get("name") or "", args if isinstance(args, dict) else {"_": args})


def _store_ref(payload: str, *, tool: str, args: dict, session_id: str | None,
               turn: int) -> str | None:
    """大结果落库换 ref。ref 用内容哈希，同一份数据重复出现不会堆行。"""
    ref = "tr-" + hashlib.sha1(payload.encode()).hexdigest()[:16]
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tool_results (ref,tool,args_json,content,chars,"
                "session_id,turn,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ref, tool, json.dumps(args, ensure_ascii=False)[:2000], payload, len(payload),
                 session_id, turn, time.strftime("%Y-%m-%dT%H:%M:%S")))
            conn.commit()
        finally:
            conn.close()
        return ref
    except Exception:  # noqa: BLE001  落库失败就退回纯截断，不能让工具结果丢掉
        return None


def fetch_result(ref: str, offset: int = 0, limit: int = 1500) -> dict:
    """按 ref 取回被截断的大结果片段。模型用它"翻页"，而不是重新调工具。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT content, chars, tool FROM tool_results WHERE ref=?",
                           (ref,)).fetchone()
        if not row:
            return {"ok": False, "error": f"结果不存在或已过期: {ref}"}
        content = row["content"]
        offset = max(0, min(int(offset or 0), len(content)))
        limit = max(100, min(int(limit or 1500), 4000))
        chunk = content[offset:offset + limit]
        return {"ok": True, "ref": ref, "tool": row["tool"], "total_chars": row["chars"],
                "offset": offset, "next_offset": offset + len(chunk) if offset + len(chunk) < len(content) else None,
                "chunk": chunk}
    finally:
        conn.close()


def _serialize(result, *, tool: str, args: dict, session_id: str | None, turn: int):
    """序列化 + 超长换 ref。返回 (payload, ref)。"""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            text = json.dumps(str(result), ensure_ascii=False)

    if len(text) <= TOOL_REF_CHARS:
        return text, None

    ref = _store_ref(text, tool=tool, args=args, session_id=session_id, turn=turn)
    if not ref:
        return text[:TOOL_REF_CHARS] + f"…（已截断，原长 {len(text)} 字）", None
    hint = {"_ref": ref, "_chars": len(text), "_preview": text[:PREVIEW_CHARS],
            "_next": "结果较长已分片保存；需要更多内容调用 fetch_tool_result(ref, offset)"}
    return json.dumps(hint, ensure_ascii=False), ref


def _exec_one(call: dict, impl: dict, schemas: dict, *, session_id: str | None,
              turn: int, readonly: bool, cache: dict, source: str = "console",
              skill: str = "-") -> ToolOutcome:
    name = (call.get("function") or {}).get("name") or ""
    raw = (call.get("function") or {}).get("arguments") or "{}"
    call_id = call.get("id") or ""
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        return ToolOutcome(call_id, name, False,
                           json.dumps({"ok": False, "error": "参数不是合法 JSON，请重新生成调用"},
                                      ensure_ascii=False),
                           error="bad_json", args={}, read_only=readonly)

    key = _args_hash(name, args)
    if readonly and key in cache:
        oc = cache[key]
        return ToolOutcome(call_id, name, oc.ok, oc.payload, oc.error, 0, cached=True,
                           ref=oc.ref, args=args, read_only=readonly)

    errs = validate(schemas.get(name) or {}, args)
    if errs:
        # 校验失败直接返回结构化错误：模型看到"哪个字段错了"能一次改对，
        # 比抛异常让它猜要快得多。
        return ToolOutcome(call_id, name, False,
                           json.dumps({"ok": False, "error": "参数校验失败",
                                       "details": errs}, ensure_ascii=False),
                           error="invalid_args", args=args, read_only=readonly)

    fn = impl.get(name)
    if fn is None:
        return ToolOutcome(call_id, name, False,
                           json.dumps({"ok": False, "error": f"未知工具 {name}"},
                                      ensure_ascii=False),
                           error="unknown_tool", args=args, read_only=readonly)

    # 平台级停用闸门：工具清单里已经不列它了，但历史消息的 tool_calls、技能白名单、
    # 编排的 ROLE_TOOLS 都可能还点名它。这里必须真的拦下来，否则"停用"只是个显示效果。
    from ..core import tool_flags
    why = tool_flags.blocked_reason(name)
    if why is not None:
        return ToolOutcome(call_id, name, False,
                           json.dumps({"ok": False, "error":
                                       f"工具 {name} 已被平台停用（{why or '未说明原因'}）。"
                                       "不要重试它；需要这类信息请换一个工具，"
                                       "或直接告诉用户这一步做不到"},
                                      ensure_ascii=False),
                           error="disabled", args=args, read_only=readonly)

    # span 开在执行处而不是调用处：并发段里每个工具各自计时，时长才是真的
    from ..observability import tracing as tr
    with tr.span(f"tool.{name}", tr.KIND_TOOL, {
            "tool.name": name, "tool.source": source, "tool.skill": skill,
            "tool.read_only": readonly, "input.value": json.dumps(args, ensure_ascii=False),
    }) as s:
        t0 = time.perf_counter()
        result, error, retried = None, None, False
        attempt = 0
        while True:
            try:
                result = fn(args)
                break
            except Exception as e:  # noqa: BLE001
                # 只读工具可以安全重试；写工具重试可能重复落库，一律只报错
                if readonly and attempt < RETRY_READONLY:
                    attempt += 1
                    retried = True
                    time.sleep(0.2 * attempt)
                    continue
                error = f"{type(e).__name__}: {e}"
                result = {"ok": False, "error": error}
                break

        latency = int((time.perf_counter() - t0) * 1000)
        if isinstance(result, dict) and result.get("error") and not error:
            error = str(result["error"])[:300]
        payload, ref = _serialize(result, tool=name, args=args, session_id=session_id, turn=turn)
        ok = error is None and not (isinstance(result, dict) and result.get("ok") is False)
        if s is not None:
            s.set_attribute("output.value", payload)
            s.set_attribute("tool.result_chars", len(payload))
            # ok / error_kind 必须上 span：span 上有这个，才能回答"这轮为什么失败"
            # 而不用再回查 tool_calls 表（跨表 join 一次就多一次口径不一致的机会）。
            s.set_attribute("tool.ok", ok)
            if not ok:
                s.set_attribute("tool.error_kind",
                                error if error in _ERROR_KINDS else "failed")
            if retried:
                s.set_attribute("tool.retried", True)
            if ref:
                s.set_attribute("tool.result_ref", ref)
            tr.record_tool_action(s, result if isinstance(result, dict) else None)

    oc = ToolOutcome(call_id, name, ok, payload, error, latency, retried=retried,
                     ref=ref, args=args, read_only=readonly)
    if readonly:
        cache[key] = oc
    return oc


_ERROR_KINDS = {"invalid_args", "unknown_tool", "timeout", "bad_json", "failed",
                "missing", "disabled"}


def record_calls(outcomes: list[ToolOutcome], *, source: str, skill: str,
                 session_id: str | None, turn: int, trace_id: str = "") -> None:
    """批量写 tool_calls 台账。

    早退路径（参数不合法 / 未知工具 / 超时）也要记——这些恰恰是最该看的：
    参数错说明 schema 或描述没写清，超时说明工具本身慢，两类问题的修法完全不同。
    一个事务批量写，减少并发线程抢 SQLite 写锁的机会。写失败直接放弃：
    台账是事后分析，不能因为它让用户拿不到工具结果。
    """
    if not outcomes:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload = []
    for oc in outcomes:
        if not oc.name:
            continue
        kind = oc.error if oc.error in _ERROR_KINDS else ("failed" if oc.error else None)
        payload.append((
            now, oc.name, source or "console", skill or "-", session_id, int(turn or 0),
            trace_id or None,
            1 if oc.read_only else 0, "ok" if oc.ok else "error", kind,
            (oc.error or "")[:300] or None, int(oc.latency_ms or 0),
            len(oc.payload or ""), 1 if oc.retried else 0, 1 if oc.cached else 0,
            oc.ref, now))
    if not payload:
        return
    try:
        conn = get_conn()
        try:
            conn.executemany(
                "INSERT INTO tool_calls (ts,tool,source,skill,session_id,turn,trace_id,"
                "read_only,status,error_kind,error,latency_ms,result_chars,retried,cached,"
                "ref,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def run_batch(calls: list[dict], impl: dict, schemas: dict, *, readonly_fn,
              session_id: str | None = None, turn: int = 0,
              source: str = "console", skill: str = "-") -> list[ToolOutcome]:
    """按原序执行一批 tool_calls：连续只读段并发，写操作串行。"""
    calls = list(calls or [])
    if not calls:
        return []
    from ..observability import tracing as tr

    # contextvars 不下传线程池：不显式继承的话，这批 tool span 的 parent 全是 None，
    # 瀑布图上三个并行工具会变成三棵独立的根——"并发"被读成"三次不相干的调用"。
    tctx = tr.capture()
    results: list[ToolOutcome | None] = [None] * len(calls)
    cache: dict = {}
    i = 0
    while i < len(calls):
        name = (calls[i].get("function") or {}).get("name") or ""
        if readonly_fn(name):
            j = i
            while j < len(calls) and readonly_fn((calls[j].get("function") or {}).get("name") or ""):
                j += 1
            batch = calls[i:j]
            if len(batch) == 1:
                results[i] = _exec_one(batch[0], impl, schemas, session_id=session_id,
                                       turn=turn, readonly=True, cache=cache,
                                       source=source, skill=skill)
            else:
                # 批内去重必须在**并发之前**完成：两个线程同时查 cache 会都判"未命中"，
                # 同一个查询被跑两遍——去重退化成概率行为（实测 1/2 概率）。
                # 所以先按 (name, args_hash) 分组，每组只提交一次，其余复制结果。
                groups: dict[str, list[int]] = {}
                for k in range(len(batch)):
                    groups.setdefault(_call_key(batch[k]), []).append(k)
                leader = {idxs[0]: key for key, idxs in groups.items()}
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(MAX_WORKERS, len(batch))) as ex:
                    def _run(c, _ctx=tctx):
                        tr.adopt(_ctx)
                        return _exec_one(c, impl, schemas, session_id=session_id,
                                         turn=turn, readonly=True, cache=cache,
                                         source=source, skill=skill)

                    futs = {ex.submit(_run, batch[idx]): idx for idx in leader}
                    try:
                        for fut in concurrent.futures.as_completed(futs, timeout=TOOL_TIMEOUT_S):
                            idx = futs[fut]
                            try:
                                results[idx] = fut.result()
                            except Exception as e:  # noqa: BLE001
                                nm = (calls[idx].get("function") or {}).get("name") or ""
                                results[idx] = ToolOutcome(
                                    calls[idx].get("id") or "", nm, False,
                                    json.dumps({"ok": False, "error": f"执行失败: {e}"},
                                               ensure_ascii=False), error="failed")
                    except concurrent.futures.TimeoutError:
                        pass
                # 组内复制：跟随者共用队首结果并标 cached（它确实没真跑）。
                # 队首失败也照样复制——同一参数再跑一遍大概率还是同样失败，
                # 白花一次调用，还会让"工具报错"的统计翻倍失真。
                for idxs in groups.values():
                    src = results[idxs[0]]
                    if src is None:
                        continue
                    for k in idxs[1:]:
                        if results[k] is None:
                            results[k] = replace(src, call_id=calls[k].get("id") or "",
                                                 cached=True)
                # 超时没返回的，补一条错误结果——循环必须继续，不能被一个卡死的工具拖住
                for idx in range(i, j):
                    if results[idx] is None:
                        nm = (calls[idx].get("function") or {}).get("name") or ""
                        results[idx] = ToolOutcome(
                            calls[idx].get("id") or "", nm, False,
                            json.dumps({"ok": False,
                                        "error": f"工具超过 {TOOL_TIMEOUT_S}s 未返回，已跳过"},
                                       ensure_ascii=False), error="timeout")
            i = j
        else:
            results[i] = _exec_one(calls[i], impl, schemas, session_id=session_id,
                                   turn=turn, readonly=False, cache=cache,
                                   source=source, skill=skill)
            i += 1
    outcomes = [r if r is not None else ToolOutcome("", "", False, '{"ok":false}', "missing")
                for r in results]
    # trace_id 在主线程取一次：工具跑在并发线程里，contextvar 不跨线程，
    # 让每个 _exec_one 自己去取会全部拿到空值。
    from ..observability import tracing as tr
    record_calls(outcomes, source=source, skill=skill, session_id=session_id, turn=turn,
                 trace_id=tr.current_trace_id())
    return outcomes


def build_schemas(tools: list[dict]) -> dict:
    """从 OpenAI tools 列表抽出 {name: parameters} 供校验用。"""
    out = {}
    for t in tools or []:
        fn = t.get("function") or {}
        if fn.get("name"):
            out[fn["name"]] = fn.get("parameters") or {}
    return out
