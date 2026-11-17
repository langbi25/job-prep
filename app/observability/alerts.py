"""可观测告警：错误率 / 慢调用 / 成本 / 工具 / 编排 / 容量 超阈值 → webhook 推送。

数据源是本地 spans 表（事实源），不是 Langfuse/Phoenix——观测后端挂了告警也必须
照常工作，这正是"本地主存、旁路导出"原则在告警上的延续。

判定口径刻意选**顶层调用**（parent_id IS NULL 的 span），而不是所有 span：
一次失败会让从根到叶的每层 span 都标 error（异常沿上下文管理器向上传播），
拿全部 span 算错误率，一次失败会被算成 4~6 次，阈值形同虚设。顶层调用正好
是"一次请求/一轮对话"的单位，与人对"错误率"的直觉一致。

三条设计纪律：
1. **没送达也要落库**（delivered=0 + error）——webhook 断了之后告警静默失败
   等于没有告警，"告警系统本身失联"必须可被发现。
2. **冷却查表不查内存**——重启不丢冷却状态；同一条告警 30 分钟内不重复轰炸。
3. **判定失败绝不影响业务**——告警循环在自己的线程里，任何异常只打日志。
"""
import datetime
import json
import os
import threading
import time

import requests

RULE_ERROR_RATE = "error_rate"
RULE_SLOW = "slow_calls"
# 后三条是 2026-11-15 补的。前两条只看"请求有没有失败/慢不慢"，而实际出事
# 最常见的三种形态一个都覆盖不到：钱悄悄花超了、某个工具一直失败但整体错误率
# 没到阈值（工具调用只占少数）、编排在反复挂。三条都带样本保护——
# 样本太少时任何比率都不可信，宁可不报。

_loop_stop = threading.Event()
_loop_thread: threading.Thread | None = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


RULE_COST = "cost_spike"
RULE_TOOL_FAILURE = "tool_failure"
RULE_PLAN_FAILURE = "plan_failure"
# 容量三条（2026-11-17，阶段 C）：前五条都在看"业务跑得怎么样"，没有一条会在
# **库悄悄长满 / 写锁开始排队**时提醒。这类问题不会让请求立刻失败，却会让备份
# 越来越慢、写事务越来越挤——正是需要一个不依赖人工看面板的信号。
RULE_CAPACITY_DB = "capacity_db_size"
RULE_CAPACITY_LOCK = "capacity_write_lock"
RULE_CAPACITY_GROWTH = "capacity_db_growth"

# 分级路由（2026-11-18）：每条规则默认 severity。critical 走
# ALERT_WEBHOOK_URL_CRITICAL，warning 走 ALERT_WEBHOOK_URL_WARNING，
# 独立 URL 未配时都兜底到 ALERT_WEBHOOK_URL。
# 默认只有"错误率高"和"库满"算 critical——值得半夜被叫醒的就这两类；
# 其余是"天亮处理"级别，为它们配值班电话是告警疲劳的开端。
RULE_SEVERITY: dict[str, str] = {
    RULE_ERROR_RATE: "critical",
    RULE_SLOW: "warning",
    RULE_COST: "warning",
    RULE_TOOL_FAILURE: "warning",
    RULE_PLAN_FAILURE: "warning",
    RULE_CAPACITY_DB: "warning",      # 触发时按"是否已满"升 critical
    RULE_CAPACITY_LOCK: "warning",
    RULE_CAPACITY_GROWTH: "warning",
}


def config() -> dict:
    """当前阈值摘要。webhook 只回是否配置 + 域名打码——完整 URL 是凭据的一部分，
    不能从面板接口原样吐出去。"""
    url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    masked = ""
    if url:
        rest = url.split("://", 1)[-1]
        host = rest.split("/", 1)[0].split("?")[0]
        masked = f"...{host}（已配置）"
    crit = (os.getenv("ALERT_WEBHOOK_URL_CRITICAL") or "").strip()
    warn = (os.getenv("ALERT_WEBHOOK_URL_WARNING") or "").strip()
    return {
        "enabled": bool(url or crit or warn),
        "webhook": masked,
        "routes": {
            "critical": "独立" if crit else ("兜底" if url else "未配置"),
            "warning": "独立" if warn else ("兜底" if url else "未配置"),
        },
        "format": (os.getenv("ALERT_FORMAT") or "generic").strip().lower(),
        "window_min": _env_int("ALERT_WINDOW_MIN", 5),
        "min_calls": _env_int("ALERT_MIN_CALLS", 10),
        "error_rate": _env_float("ALERT_ERROR_RATE", 0.5),
        "slow_ms": _env_int("ALERT_SLOW_MS", 10000),
        "slow_count": _env_int("ALERT_SLOW_COUNT", 3),
        "cooldown_min": _env_int("ALERT_COOLDOWN_MIN", 30),
        "check_sec": _env_int("ALERT_CHECK_SEC", 60),
        # 成本：既看"离日预算还有多远"，也看"比平时贵几倍"。
        # 只看预算会漏掉"平时 5 分钱、今天 2 块钱"这种异常（还没触顶但很不正常）；
        # 只看倍数会在总花费很小时乱报，所以两者都加绝对下限。
        "budget_ratio": _env_float("ALERT_BUDGET_RATIO", 0.8),
        "cost_spike_ratio": _env_float("ALERT_COST_SPIKE_RATIO", 3.0),
        "cost_min_cny": _env_float("ALERT_COST_MIN_CNY", 0.5),
        "tool_min_calls": _env_int("ALERT_TOOL_MIN_CALLS", 5),
        "tool_error_rate": _env_float("ALERT_TOOL_ERROR_RATE", 0.3),
        "plan_min_plans": _env_int("ALERT_PLAN_MIN_PLANS", 3),
        "plan_failure_rate": _env_float("ALERT_PLAN_FAILURE_RATE", 0.5),
        # 编排很稀疏（一天几条），用 5 分钟窗口永远凑不满样本。
        # 所以它单独用小时级窗口，其余规则用 window_min。
        "plan_window_min": _env_int("ALERT_PLAN_WINDOW_MIN", 180),
        # 容量：体积用"占上限多少"，写锁用"最近半小时挡住几次"。
        # 挡住率下限 3 个样本（15 分钟）：1 条样本的比率非 0 即 100%，报警等于扔硬币。
        "db_size_ratio": _env_float("ALERT_DB_SIZE_RATIO", 0.8),
        "lock_window_min": _env_int("ALERT_LOCK_WINDOW_MIN", 30),
        "lock_min_samples": _env_int("ALERT_LOCK_MIN_SAMPLES", 3),
        "lock_blocked_rate": _env_float("ALERT_LOCK_BLOCKED_RATE", 0.05),
        "db_eta_days": _env_float("ALERT_DB_ETA_DAYS", 30),
        "growth_min_samples": _env_int("ALERT_GROWTH_MIN_SAMPLES", 20),
        # 没配 webhook 时判定仍然跑（落库能看到"本该报什么"），但冷却拉长，
        # 否则每 30 分钟一条同样的记录，历史页会被自己的噪声淹掉。
        "local_cooldown_min": _env_int("ALERT_LOCAL_COOLDOWN_MIN", 360),
        # 静默时段：窗口内的告警只落库不推送（deep night 不轰炸）。
        # 支持跨午夜（23:00-07:00），两端相等视为未配置。
        "quiet_start": (os.getenv("ALERT_QUIET_START") or "").strip(),
        "quiet_end": (os.getenv("ALERT_QUIET_END") or "").strip(),
        "severity_map": dict(RULE_SEVERITY),
    }


def in_quiet_window(now: datetime.datetime, start: str, end: str) -> bool:
    """现在是否在静默时段内。纯函数（可测）：解析失败按"未配置"处理，
    支持跨午夜（23:00→07:00 静默段跨过 0 点）。两端相等视为未配置。"""
    def _parse(s: str):
        parts = s.strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() and len(p) <= 2 for p in parts):
            return None
        h, m = int(parts[0]), int(parts[1])
        if h > 23 or m > 59:
            return None
        return datetime.time(h, m)

    t0, t1 = _parse(start or ""), _parse(end or "")
    if t0 is None or t1 is None or t0 == t1:
        return False
    cur = now.time()
    if t0 < t1:
        return t0 <= cur <= t1
    return cur >= t0 or cur <= t1   # 跨午夜：晚于起点或早于终点都算静默


def window_metrics(window_min: int) -> dict:
    """滚动窗口内的顶层调用指标。spans 表是唯一事实源。"""
    from ..core.db import get_conn  # noqa: PLC0415  惰性导入避免循环依赖

    since = (datetime.datetime.now()
             - datetime.timedelta(minutes=max(int(window_min), 1))).isoformat(
                 timespec="milliseconds")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
            "SUM(CASE WHEN duration_ms >= ? THEN 1 ELSE 0 END) AS slow "
            "FROM spans WHERE parent_id IS NULL AND start_ts >= ?",
            (config()["slow_ms"], since)).fetchone()
        tops = conn.execute(
            "SELECT name, COUNT(*) AS n FROM spans "
            "WHERE parent_id IS NULL AND start_ts >= ? AND status='error' "
            "GROUP BY name ORDER BY n DESC LIMIT 3", (since,)).fetchall()
    finally:
        conn.close()
    total = int(row["total"] or 0)
    errors = int(row["errors"] or 0)
    return {"window_min": int(window_min), "total": total, "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "slow": int(row["slow"] or 0),
            "top_errors": [{"name": r["name"], "n": r["n"]} for r in tops]}


def _since_iso(minutes: int) -> str:
    return (datetime.datetime.now()
            - datetime.timedelta(minutes=max(int(minutes), 1))).isoformat(
                timespec="milliseconds")


def cost_metrics(days: int = 7) -> dict:
    """今日花费 vs 日均 vs 日预算。

    口径是 llm_usage（业务视角的钱），不是 span 数——告警要说清"花了多少"，
    而不是"调了几次"。日均**不含今天**：把今天算进去会把突增自己摊平，
    花了 3 倍的钱只显示 1.5 倍。
    """
    from ..core import config as app_config  # noqa: PLC0415
    from ..core.db import get_conn  # noqa: PLC0415

    today = datetime.date.today()
    since = (today - datetime.timedelta(days=max(2, int(days)) - 1)).isoformat()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT substr(ts,1,10) AS day, ROUND(SUM(cost_cny), 6) AS c "
            "FROM llm_usage WHERE substr(ts,1,10) >= ? GROUP BY day", (since,)).fetchall()
    finally:
        conn.close()
    by_day = {r["day"]: float(r["c"] or 0.0) for r in rows}
    today_cost = by_day.get(today.isoformat(), 0.0)
    prev = [v for k, v in by_day.items() if k != today.isoformat()]
    avg = round(sum(prev) / len(prev), 6) if prev else 0.0
    limit = float(getattr(app_config, "LLM_DAILY_BUDGET_CNY", 5.0) or 5.0)
    return {"today_cost_cny": round(today_cost, 6), "avg_day_cny": avg,
            "prev_days": len(prev), "budget_cny": limit,
            "budget_ratio": round(today_cost / limit, 4) if limit else 0.0,
            "spike_ratio": round(today_cost / avg, 2) if avg > 0 else 0.0}


def tool_window_metrics(window_min: int) -> dict:
    """窗口内工具调用指标。工具失败率和请求错误率是两件事：
    一个工具坏了，整体错误率可能只有 5%（工具调用只占少数请求），
    但那个工具的成功率是 0——这条规则就是为这种情况存在的。"""
    from ..core.db import get_conn  # noqa: PLC0415

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status != 'ok' AND COALESCE(error_kind,'') != 'policy_denied' "
            "THEN 1 ELSE 0 END) AS errors, "
            "SUM(CASE WHEN error_kind = 'policy_denied' THEN 1 ELSE 0 END) AS denied "
            "FROM tool_calls WHERE ts >= ?", (_since_iso(window_min),)).fetchone()
        tops = conn.execute(
            "SELECT tool, COUNT(*) AS n FROM tool_calls "
            "WHERE ts >= ? AND status != 'ok' AND COALESCE(error_kind,'') != 'policy_denied' "
            "GROUP BY tool ORDER BY n DESC LIMIT 3",
            (_since_iso(window_min),)).fetchall()
    finally:
        conn.close()
    total, errors = int(row["total"] or 0), int(row["errors"] or 0)
    denied = int(row["denied"] or 0)
    executed = max(0, total - denied)
    return {"total": total, "errors": errors, "denied": denied,
            "error_rate": round(errors / executed, 4) if executed else 0.0,
            "top_tools": [{"tool": r["tool"], "n": r["n"]} for r in tops]}


def plan_window_metrics(window_min: int) -> dict:
    """窗口内编排终态分布。capped/timeout 单列：它们是"钱/时间不够"，
    不是"跑挂了"，混进失败率会让告警指向错误的修法。"""
    from ..core.db import get_conn  # noqa: PLC0415

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM agent_plans "
            "WHERE created_at >= ? GROUP BY status", (_since_iso(window_min),)).fetchall()
    finally:
        conn.close()
    by_status = {r["status"]: int(r["n"]) for r in rows}
    total = sum(by_status.values())
    hard = sum(n for k, n in by_status.items() if k in ("failed", "partial"))
    soft = sum(n for k, n in by_status.items() if k in ("capped", "timeout"))
    return {"total": total, "failed": hard, "soft_stopped": soft,
            "by_status": by_status,
            "failure_rate": round(hard / total, 4) if total else 0.0}


def capacity_metrics(lock_window_min: int = 30) -> dict:
    """容量现状：最近窗口的写锁分布 + 当前体积 + 7 天增速外推。

    数据源是 capacity_samples（worker 每 5 分钟采一次），不是每次现探——
    告警循环不该自己去抢写锁，那会让"观测"变成被测对象的一部分。
    """
    from ..services import capacity_service as cap  # noqa: PLC0415

    rec = cap.recent_stats(lock_window_min)
    hist = cap.history(7)
    return {**rec, "growth_bytes_per_day": hist["growth_bytes_per_day"],
            "eta_days": hist["eta_days"], "samples_7d": hist["n"]}


def _recently_alerted(rule: str, cooldown_min: int) -> bool:
    """冷却判定查 alerts 表：重启不丢状态，也不需要第二份内存状态。"""
    from ..core.db import get_conn  # noqa: PLC0415

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT ts FROM alerts WHERE rule=? ORDER BY ts DESC LIMIT 1",
            (rule,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    last = datetime.datetime.fromisoformat(row["ts"])
    return last >= datetime.datetime.now() - datetime.timedelta(minutes=cooldown_min)


def _record(rule: str, severity: str, message: str, metrics: dict,
            delivered: bool, error: str | None) -> None:
    from ..core.db import get_conn  # noqa: PLC0415

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO alerts (ts, rule, severity, message, metrics, delivered, error) "
            "VALUES (?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="milliseconds"),
             rule, severity, message,
             json.dumps(metrics, ensure_ascii=False), 1 if delivered else 0, error))
        conn.commit()
    finally:
        conn.close()


def _payload(message: str, fmt: str, severity: str = "warning") -> dict:
    """三种接收端的报文格式。generic 给自写接收器；wecom/dingtalk 是机器人格式。
    严重级放 payload 字段 + 消息前缀双保险：接收端不认识 level 字段时，
    前缀至少让值班的人肉眼看出来。"""
    tagged = message.replace("【job-prep 告警】", f"【job-prep 告警·{severity.upper()}】", 1)
    if fmt == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": tagged}}
    if fmt == "dingtalk":
        return {"msgtype": "markdown",
                "markdown": {"title": f"job-prep {severity}告警", "text": tagged}}
    return {"text": tagged, "level": severity}


def _post(url: str, payload: dict, timeout: int = 5):
    """发 HTTP 请求的唯一出口（测试从这里替换，不 mock requests 全家）。"""
    # trust_env=False：告警链路自己必须是最稳的一段，不走系统代理（与 OTLP 同款坑）
    session = requests.Session()
    session.trust_env = False
    return session.post(url, json=payload, timeout=timeout)


def _route_url(severity: str) -> tuple[str | None, str]:
    """按级别选通道：独立 URL 优先，未配时兜底到通用 URL。返回 (url, 通道说明)。"""
    generic = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if severity == "critical":
        url = (os.getenv("ALERT_WEBHOOK_URL_CRITICAL") or "").strip() or generic
        return url, ("critical 独立通道" if os.getenv("ALERT_WEBHOOK_URL_CRITICAL", "").strip()
                     else "critical 兜底通用通道")
    url = (os.getenv("ALERT_WEBHOOK_URL_WARNING") or "").strip() or generic
    return url, ("warning 独立通道" if os.getenv("ALERT_WEBHOOK_URL_WARNING", "").strip()
                 else "warning 兜底通用通道")


def _deliver(message: str, severity: str = "warning") -> tuple[bool, str | None]:
    url, channel = _route_url(severity)
    if not url:
        return False, f"{channel} 未配置，仅落库不推送"
    fmt = (os.getenv("ALERT_FORMAT") or "generic").strip().lower()
    try:
        resp = _post(url, _payload(message, fmt, severity))
        if 200 <= resp.status_code < 300:
            return True, None
        return False, f"webhook 返回 {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001  推送失败不能打断判定循环
        return False, f"{type(e).__name__}: {e}"


def send_test() -> dict:
    """手动发一条测试告警：配置完 webhook 后用它验收链路，不必等真故障。"""
    metrics = window_metrics(config()["window_min"])
    msg = (f"【job-prep 测试告警】链路连通性检查。当前窗口指标："
           f"顶层调用 {metrics['total']}，错误 {metrics['errors']}，"
           f"慢调用 {metrics['slow']}。收到这条说明 webhook 通了。")
    ok, err = _deliver(msg, "info")
    _record("test", "info", msg, metrics, ok, err)
    return {"delivered": ok, "error": err, "metrics": metrics}


def check_once() -> dict:
    """判定一次。返回触发与跳过明细——后台循环和测试都从这里走，单一入口。"""
    cfg = config()
    metrics = window_metrics(cfg["window_min"])
    triggered, skipped = [], []
    # 没配 webhook 时判定照跑（历史里能看到"本该报什么"），但冷却拉长到
    # local_cooldown_min——否则每 30 分钟一条一模一样的记录，告警页被自己淹掉。
    cool = cfg["cooldown_min"] if cfg["enabled"] else cfg["local_cooldown_min"]

    def _fire(rule: str, severity: str, msg: str, m: dict) -> None:
        """统一的触发出口：冷却 → 静默 → 分级推送 → 落库。

        顺序有讲究：冷却在前——静默时段的告警也占冷却，否则静默一过、
        同一条规则会立刻把积压的"每轮一条"记录全补出来。
        静默时段只落库不推送（delivered=0 + 原因）：**"晚上没推"必须可见**，
        否则静默窗口会变成无声的黑洞——历史里有一条静默记录，早上能补看。
        """
        if _recently_alerted(rule, cool):
            skipped.append({"rule": rule, "reason": "冷却期内"})
            return
        if in_quiet_window(datetime.datetime.now(), cfg["quiet_start"], cfg["quiet_end"]):
            reason = (f"静默时段 {cfg['quiet_start']}-{cfg['quiet_end']}，仅落库不推送")
            _record(rule, severity, msg, m, False, reason)
            triggered.append({"rule": rule, "delivered": False, "error": reason})
            return
        ok, err = _deliver(msg, severity)
        _record(rule, severity, msg, m, ok, err)
        triggered.append({"rule": rule, "delivered": ok, "error": err})

    rate = metrics["error_rate"]
    if metrics["total"] >= cfg["min_calls"] and rate >= cfg["error_rate"]:
        tops = "、".join(f"{t['name']}×{t['n']}" for t in metrics["top_errors"]) or "（无明细）"
        _fire(RULE_ERROR_RATE, RULE_SEVERITY[RULE_ERROR_RATE],
              f"【job-prep 告警】错误率过高：近 {cfg['window_min']} 分钟顶层调用 "
              f"{metrics['total']} 次，错误 {metrics['errors']} 次"
              f"（{rate:.0%} ≥ 阈值 {cfg['error_rate']:.0%}）。最常报错：{tops}", metrics)

    if metrics["slow"] >= cfg["slow_count"]:
        _fire(RULE_SLOW, RULE_SEVERITY[RULE_SLOW],
              f"【job-prep 告警】慢调用集中：近 {cfg['window_min']} 分钟有 "
              f"{metrics['slow']} 次顶层调用超过 {cfg['slow_ms']}ms"
              f"（阈值 {cfg['slow_count']} 次）。去 Traces 面板看瀑布图定位慢在哪一层。", metrics)

    # ---- 成本：两个触发面，命中任一即报 ----
    cm = cost_metrics(7)
    reasons = []
    if cm["budget_ratio"] >= cfg["budget_ratio"]:
        reasons.append(f"已达日预算 {cm['budget_ratio']:.0%}")
    if cm["spike_ratio"] >= cfg["cost_spike_ratio"]:
        reasons.append(f"是近 {cm['prev_days']} 天日均的 {cm['spike_ratio']} 倍")
    if reasons and cm["today_cost_cny"] >= cfg["cost_min_cny"]:
        _fire(RULE_COST, RULE_SEVERITY[RULE_COST],
              f"【job-prep 告警】成本异常：今日已花 ¥{cm['today_cost_cny']:.2f}"
              f"（日预算 ¥{cm['budget_cny']:.2f}，日均 ¥{cm['avg_day_cny']:.2f}）。"
              f"原因：{'；'.join(reasons)}。去指标页看是哪个场景在涨。", cm)

    # ---- 工具失败率 ----
    tm = tool_window_metrics(cfg["window_min"])
    if tm["total"] >= cfg["tool_min_calls"] and tm["error_rate"] >= cfg["tool_error_rate"]:
        tops = "、".join(f"{t['tool']}×{t['n']}" for t in tm["top_tools"]) or "（无明细）"
        _fire(RULE_TOOL_FAILURE, RULE_SEVERITY[RULE_TOOL_FAILURE],
              f"【job-prep 告警】工具失败集中：近 {cfg['window_min']} 分钟工具调用 "
              f"{tm['total']} 次、失败 {tm['errors']} 次"
              f"（{tm['error_rate']:.0%} ≥ 阈值 {cfg['tool_error_rate']:.0%}）。"
              f"最常失败：{tops}。参数错改 schema，执行错修实现。", tm)

    # ---- 编排失败率 ----
    pm = plan_window_metrics(cfg["plan_window_min"])
    if pm["total"] >= cfg["plan_min_plans"] and pm["failure_rate"] >= cfg["plan_failure_rate"]:
        dist = "、".join(f"{k}×{v}" for k, v in sorted(pm["by_status"].items()))
        _fire(RULE_PLAN_FAILURE, RULE_SEVERITY[RULE_PLAN_FAILURE],
              f"【job-prep 告警】编排失败率高：近 {cfg['plan_window_min']} 分钟 "
              f"{pm['total']} 条计划里失败/部分失败 {pm['failed']} 条"
              f"（{pm['failure_rate']:.0%} ≥ 阈值 {cfg['plan_failure_rate']:.0%}）。"
              f"分布：{dist}。capped/timeout 不计入失败——那是预算与时间的问题。", pm)

    # ---- 容量：体积 / 写锁 / 增速（阶段 C）--------------------------------
    # 三条都读 capacity_samples（worker 每 5 分钟采），不在告警里现探写锁——
    # 观测动作不该去抢被测资源。
    capm = capacity_metrics(cfg["lock_window_min"])
    size_mb = (capm["db_bytes"] or 0) / 1048576
    limit_mb = round((capm["db_limit_bytes"] or 0) / 1048576)
    ratio = capm["size_ratio"]

    if ratio is not None and ratio >= cfg["db_size_ratio"]:
        full = ratio >= 1
        sev = "critical" if full else "warning"
        _fire(RULE_CAPACITY_DB, sev,
              f"【job-prep 告警】库体积{'已超上限' if full else '接近上限'}："
              f"{size_mb:.1f} MB / {limit_mb} MB"
              f"（{ratio:.0%} ≥ 阈值 {cfg['db_size_ratio']:.0%}）。"
              f"该排期换 Postgres（先补仓储接口，别让 SQL 散在业务里）；"
              f"换之前先把备份 + 恢复演练跑一遍确认时长还能接受。", capm)

    # 用"挡住率"而不是 P95 作触发面：探测预算 250ms，挡住一次 p95 就 ≥250ms，
    # 两条样本就能让 P95 超线——那是统计假象。挡住率配最小样本数才说得通。
    if (capm["n"] >= cfg["lock_min_samples"]
            and capm["blocked_rate"] >= cfg["lock_blocked_rate"]):
        _fire(RULE_CAPACITY_LOCK, RULE_SEVERITY[RULE_CAPACITY_LOCK],
              f"【job-prep 告警】SQLite 写锁排队：近 {cfg['lock_window_min']} 分钟 "
              f"{capm['n']} 次采样里 {capm['blocked']} 次被写锁挡住"
              f"（{capm['blocked_rate']:.0%} ≥ 阈值 {cfg['lock_blocked_rate']:.0%}，"
              f"P95 {capm['lock_wait_p95_ms']}ms）。先查长事务（批量迁移/清理、"
              f"事务里包了 LLM 调用），持续存在再谈换库或读写分离。", capm)

    eta = capm["eta_days"]
    if (eta is not None and eta <= cfg["db_eta_days"]
            and capm["samples_7d"] >= cfg["growth_min_samples"]):
        grow_mb = (capm["growth_bytes_per_day"] or 0) / 1048576
        _fire(RULE_CAPACITY_GROWTH, RULE_SEVERITY[RULE_CAPACITY_GROWTH],
              f"【job-prep 告警】库体积增速偏快：日增 {grow_mb:.1f} MB，"
              f"按此速度约 {eta:.0f} 天到 {limit_mb} MB 上限"
              f"（阈值 {cfg['db_eta_days']:.0f} 天）。线性外推不是预测，"
              f"但值得先确认增长来自哪：span 保留期、台账累积，还是向量表。", capm)

    return {"checked": True, "metrics": metrics,
            "cost": cm, "tools": tm, "plans": pm, "capacity": capm,
            "triggered": triggered, "skipped": skipped}


def history(days: int = 1, limit: int = 50) -> dict:
    """告警历史（面板用）。含送达失败率——它高说明 webhook 链路有问题。"""
    from ..core.db import get_conn  # noqa: PLC0415

    since = _since_iso(max(days, 1) * 24 * 60)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ts, rule, severity, message, metrics, delivered, error "
            "FROM alerts WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (since, max(1, min(int(limit), 200)))).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE ts >= ?", (since,)).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE ts >= ? AND delivered = 0 "
            "AND rule != 'test'", (since,)).fetchone()[0]
    finally:
        conn.close()
    return {"available": True, "total": total, "deliver_failed": int(failed),
            "config": config(),
            "items": [{"ts": r["ts"], "rule": r["rule"], "severity": r["severity"],
                       "message": r["message"],
                       "metrics": json.loads(r["metrics"] or "{}"),
                       "delivered": bool(r["delivered"]), "error": r["error"]}
                      for r in rows]}


def _loop() -> None:
    cfg = config()
    interval = max(cfg["check_sec"], 10)
    while not _loop_stop.is_set():
        try:
            check_once()
        except Exception as e:  # noqa: BLE001  告警自身崩溃只打日志，绝不影响业务
            print(f"[alerts] check failed: {type(e).__name__}: {e}")
        _loop_stop.wait(timeout=interval)


def start_background() -> bool:
    """启动后台判定循环。webhook 未配置或 check_sec=0 时不启动（零开销）。"""
    global _loop_thread
    cfg = config()
    if not cfg["enabled"] or cfg["check_sec"] <= 0:
        return False
    if _loop_thread is not None and _loop_thread.is_alive():
        return True
    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_loop, name="obs-alerts", daemon=True)
    _loop_thread.start()
    return True


def stop_background() -> None:
    """测试与优雅退出用。"""
    _loop_stop.set()
    t = _loop_thread
    if t is not None:
        t.join(timeout=3)
