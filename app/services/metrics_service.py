"""指标聚合：把三张台账拼成"哪里慢、哪里贵、哪里在失败"。

为什么不让面板直接查库：
1) 分位数（P50/P95）SQLite 算不了，必须在内存里排一次；
2) 三张表的窗口口径要一致（同一天、同一时区），散在三个接口里迟早对不上；
3) 面板要的是"一眼看结论"，不是原始行——聚合规则应该落在后端，能被测试钉住。

数据源是本地三张台账，不是 Phoenix/Langfuse：
- llm_usage  每次 LLM 调用一行（含失败行），有 model / scene / latency / cost
- tool_calls 每次工具执行一行，有 status / error_kind / latency
- agent_plans 每条编排一行，steps_json 里有每步的 role / status / model / cost

三个刻意的口径选择：
- **失败率的分母包含失败行**。llm_usage 从 2026-11-12 起失败也落账，
  只统计成功行会把"调 5 次挂 2 次"算成 100% 成功。
- **分位数用最近秩（nearest-rank）而不是插值**。样本少的时候插值会造出
  一个不存在的耗时（比如 3 个样本报 P95=第 3.1 个），看着精确其实误导。
- **编排成功率不把 capped/timeout 算成失败**，单独成栏。它们是"预算/时间不够"，
  和"跑挂了"是两种处置（前者调预算或拆 DAG，后者修 bug）。
- **策略拒绝不计入工具失败率**。assist 模式下模型试图写库被拦住，是闸门在工作；
  算成失败会让健康系统看起来在挂（实测 6 次 remember 全被拒 = 100% 失败）。
  但它仍然出现在失败分类里——"模型总想写库"本身是值得看的信息。
"""
import datetime
import json

from ..core.db import get_conn

# 编排终态里不算"失败"的两类：钱/时间不够，不是实现坏了
_SOFT_STOPS = ("capped", "timeout")

# 意图中文标签（单一来源是 app/agents/intent.INTENTS，这里只读）
from ..agents.intent import INTENTS as _INTENT_LABELS  # noqa: E402

# 子 agent 角色清单（与 orchestrator.ROLE_TOOLS 对齐；主控环节单独成栏，不混进角色）
_SUB_ROLES = ("researcher", "analyst", "writer", "critic")
_MAIN_SCENES = ("orchestrate:plan", "orchestrate:synthesize", "orchestrate:critique")


def _since(days: int) -> str:
    days = max(1, int(days))
    return (datetime.datetime.now() - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")


def _pct(values: list[int], q: float) -> int | None:
    """最近秩分位数。空样本返回 None——返回 0 会让"没有调用"看起来像"很快"。"""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, min(len(ordered), int(round(q * len(ordered) + 0.5))))
    return int(ordered[k - 1])


def _rate(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _bucket() -> dict:
    return {"calls": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_cny": 0.0, "latencies": [], "retried": 0}


def _finish(b: dict) -> dict:
    lat = b.pop("latencies")
    total = b["calls"]
    return {
        "calls": total,
        "errors": b["errors"],
        "error_rate": _rate(b["errors"], total),
        "prompt_tokens": b["prompt_tokens"],
        "completion_tokens": b["completion_tokens"],
        "total_tokens": b["prompt_tokens"] + b["completion_tokens"],
        "cost_cny": round(b["cost_cny"], 6),
        "retried": b["retried"],
        "p50_ms": _pct(lat, 0.50),
        "p95_ms": _pct(lat, 0.95),
        "max_ms": max(lat) if lat else None,
    }


def _llm(days: int) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT model, scene, prompt_tokens, completion_tokens, cost_cny, "
            "latency_ms, status, retried, substr(ts,1,10) AS day "
            "FROM llm_usage WHERE substr(ts,1,10) >= ? ORDER BY id", (_since(days),)).fetchall()
    finally:
        conn.close()

    total, by_scene, by_model, by_day = _bucket(), {}, {}, {}
    for r in rows:
        scene = r["scene"] or "unknown"
        model = r["model"] or "unknown"
        day = r["day"]
        failed = (r["status"] or "ok") != "ok"
        for b in (total,
                  by_scene.setdefault(scene, _bucket()),
                  by_model.setdefault(model, _bucket()),
                  by_day.setdefault(day, _bucket())):
            b["calls"] += 1
            b["errors"] += 1 if failed else 0
            b["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            b["completion_tokens"] += int(r["completion_tokens"] or 0)
            b["cost_cny"] += float(r["cost_cny"] or 0.0)
            b["retried"] += 1 if r["retried"] else 0
            if r["latency_ms"] is not None and not failed:
                # 失败行的 latency 是"等了多久才报错"，混进成功分位数会把
                # "模型变慢"和"上游挂了"画成同一根线
                b["latencies"].append(int(r["latency_ms"]))

    return {
        "summary": _finish(total),
        "by_scene": sorted(({"scene": k, **_finish(v)} for k, v in by_scene.items()),
                           key=lambda d: -d["cost_cny"]),
        "by_model": sorted(({"model": k, **_finish(v)} for k, v in by_model.items()),
                           key=lambda d: -d["cost_cny"]),
        "by_day": [{"day": k, **_finish(v)} for k, v in sorted(by_day.items())],
    }


def _tools(days: int) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT tool, status, error_kind, latency_ms, retried, cached "
            "FROM tool_calls WHERE ts >= ?", (_since(days),)).fetchall()
    finally:
        conn.close()

    total = {"calls": 0, "errors": 0, "denied": 0, "latencies": [],
             "retried": 0, "cached": 0}
    by_tool: dict[str, dict] = {}
    errs: dict[str, int] = {}
    for r in rows:
        failed = (r["status"] or "ok") != "ok"
        # 策略拒绝不是失败：assist 模式下模型试图写库被拦住，说明闸门在工作。
        # 把它算进失败率，会让"工具很健康"的系统看起来 6% 在挂（实测数字）。
        denied = (r["error_kind"] or "") == "policy_denied"
        for b in (total, by_tool.setdefault(r["tool"] or "unknown",
                                           {"calls": 0, "errors": 0, "denied": 0,
                                            "latencies": [], "retried": 0, "cached": 0})):
            b["calls"] += 1
            b["errors"] += 1 if (failed and not denied) else 0
            b["denied"] += 1 if denied else 0
            b["retried"] += 1 if r["retried"] else 0
            b["cached"] += 1 if r["cached"] else 0
            if r["latency_ms"] is not None:
                b["latencies"].append(int(r["latency_ms"]))
        if failed:
            kind = r["error_kind"] or "failed"
            errs[kind] = errs.get(kind, 0) + 1

    def _one(b: dict) -> dict:
        lat = b.pop("latencies")
        # 分母扣掉被拒绝的那部分：拒绝根本没执行，不该影响"执行成功率"
        executed = max(0, b["calls"] - b["denied"])
        return {**b, "executed": executed,
                "error_rate": _rate(b["errors"], executed),
                "p50_ms": _pct(lat, 0.50), "p95_ms": _pct(lat, 0.95),
                "max_ms": max(lat) if lat else None}

    return {
        "summary": _one(total),
        "by_tool": sorted(({"tool": k, **_one(v)} for k, v in by_tool.items()),
                          key=lambda d: -d["calls"]),
        # 参数错 vs 执行错要分开看：前者改 schema/描述，后者修实现
        "error_kinds": sorted(({"kind": k, "n": v} for k, v in errs.items()),
                              key=lambda d: -d["n"]),
    }


def _orchestration(days: int) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, status, cost_cny, duration_ms, tokens_used, steps_json "
            "FROM agent_plans WHERE substr(created_at,1,10) >= ?", (_since(days),)).fetchall()
    finally:
        conn.close()

    by_status: dict[str, int] = {}
    step_status: dict[tuple[str, str], int] = {}
    step_models: dict[str, int] = {}
    costs, durations, n_steps, retried_steps = [], [], [], 0
    for r in rows:
        st = r["status"] or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        costs.append(float(r["cost_cny"] or 0.0))
        durations.append(int(r["duration_ms"] or 0))
        try:
            steps = json.loads(r["steps_json"] or "[]")
        except (ValueError, TypeError):
            steps = []
        n_steps.append(len(steps))
        for s in steps if isinstance(steps, list) else []:
            if not isinstance(s, dict):
                continue
            role, stt = str(s.get("role") or "?"), str(s.get("status") or "?")
            step_status[(role, stt)] = step_status.get((role, stt), 0) + 1
            model = str(s.get("model") or "")
            if model:
                step_models[model] = step_models.get(model, 0) + 1
            retried_steps += 1 if s.get("retries") else 0

    plans = len(rows)
    done = by_status.get("done", 0)
    soft = sum(by_status.get(s, 0) for s in _SOFT_STOPS)
    hard_failed = sum(v for k, v in by_status.items()
                      if k not in ("done",) + _SOFT_STOPS)
    return {
        "plans": plans,
        "by_status": by_status,
        "success_rate": _rate(done, plans),
        # 两个分母都给：success_rate 是"一次跑成的比例"，hard_failure_rate 才是
        # "实现有问题"的比例。只报前者会把预算不足也算成 bug。
        "hard_failure_rate": _rate(hard_failed, plans),
        "soft_stop_rate": _rate(soft, plans),
        "avg_cost_cny": round(sum(costs) / plans, 6) if plans else 0.0,
        "avg_duration_ms": int(sum(durations) / plans) if plans else 0,
        "avg_steps": round(sum(n_steps) / plans, 1) if plans else 0.0,
        "retried_steps": retried_steps,
        "step_status": sorted(({"role": k[0], "status": k[1], "n": v}
                               for k, v in step_status.items()),
                              key=lambda d: (d["role"], d["status"])),
        "step_models": sorted(({"model": k, "n": v} for k, v in step_models.items()),
                              key=lambda d: -d["n"]),
    }


def _agents(days: int) -> dict:
    """子 agent 角色级运行视图：每个角色单独一笔账。

    原始数据在落库时已各自打标，这里只按同一把钥匙拼：
    - llm_usage.scene  = "orchestrate:{role}"  → LLM 调用/费用/延迟
    - tool_calls.source = "agent:{role}"       → 工具调用/失败/拒绝
    - agent_plans.steps_json                   → 步骤终态/重试/实际模型

    主控环节（plan/synthesize/critique）不算子 agent，单独归 "main" 栏：
    它们没有工具、没有角色权限，混进角色表会让"哪个角色贵"这个问题答不准——
    synthesize 一次就比 researcher 一个下午查证的钱还多。
    """
    def _bucket() -> dict:
        return {"llm_calls": 0, "llm_errors": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "cost_cny": 0.0, "llm_lat": [],
                "tool_calls": 0, "tool_errors": 0, "tool_denied": 0, "tool_lat": [],
                "tools": {}, "steps": 0, "steps_done": 0, "steps_failed": 0,
                "steps_skipped": 0, "retries": 0, "step_cost": 0.0,
                "step_durations": [], "models": {}}

    buckets: dict[str, dict] = {}

    def _b(key: str) -> dict:
        return buckets.setdefault(key, _bucket())

    conn = get_conn()
    try:
        llm_rows = conn.execute(
            "SELECT model, scene, prompt_tokens, completion_tokens, cost_cny, latency_ms, status "
            "FROM llm_usage WHERE scene LIKE 'orchestrate:%' AND substr(ts,1,10) >= ?",
            (_since(days),)).fetchall()
        tool_rows = conn.execute(
            "SELECT tool, source, status, error_kind, latency_ms "
            "FROM tool_calls WHERE source LIKE 'agent:%' AND ts >= ?",
            (_since(days),)).fetchall()
        plan_rows = conn.execute(
            "SELECT steps_json FROM agent_plans WHERE substr(created_at,1,10) >= ?",
            (_since(days),)).fetchall()
    finally:
        conn.close()

    for r in llm_rows:
        suffix = (r["scene"] or "").split(":", 1)[-1]
        key = suffix if suffix in _SUB_ROLES else (
            "main" if suffix in ("plan", "synthesize", "critique") else None)
        if key is None:
            continue  # 未知场景后缀不猜——指标宁可少一行也不能串账
        b = _b(key)
        failed = (r["status"] or "ok") != "ok"
        b["llm_calls"] += 1
        b["llm_errors"] += 1 if failed else 0
        b["prompt_tokens"] += int(r["prompt_tokens"] or 0)
        b["completion_tokens"] += int(r["completion_tokens"] or 0)
        b["cost_cny"] += float(r["cost_cny"] or 0.0)
        if r["latency_ms"] is not None and not failed:
            b["llm_lat"].append(int(r["latency_ms"]))

    for r in tool_rows:
        role = (r["source"] or "").split(":", 1)[-1]
        if role not in _SUB_ROLES:
            continue
        b = _b(role)
        denied = (r["error_kind"] or "") == "policy_denied"
        failed = (r["status"] or "ok") != "ok"
        b["tool_calls"] += 1
        b["tool_errors"] += 1 if (failed and not denied) else 0
        b["tool_denied"] += 1 if denied else 0
        if r["latency_ms"] is not None:
            b["tool_lat"].append(int(r["latency_ms"]))
        tool = r["tool"] or "unknown"
        b["tools"][tool] = b["tools"].get(tool, 0) + 1

    for r in plan_rows:
        try:
            steps = json.loads(r["steps_json"] or "[]")
        except (ValueError, TypeError):
            continue
        for s in steps if isinstance(steps, list) else []:
            if not isinstance(s, dict):
                continue
            role = str(s.get("role") or "")
            if role not in _SUB_ROLES:
                continue
            b = _b(role)
            stt = str(s.get("status") or "?")
            b["steps"] += 1
            b["steps_done"] += 1 if stt == "done" else 0
            b["steps_failed"] += 1 if stt == "failed" else 0
            b["steps_skipped"] += 1 if stt == "skipped" else 0
            b["retries"] += 1 if s.get("retries") else 0
            b["step_cost"] += float(s.get("cost_cny") or 0.0)
            if s.get("duration_ms"):
                b["step_durations"].append(int(s["duration_ms"]))
            model = str(s.get("model") or "")
            if model:
                b["models"][model] = b["models"].get(model, 0) + 1

    def _main_model(b: dict) -> str:
        return max(b["models"].items(), key=lambda kv: kv[1])[0] if b["models"] else ""

    out = []
    # 角色按固定顺序展示，多一个空角色说明枚举没对齐，别隐藏它
    for key in _SUB_ROLES + ("main",):
        if key not in buckets:
            continue
        b = buckets[key]
        executed_tools = max(0, b["tool_calls"] - b["tool_denied"])
        top_tools = sorted(b["tools"].items(), key=lambda kv: -kv[1])[:3]
        out.append({
            "role": key,
            "llm_calls": b["llm_calls"], "llm_errors": b["llm_errors"],
            "tokens": b["prompt_tokens"] + b["completion_tokens"],
            "cost_cny": round(b["cost_cny"], 6),
            "llm_p95_ms": _pct(b["llm_lat"], 0.95),
            "tool_calls": b["tool_calls"], "tool_errors": b["tool_errors"],
            "tool_denied": b["tool_denied"],
            "tool_p95_ms": _pct(b["tool_lat"], 0.95),
            "top_tools": [{"tool": t, "n": n} for t, n in top_tools],
            "steps": b["steps"], "steps_done": b["steps_done"],
            "steps_failed": b["steps_failed"], "steps_skipped": b["steps_skipped"],
            "retries": b["retries"],
            "step_cost_cny": round(b["step_cost"], 6),
            "avg_step_ms": int(sum(b["step_durations"]) / len(b["step_durations"]))
                           if b["step_durations"] else None,
            "main_model": _main_model(b),
        })
    return {"roles": out}


def _intents(days: int) -> dict:
    """按意图聚合编排：用户在拿 agent 干什么、哪类意图贵/容易失败。

    早期计划没有 intent 列（NULL），归 unknown 单列而不是回填 other——
    "没分过类"和"分出来是其他"是两回事；混在一起会把兜底类的真实占比抬高。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT intent, status, cost_cny, duration_ms, steps_json "
            "FROM agent_plans WHERE substr(created_at,1,10) >= ?", (_since(days),)).fetchall()
    finally:
        conn.close()

    def _bucket() -> dict:
        return {"plans": 0, "done": 0, "soft": 0, "hard": 0, "other_status": 0,
                "costs": [], "durations": [], "steps": 0}

    by: dict[str, dict] = {}
    for r in rows:
        intent = r["intent"] or "unknown"
        b = by.setdefault(intent, _bucket())
        b["plans"] += 1
        st = r["status"] or "unknown"
        if st == "done":
            b["done"] += 1
        elif st in _SOFT_STOPS:
            b["soft"] += 1
        elif st in ("failed", "partial"):
            b["hard"] += 1
        else:
            b["other_status"] += 1
        b["costs"].append(float(r["cost_cny"] or 0.0))
        b["durations"].append(int(r["duration_ms"] or 0))
        try:
            steps = json.loads(r["steps_json"] or "[]")
        except (ValueError, TypeError):
            steps = []
        b["steps"] += len(steps) if isinstance(steps, list) else 0

    items = []
    for intent, b in by.items():
        plans = b["plans"] or 1
        items.append({
            "intent": intent,
            "label": _INTENT_LABELS.get(intent, intent),
            "plans": b["plans"], "done": b["done"],
            "success_rate": _rate(b["done"], b["plans"]),
            "hard_failure_rate": _rate(b["hard"], b["plans"]),
            "soft_stop_rate": _rate(b["soft"], b["plans"]),
            "avg_cost_cny": round(sum(b["costs"]) / plans, 6),
            "avg_duration_ms": int(sum(b["durations"]) / plans),
            "avg_steps": round(b["steps"] / plans, 1),
        })
    items.sort(key=lambda d: -d["plans"])
    return {"items": items,
            "unclassified": by.get("unknown", {}).get("plans", 0)}


def overview(days: int = 7) -> dict:
    """一次拿全部：LLM / 工具 / 编排 + 当日成本水位。

    `today` 单独给，是因为"今天花了多少、离日预算还有多远"是每天都要看的问题，
    而 7 天窗口里的均值会把今天的问题平均掉。
    """
    from ..core import budget

    llm = _llm(days)
    today = next((d for d in llm["by_day"]
                  if d["day"] == datetime.date.today().isoformat()), None)
    return {
        "window_days": max(1, int(days)),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "llm": llm,
        "tools": _tools(days),
        "orchestration": _orchestration(days),
        "subagents": _agents(days),
        "intents": _intents(days),
        "budget": {**budget.status(),
                   "today_cost_cny": (today or {}).get("cost_cny", 0.0),
                   "today_calls": (today or {}).get("calls", 0)},
    }
