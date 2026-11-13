"""成本核算：单价表 + 用量台账 + 聚合查询。

**为什么必须有一张台账而不是从 span 反推**：观测后端（Phoenix/Langfuse）记了
token，但它是"运维视角"——按 trace 散落、没折算钱、也不归业务场景管。
回答"这个技能一次调用花多少钱""编排比单 agent 贵几倍"必须有一张按调用逐行
记账的表，且写入点在 llm._chat_json 这个唯一咽喉——所有 LLM 调用都从这过，
漏不掉。

**单价怎么定**：写死在表里但允许环境变量整体覆盖（COST_PRICE_JSON）。
DeepSeek 单价（CNY / 1M tokens，2025-02 公布价，改动频繁以官网为准）：
  deepseek-chat     输入 2.0（缓存命中 0.5）  输出 8.0
  deepseek-reasoner 输入 4.0（缓存命中 1.0）  输出 16.0
未登记的模型按"当前默认模型价"估算并打 estimated 标记——宁可标估算也不
返回 0，0 元会让成本视图看起来"免费"，比贵了更危险。

**collect() 上下文收集器**：编排的 plan 级费用没法从步骤直接汇总（合成/自检
在主线程单独调用），用 contextvar 收集器把一段代码块内的所有调用费用攒起来。
注意 contextvar 不跨线程传播——这正好：worker 线程里 run_step 自己用
msg["_usage"] 记账（顺带修掉并行时进程级 last_usage 串账的隐患），
收集器只负责主线程的 plan/synthesize/critique。
"""
import datetime
import json
import os
from contextvars import ContextVar
from contextlib import contextmanager

from . import budget, db

# ---- 单价表：CNY / 1M tokens ----
PRICES: dict[str, dict] = {
    "deepseek-chat": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
    "deepseek-reasoner": {"input": 4.0, "cached_input": 1.0, "output": 16.0},
}
# 环境变量整体覆盖（JSON：{"model": {"input": .., "cached_input": .., "output": ..}}）
_raw = os.getenv("COST_PRICE_JSON", "").strip()
if _raw:
    try:
        _over = json.loads(_raw)
        if isinstance(_over, dict):
            PRICES.update({str(k).lower(): dict(v) for k, v in _over.items()})
    except (ValueError, TypeError):
        pass  # 配置写坏不能用异常打断启动，退回内置表

# 未登记模型的兜底单价：跟随当前默认模型，标 estimated
DEFAULT_PRICE = dict(PRICES.get(os.getenv("LLM_MODEL", "deepseek-chat").lower())
                     or {"input": 2.0, "cached_input": 0.5, "output": 8.0})


def price_for(model: str) -> dict:
    """查单价。返回 {input, cached_input, output, estimated}。"""
    p = PRICES.get((model or "").lower())
    if p:
        return {**p, "estimated": False}
    return {**DEFAULT_PRICE, "estimated": True}


def cost_cny(model: str, prompt_tokens: int, completion_tokens: int,
             cached_tokens: int = 0) -> float:
    """一次调用的费用（元）。缓存命中部分按 cached_input 价计。

    cached_tokens 超过 prompt_tokens 时按 prompt 全命中算（上游脏数据兜底，
    否则负数输入会算出负费用，聚合后悄悄把总账拉低）。
    """
    p = price_for(model)
    cached = min(max(int(cached_tokens or 0), 0), max(int(prompt_tokens or 0), 0))
    fresh = max(int(prompt_tokens or 0), 0) - cached
    ct = max(int(completion_tokens or 0), 0)
    cost = (fresh * p["input"] + cached * p["cached_input"] + ct * p["output"]) / 1e6
    return round(cost, 6)


# ---- collect() 收集器：主线程内一段代码块的费用归属 ----
_ACC: ContextVar[tuple | None] = ContextVar("cost_acc", default=None)


@contextmanager
def collect():
    """收集代码块内所有 LLM 调用的 usage（不跨线程——并行步骤走 msg._usage）。"""
    rows: list[dict] = []
    tok = _ACC.set(rows)
    try:
        yield rows
    finally:
        _ACC.reset(tok)


def _collect_into(row: dict) -> None:
    acc = _ACC.get()
    if acc is not None:
        acc.append({k: row[k] for k in ("model", "prompt_tokens", "completion_tokens",
                                        "cached_tokens", "cost_cny")})


def accumulate(rows: list[dict]) -> dict:
    """把收集到的 usage 行聚合成一个汇总 dict。"""
    out = {"calls": len(rows), "prompt_tokens": 0, "completion_tokens": 0,
           "cached_tokens": 0, "cost_cny": 0.0}
    for r in rows:
        out["prompt_tokens"] += r.get("prompt_tokens", 0)
        out["completion_tokens"] += r.get("completion_tokens", 0)
        out["cached_tokens"] += r.get("cached_tokens", 0)
        out["cost_cny"] += r.get("cost_cny", 0.0)
    out["cost_cny"] = round(out["cost_cny"], 6)
    out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


def snapshot() -> dict:
    """当前收集器里已经攒下的用量。

    给"跑完一段代码再回头记账"用（技能运行要落 tokens/cost，但记账点在
    LLM 咽喉里，跑完才知道总数）。没有收集器时返回全零，调用方不必判空。
    """
    return accumulate(_ACC.get() or [])


# ---- 台账写入 ----

def record_usage(model: str, *, prompt_tokens: int = 0, completion_tokens: int = 0,
                 cached_tokens: int = 0, scene: str = "", session_id: str | None = None,
                 turn: int = 0, trace_id: str = "", latency_ms: int | None = None,
                 status: str = "ok", error: str | None = None,
                 finish_reason: str | None = None, retried: bool = False) -> dict:
    """落一行用量账。写失败绝不影响调用方（记账是横切关注，不是业务）。

    失败调用也要走这里（status='error'，token 记 0）：**一次没成功的调用也是一次调用**。
    原来只在成功路径记账，实测量出来 194 行台账里没有任何失败痕迹，于是
    "429 重试 3 次才成功"和"一次就成"在数据上完全一样——稳定性问题无法归因。
    """
    row = {
        "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
        "model": model, "scene": scene or "unknown",
        "session_id": session_id, "turn": int(turn or 0), "trace_id": trace_id,
        "prompt_tokens": max(int(prompt_tokens or 0), 0),
        "completion_tokens": max(int(completion_tokens or 0), 0),
        "cached_tokens": min(max(int(cached_tokens or 0), 0),
                             max(int(prompt_tokens or 0), 0)),
        "latency_ms": latency_ms,
        "status": "ok" if status != "error" else "error",
        "error": (error or None) and str(error)[:300],
        "finish_reason": finish_reason,
        "retried": 1 if retried else 0,
    }
    p = price_for(model)
    row["cost_cny"] = cost_cny(model, row["prompt_tokens"], row["completion_tokens"],
                               row["cached_tokens"])
    row["price_estimated"] = 1 if p["estimated"] else 0
    # 收集器只收成功调用：失败调用没有 token 可归属，混进去会让"这段代码花了多少"
    # 出现一行 0 元记录，把 calls 数拉高。
    if row["status"] == "ok":
        _collect_into(row)
    # 同步进预算闸门的进程内累计：闸门只在启动/跨天时回库校准，
    # 光靠它自己查库的话，30 秒窗口内的连续调用会全部放行，5 元的额度能花到 6 元。
    budget.add_spend(row["cost_cny"])
    try:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO llm_usage (ts,model,scene,session_id,turn,trace_id,"
                "prompt_tokens,completion_tokens,cached_tokens,cost_cny,price_estimated,"
                "latency_ms,status,error,finish_reason,retried) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["model"], row["scene"], row["session_id"], row["turn"],
                 row["trace_id"], row["prompt_tokens"], row["completion_tokens"],
                 row["cached_tokens"], row["cost_cny"], row["price_estimated"],
                 row["latency_ms"], row["status"], row["error"], row["finish_reason"],
                 row["retried"]))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001  台账写不进去不能打断对话
        pass
    return row


# ---- 聚合查询 ----

def _rows(conn, where: str = "", args: tuple = ()):
    return conn.execute(
        "SELECT model, scene, session_id, turn, prompt_tokens pt, completion_tokens ct,"
        " cached_tokens ck, cost_cny c, price_estimated est, substr(ts,1,10) day,"
        " ts FROM llm_usage " + where, args).fetchall()


def summary(days: int = 14) -> dict:
    """总览 + 按天 / 按场景 / 按模型 / 按会话（成本 Top）。

    按天用 substr(ts,1,10) 而不是 date()：ts 是 ISO 字符串（本地时区），
    date() 反而要做一轮解析，ISO 前十位就是日期，索引也用得上。
    """
    conn = db.get_conn()
    try:
        since = (datetime.datetime.now() - datetime.timedelta(days=max(1, int(days)) - 1)
                 ).strftime("%Y-%m-%d")
        rows = _rows(conn, "WHERE substr(ts,1,10) >= ?", (since,))

        def agg(rs):
            out = {"calls": len(rs), "prompt_tokens": 0, "completion_tokens": 0,
                   "cached_tokens": 0, "cost_cny": 0.0, "estimated_calls": 0}
            for r in rs:
                out["prompt_tokens"] += r["pt"]
                out["completion_tokens"] += r["ct"]
                out["cached_tokens"] += r["ck"]
                out["cost_cny"] += r["c"] or 0.0
                out["estimated_calls"] += r["est"] or 0
            out["cost_cny"] = round(out["cost_cny"], 6)
            return out

        by_day: dict[str, dict] = {}
        by_scene: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        by_session: dict[str, dict] = {}
        for r in rows:
            for key, bucket in ((r["day"], by_day), (r["scene"], by_scene),
                                (r["model"], by_model),
                                (r["session_id"] or "无会话", by_session)):
                b = bucket.setdefault(key, {"calls": 0, "prompt_tokens": 0,
                                            "completion_tokens": 0, "cached_tokens": 0,
                                            "cost_cny": 0.0})
                b["calls"] += 1
                b["prompt_tokens"] += r["pt"]
                b["completion_tokens"] += r["ct"]
                b["cached_tokens"] += r["ck"]
                b["cost_cny"] += r["c"] or 0.0
        for bucket in (by_day, by_scene, by_model, by_session):
            for b in bucket.values():
                b["cost_cny"] = round(b["cost_cny"], 6)
                b["total_tokens"] = b["prompt_tokens"] + b["completion_tokens"]

        sessions = sorted(by_session.items(), key=lambda kv: -kv[1]["cost_cny"])
        return {
            "window_days": int(days),
            "summary": agg(rows),
            "by_day": [{"day": k, **v} for k, v in sorted(by_day.items())],
            "by_scene": sorted(({"scene": k, **v} for k, v in by_scene.items()),
                               key=lambda d: -d["cost_cny"]),
            "by_model": sorted(({"model": k, **v} for k, v in by_model.items()),
                               key=lambda d: -d["cost_cny"]),
            "by_session": [{"session_id": k, **v} for k, v in sessions[:10]],
        }
    finally:
        conn.close()


def session_turn_cost(session_id: str, turn: int | None = None) -> dict:
    """单会话（或单轮）的费用。对话页"这轮花了多少钱"直接查这里。"""
    conn = db.get_conn()
    try:
        where, args = "WHERE session_id=?", (session_id,)
        if turn is not None:
            where += " AND turn=?"
            args = (session_id, int(turn))
        rows = _rows(conn, where, args)
        return {
            "calls": len(rows),
            "prompt_tokens": sum(r["pt"] for r in rows),
            "completion_tokens": sum(r["ct"] for r in rows),
            "cached_tokens": sum(r["ck"] for r in rows),
            "cost_cny": round(sum(r["c"] or 0.0 for r in rows), 6),
        }
    finally:
        conn.close()
