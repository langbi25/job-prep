"""可观测告警：错误率 / 慢调用超阈值 → webhook 推送。

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


def config() -> dict:
    """当前阈值摘要。webhook 只回是否配置 + 域名打码——完整 URL 是凭据的一部分，
    不能从面板接口原样吐出去。"""
    url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    masked = ""
    if url:
        rest = url.split("://", 1)[-1]
        host = rest.split("/", 1)[0].split("?")[0]
        masked = f"...{host}（已配置）"
    return {
        "enabled": bool(url),
        "webhook": masked,
        "format": (os.getenv("ALERT_FORMAT") or "generic").strip().lower(),
        "window_min": _env_int("ALERT_WINDOW_MIN", 5),
        "min_calls": _env_int("ALERT_MIN_CALLS", 10),
        "error_rate": _env_float("ALERT_ERROR_RATE", 0.5),
        "slow_ms": _env_int("ALERT_SLOW_MS", 10000),
        "slow_count": _env_int("ALERT_SLOW_COUNT", 3),
        "cooldown_min": _env_int("ALERT_COOLDOWN_MIN", 30),
        "check_sec": _env_int("ALERT_CHECK_SEC", 60),
    }


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


def _payload(message: str, fmt: str) -> dict:
    """三种接收端的报文格式。generic 给自写接收器；wecom/dingtalk 是机器人格式。"""
    if fmt == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": message}}
    if fmt == "dingtalk":
        return {"msgtype": "markdown",
                "markdown": {"title": "job-prep 告警", "text": message}}
    return {"text": message}


def _deliver(message: str) -> tuple[bool, str | None]:
    url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return False, "ALERT_WEBHOOK_URL 未配置，仅落库不推送"
    fmt = (os.getenv("ALERT_FORMAT") or "generic").strip().lower()
    try:
        # trust_env=False：告警链路自己必须是最稳的一段，不走系统代理（与 OTLP 同款坑）
        session = requests.Session()
        session.trust_env = False
        resp = session.post(url, json=_payload(message, fmt), timeout=5)
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
    ok, err = _deliver(msg)
    _record("test", "info", msg, metrics, ok, err)
    return {"delivered": ok, "error": err, "metrics": metrics}


def check_once() -> dict:
    """判定一次。返回触发与跳过明细——后台循环和测试都从这里走，单一入口。"""
    cfg = config()
    metrics = window_metrics(cfg["window_min"])
    triggered, skipped = [], []

    rate = metrics["error_rate"]
    if metrics["total"] >= cfg["min_calls"] and rate >= cfg["error_rate"]:
        if _recently_alerted(RULE_ERROR_RATE, cfg["cooldown_min"]):
            skipped.append({"rule": RULE_ERROR_RATE, "reason": "冷却期内"})
        else:
            tops = "、".join(f"{t['name']}×{t['n']}" for t in metrics["top_errors"]) or "（无明细）"
            msg = (f"【job-prep 告警】错误率过高：近 {cfg['window_min']} 分钟顶层调用 "
                   f"{metrics['total']} 次，错误 {metrics['errors']} 次"
                   f"（{rate:.0%} ≥ 阈值 {cfg['error_rate']:.0%}）。最常报错：{tops}")
            ok, err = _deliver(msg)
            _record(RULE_ERROR_RATE, "warning", msg, metrics, ok, err)
            triggered.append({"rule": RULE_ERROR_RATE, "delivered": ok, "error": err})

    if metrics["slow"] >= cfg["slow_count"]:
        if _recently_alerted(RULE_SLOW, cfg["cooldown_min"]):
            skipped.append({"rule": RULE_SLOW, "reason": "冷却期内"})
        else:
            msg = (f"【job-prep 告警】慢调用集中：近 {cfg['window_min']} 分钟有 "
                   f"{metrics['slow']} 次顶层调用超过 {cfg['slow_ms']}ms"
                   f"（阈值 {cfg['slow_count']} 次）。去 Traces 面板看瀑布图定位慢在哪一层。")
            ok, err = _deliver(msg)
            _record(RULE_SLOW, "warning", msg, metrics, ok, err)
            triggered.append({"rule": RULE_SLOW, "delivered": ok, "error": err})

    return {"checked": True, "metrics": metrics,
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
