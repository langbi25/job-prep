"""容量采样与扩容决策（阶段 C）。

**问题**：什么情况下该换 Postgres、该加副本、该上 Redis？如果回答是"感觉库
有点大了"，那这个决定就没法复核、也没法在面试里讲清。所以这一层只做一件事：
把触发条件写成可测阈值，把测量结果攒成趋势，让"暂时不换"也成为一个有依据的
结论——而不是懒得动。

**测什么**（每 5 分钟一次，由 job runner 在 worker 容器里跑）：

| 信号 | 取法 | 为什么是它 |
| --- | --- | --- |
| 库文件字节 | PRAGMA page_count × page_size | 备份/恢复时长、单文件搬运风险的直接来源 |
| WAL 字节 | `kb.sqlite-wal` 文件大小 | 写并发高时会鼓起来，checkpoint 跟不上就是信号 |
| 写锁等待 | 独立连接 `BEGIN IMMEDIATE` 计时（预算 250ms） | WAL 单写者，这条直接量化"别人要排队多久" |
| 写锁是否挡住 | 同上，抛 locked 即被挡 | 挡住率 > 1% 说明写事务已经长到影响别人 |
| 写入量代理 | 热表 `MAX(rowid)` 之和的增量 | 没有 `rows written` 计数器，rowid 上界是最便宜的代理 |
| load1 / CPU 数 | os.getloadavg() / os.cpu_count() | 判断"是不是算力不够"（实测结论是：不是） |

**为什么探测用 BEGIN IMMEDIATE 而不是统计错误**：应用层的 "database is locked"
只会在**等满 busy_timeout 之后**才出现，等 3ms 就拿到锁的排队是看不见的。
主动探测能测到"还没报错但已经在排队"的区间——这正是"快该换库了"的早期信号。

**为什么采样放 worker**：api 是请求面，不该为了观测往自己身上加定时器；worker
本来就是"定时干活的进程"。且探测读的是跨进程写锁，worker 视角和 api 视角一致。
"""
from __future__ import annotations

import datetime
import math
import os
import sqlite3
import time
from pathlib import Path

from ..core import db

# 采样间隔与保留期（jobdefs 注册 job 时用同一个常量）
SAMPLE_INTERVAL_S = 300
KEEP_DAYS = 30

# 热表：写入量的代理。只取"高频写"的表，冷表（prompt_versions 之类）加进来
# 只会让增量噪声变大。spans 是 TEXT 主键，但 SQLite 普通表仍有隐式 rowid。
HOT_TABLES = ("spans", "llm_usage", "tool_calls", "console_messages",
              "turn_facts", "agent_plans", "job_runs")

# 探测预算：一条写事务正常是毫秒级，等 250ms 还没拿到锁 = 真有长事务在占着。
PROBE_WAIT_MS = 250

# 阈值（触发判定全部写死在这里，方便面试时逐条解释，也方便测试钉住）
DB_SIZE_WARN_RATIO = 0.7          # 到上限 70% 开始预警
LOCK_WAIT_P95_TRIGGER_MS = 200    # 近 7 天写锁等待 P95 超过 200ms
LOCK_WAIT_P95_WARN_MS = 50
LOCK_BLOCKED_RATE_TRIGGER = 0.01  # 探测被挡住的样本占比 > 1%
LOCK_BLOCKED_RATE_WARN = 0.002
MIN_SAMPLES_FOR_TREND = 20        # 少于这么多样本，趋势只报数不下结论


def db_limit_bytes() -> int:
    """库体积上限（换库触发线）。默认 2 GiB。

    2 GiB 不是"SQLite 撑不住"——SQLite 官方支持到 TB 级。选它是因为**运维动作**
    撑不住：单文件冷备 + 校验 + 恢复演练这条链路，2 GiB 以内是分钟级，再大就
    开始影响"出事能多快回来"。真要调，改 env 而不是改代码。
    """
    try:
        mb = int(os.environ.get("CAPACITY_DB_LIMIT_MB", "") or 2048)
    except ValueError:
        mb = 2048
    return max(mb, 64) * 1024 * 1024


def api_replicas() -> int:
    """当前 api 副本数（部署事实，不是运行时自动发现的）。

    单副本时限流/熔断状态放进程内存是对的；一旦 >1，桶和熔断状态必须在实例间
    共享，否则每个副本各自计数 = 实际阈值翻 N 倍。这个数字由 compose/k8s 注入。
    """
    try:
        return max(int(os.environ.get("API_REPLICAS", "") or 1), 1)
    except ValueError:
        return 1


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

def db_file_stats() -> dict:
    """库文件与热表写入量（只读，不改数据）。"""
    conn = db.get_conn()
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
    rows_total = 0
    for table in HOT_TABLES:
        try:
            v = conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
            rows_total += int(v or 0)
        except sqlite3.Error:
            # 表还没建（老库 / 精简库）就跳过，不能让一个缺失的表把采样打挂
            continue
    wal = Path(str(db.DB_PATH) + "-wal")
    try:
        wal_bytes = wal.stat().st_size if wal.exists() else 0
    except OSError:
        wal_bytes = 0
    return {"db_bytes": page_count * page_size, "wal_bytes": wal_bytes,
            "rows_total": rows_total}


def probe_write_lock(wait_ms: int = PROBE_WAIT_MS) -> dict:
    """探测写锁：独立连接抢一次 `BEGIN IMMEDIATE`，立刻回滚。

    返回 {blocked, wait_ms, error}。`wait_ms` 是**真实排队时间**：
    - 空闲时是 0~2ms（拿锁本身的开销）；
    - 有写事务在跑时是排队时长；
    - `blocked=True` 表示等满预算仍没拿到（此时 wait_ms ≈ 预算）。
    探测只用独立连接、不做任何写操作（BEGIN IMMEDIATE 只取锁不写页），
    回滚后对 WAL 无残留。
    """
    path = str(db.DB_PATH)
    if not Path(path).exists():
        # 不能让它去 connect：sqlite3 会顺手建一个空库文件，
        # 把"库还没初始化"变成"库突然变成 0 字节"。
        return {"blocked": False, "wait_ms": 0, "error": "库文件不存在，跳过探测"}
    t0 = time.perf_counter()
    try:
        conn = sqlite3.connect(path, timeout=max(int(wait_ms), 0) / 1000.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            return {"blocked": False, "wait_ms": int((time.perf_counter() - t0) * 1000)}
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return {"blocked": True,
                "wait_ms": int((time.perf_counter() - t0) * 1000),
                "error": str(e)[:120]}


def _load() -> tuple[float | None, int]:
    """load1 与 CPU 数。容器里 /proc/loadavg 是**宿主**视角（未命名空间隔离），
    所以这个值要跟宿主核数一起读才有意义——用它回答"机器是不是吃满了"，
    而不是"这个容器用了多少 CPU"。"""
    try:
        load1: float | None = float(os.getloadavg()[0])
    except (AttributeError, OSError):
        load1 = None      # Windows 没有 getloadavg
    try:
        cpu_n = int(os.cpu_count() or 0)
    except Exception:      # noqa: BLE001
        cpu_n = 0
    return load1, cpu_n


def sample() -> dict:
    """采一次并落库，顺带清理过期样本。返回这条记录（字段与表一致）。"""
    stats = db_file_stats()
    lock = probe_write_lock()
    load1, cpu_n = _load()
    rec = {
        "ts": _now(),
        "db_bytes": stats["db_bytes"],
        "wal_bytes": stats["wal_bytes"],
        "lock_wait_ms": lock["wait_ms"],
        "lock_blocked": 1 if lock["blocked"] else 0,
        "rows_total": stats["rows_total"],
        "load1": load1,
        "cpu_count": cpu_n,
    }
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO capacity_samples "
        "(ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (rec["ts"], rec["db_bytes"], rec["wal_bytes"], rec["lock_wait_ms"],
         rec["lock_blocked"], rec["rows_total"], rec["load1"], rec["cpu_count"]))
    # 保留期在采样时顺手清：多一个清理 job 就多一份"它挂了没人发现"的风险，
    # 而这张表本来就是采样 job 自己的产出。
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=KEEP_DAYS)
              ).isoformat(timespec="seconds")
    conn.execute("DELETE FROM capacity_samples WHERE ts < ?", (cutoff,))
    conn.commit()
    rec["lock_error"] = lock.get("error")
    return rec


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------

def _p95(values: list[int]) -> int | None:
    """最近秩分位数（与 scripts/load_test.py 同口径，避免两处算法不一致）。"""
    if not values:
        return None
    xs = sorted(values)
    k = max(1, math.ceil(0.95 * len(xs)))
    return int(xs[min(k, len(xs)) - 1])


def history(days: int = 7) -> dict:
    """近 N 天趋势：体积增速、写锁等待分布、挡住率、写入量速率。"""
    since = (datetime.datetime.now() - datetime.timedelta(days=max(1, int(days)))
             ).isoformat(timespec="seconds")
    rows = db.get_conn().execute(
        "SELECT ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count "
        "FROM capacity_samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    items = [dict(r) for r in rows]
    n = len(items)

    growth = None
    eta_days = None
    if n >= 2:
        first, last = items[0], items[-1]
        try:
            t0 = datetime.datetime.fromisoformat(first["ts"])
            t1 = datetime.datetime.fromisoformat(last["ts"])
            span_days = max((t1 - t0).total_seconds() / 86400.0, 0.0)
        except (TypeError, ValueError):
            span_days = 0.0
        # 跨度不足 1 小时不报增速：两个点差几分钟，除出来的日增速能差十倍，
        # 那种数字贴到面板上会被当成"明天就要爆"。
        if span_days >= 1 / 24:
            growth = round((last["db_bytes"] - first["db_bytes"]) / span_days, 1)
            limit = db_limit_bytes()
            if growth > 0 and last["db_bytes"] < limit:
                eta_days = round((limit - last["db_bytes"]) / growth, 1)

    waits = [int(x["lock_wait_ms"] or 0) for x in items]
    blocked_rate = round(sum(int(x["lock_blocked"] or 0) for x in items) / n, 4) if n else None
    rows_rate = None
    if n >= 2:
        try:
            t0 = datetime.datetime.fromisoformat(items[0]["ts"])
            t1 = datetime.datetime.fromisoformat(items[-1]["ts"])
            mins = (t1 - t0).total_seconds() / 60.0
            if mins >= 5:
                rows_rate = round((items[-1]["rows_total"] - items[0]["rows_total"]) / mins, 2)
        except (TypeError, ValueError):
            pass

    # 序列给前端画走势，最多 120 个点（5 分钟一采 = 10 小时），多了没必要
    series = [{"ts": x["ts"], "db_bytes": x["db_bytes"], "lock_wait_ms": x["lock_wait_ms"]}
              for x in items][-120:]
    return {"days": max(1, int(days)), "n": n,
            "db_bytes_first": items[0]["db_bytes"] if n else None,
            "db_bytes_last": items[-1]["db_bytes"] if n else None,
            "growth_bytes_per_day": growth, "eta_days": eta_days,
            "lock_wait_p95_ms": _p95(waits), "lock_wait_max_ms": max(waits) if waits else None,
            "lock_blocked_rate": blocked_rate, "rows_per_min": rows_rate,
            "series": series}


# ---------------------------------------------------------------------------
# 判定（纯函数：阈值全在参数里，测试直接喂数）
# ---------------------------------------------------------------------------

def _trigger(key: str, label: str, state: str, why: str, signals: list[dict],
             action: str) -> dict:
    return {"key": key, "label": label, "state": state, "why": why,
            "signals": signals, "action": action}


def evaluate(*, db_bytes: int, limit_bytes: int, growth_bytes_per_day: float | None,
             eta_days: float | None, lock_wait_p95_ms: int | None,
             lock_blocked_rate: float | None, samples_n: int, api_replicas_n: int,
             load1: float | None, cpu_count: int,
             limited_ratio: float | None) -> dict:
    """把测量值翻译成四个"要不要动架构"的结论。

    state 语义：
    - `ok`        没到阈值，继续现在的做法
    - `warn`      接近阈值，开始观察 / 可以先做准备工作
    - `triggered` 已越过阈值，该动手
    - `manual`    指标判断不了，只能由部署事实决定（多机/K8s）
    """
    size_ratio = (db_bytes / limit_bytes) if limit_bytes else 0.0
    size_mb = round(db_bytes / 1024 / 1024, 1)
    limit_mb = round(limit_bytes / 1024 / 1024)
    blocked = lock_blocked_rate if lock_blocked_rate is not None else 0.0
    p95 = lock_wait_p95_ms if lock_wait_p95_ms is not None else 0

    # ---- 1. 换 Postgres ---------------------------------------------------
    # 体积是事实，不受样本量影响；**分布类信号（P95 / 挡住率）必须够样本**才算数：
    # 刚部署时两条样本里有一条排队就宣布"该换库"，是拿噪声当结论。
    enough = samples_n >= MIN_SAMPLES_FOR_TREND
    pg_state, pg_why = "ok", "库体积与写锁都健康，单写者没有成为瓶颈"
    if size_ratio >= 1:
        pg_state, pg_why = "triggered", f"库体积已越过上限（{size_mb} MB ≥ {limit_mb} MB）"
    elif enough and p95 > LOCK_WAIT_P95_TRIGGER_MS:
        pg_state, pg_why = "triggered", f"写锁等待 P95 {p95}ms > {LOCK_WAIT_P95_TRIGGER_MS}ms，写事务在互相排队"
    elif enough and blocked > LOCK_BLOCKED_RATE_TRIGGER:
        pg_state, pg_why = "triggered", f"{blocked:.1%} 的采样被写锁挡住（阈值 {LOCK_BLOCKED_RATE_TRIGGER:.0%}）"
    elif size_ratio >= DB_SIZE_WARN_RATIO:
        pg_state, pg_why = "warn", f"库体积到上限的 {size_ratio:.0%}，备份/恢复时长开始变长"
    elif p95 > LOCK_WAIT_P95_TRIGGER_MS or blocked > LOCK_BLOCKED_RATE_TRIGGER:
        pg_state = "warn"
        pg_why = (f"出现过写锁排队（P95 {p95}ms，挡住 {blocked:.1%}），"
                  f"但只有 {samples_n} 条样本（<{MIN_SAMPLES_FOR_TREND}），先继续采样再判")
    elif p95 > LOCK_WAIT_P95_WARN_MS:
        pg_state, pg_why = "warn", f"写锁等待 P95 {p95}ms，还没到 {LOCK_WAIT_P95_TRIGGER_MS}ms 但已在排队"
    elif blocked > LOCK_BLOCKED_RATE_WARN:
        pg_state, pg_why = "warn", f"已有 {blocked:.1%} 的采样被写锁挡住，开始有长事务"
    elif not enough:
        pg_why = f"样本 {samples_n} 条（<{MIN_SAMPLES_FOR_TREND}），当前读数正常但趋势未成形"
    postgres = _trigger(
        "postgres", "换 Postgres（读写分离 / 多写实例）", pg_state, pg_why,
        [{"k": "库体积", "v": f"{size_mb} MB / {limit_mb} MB", "bad": size_ratio >= 1},
         {"k": "写锁等待 P95", "v": f"{p95}ms", "bad": p95 > LOCK_WAIT_P95_TRIGGER_MS},
         {"k": "被锁挡住", "v": f"{blocked:.2%}", "bad": blocked > LOCK_BLOCKED_RATE_TRIGGER},
         {"k": "日增速", "v": (f"{growth_bytes_per_day / 1024 / 1024:.1f} MB/天" if growth_bytes_per_day else "样本不足"),
          "bad": False}],
        "迁移数据 + 改连接层；先补一层仓储接口再换库，别让 SQL 散在业务里")

    # ---- 2. 水平扩容（多副本，前置是 Redis）--------------------------------
    if api_replicas_n > 1:
        rep_state, rep_why = "triggered", f"已是 {api_replicas_n} 副本，限流/熔断状态必须跨实例共享"
    elif pg_state == "triggered":
        rep_state, rep_why = "warn", "库侧已到触发线：先换库，副本才有意义（否则多副本会放大单写者竞争）"
    elif p95 > LOCK_WAIT_P95_WARN_MS:
        rep_state, rep_why = "warn", "单实例写锁已在排队，加副本只会让排队更长"
    else:
        rep_state, rep_why = "ok", "单副本未触顶：请求面不是瓶颈（压测拐点 4~8 并发，瓶颈在 SQLite 单写者）"
    replicas = _trigger(
        "replicas", "api 横向扩副本（并上 Redis 共享闸门状态）", rep_state, rep_why,
        [{"k": "当前副本", "v": str(api_replicas_n), "bad": api_replicas_n > 1},
         {"k": "限流拒绝占比", "v": (f"{limited_ratio:.1%}" if limited_ratio is not None else "—"),
          "bad": bool(limited_ratio and limited_ratio > 0.2)}],
        "先接 Redis 存限流桶与熔断状态，再起第二副本；否则每个副本各自计数，阈值实际翻倍")

    # ---- 3. 垂直扩容（加 CPU）---------------------------------------------
    load_per_cpu = (load1 / cpu_count) if (load1 is not None and cpu_count) else None
    if load_per_cpu is None:
        cpu_state, cpu_why = "manual", "读不到 loadavg（Windows / 无 procfs），只能看容器指标"
    elif load_per_cpu > 1.0:
        cpu_state, cpu_why = "triggered", f"load/cpu = {load_per_cpu:.2f} > 1，机器已排队"
    elif load_per_cpu > 0.7:
        cpu_state, cpu_why = "warn", f"load/cpu = {load_per_cpu:.2f}，接近吃满"
    else:
        cpu_state, cpu_why = "ok", f"load/cpu = {load_per_cpu:.2f}，算力不是瓶颈"
    cpu = _trigger(
        "cpu", "垂直扩容（加 CPU 核数）", cpu_state, cpu_why,
        [{"k": "load1 / 核数", "v": (f"{load1:.2f} / {cpu_count} = {load_per_cpu:.2f}"
                                     if load_per_cpu is not None else "—"),
          "bad": bool(load_per_cpu and load_per_cpu > 1.0)}],
        "实测 1 CPU 与不限额吞吐无差（291→294 rps @并发4），加核对这个瓶颈无效；先确认是 CPU 再扩")

    # ---- 4. 多机 / K8s ----------------------------------------------------
    k8s = _trigger(
        "k8s", "多机部署 / K8s", "manual",
        "指标判断不了：单机 + SQLite 文件卷，迁到多机属于「要不要多环境、要不要跨机容灾」的决策",
        [{"k": "触发方式", "v": "人工（需多机/多环境/跨机容灾）", "bad": False}],
        "先容器编排把无状态面拆干净（已完成 api/worker/web），再谈 K8s；单机上 K8s 是负优化")

    triggers = [postgres, replicas, cpu, k8s]
    triggered = [t for t in triggers if t["state"] == "triggered"]
    warned = [t for t in triggers if t["state"] == "warn"]
    if triggered:
        verdict = "有触发项：" + "、".join(t["label"] for t in triggered)
    elif warned:
        verdict = "未触发，但有 " + "、".join(t["label"] for t in warned) + " 接近阈值"
    else:
        verdict = "全部未触发：继续单机 + SQLite + 单副本"
    return {"verdict": verdict, "triggers": triggers,
            "triggered_n": len(triggered), "warn_n": len(warned)}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def recent_stats(window_min: int = 30) -> dict:
    """最近 N 分钟的**现状**（告警判定用）。

    与 history() 的分工：history 回答"7 天趋势怎么样、按这速度多久到上限"，
    这里回答"现在是不是已经在排队"。告警要的是后者——用 7 天 P95 报警，
    意味着一次 20 分钟的长事务要等 7 天才响。

    返回 blocked_rate 与 p95 都带样本数 n，让调用方自己决定下限：
    n=1 时任何比率都是 0% 或 100%，直接报警等于随机喊狼。
    """
    since = (datetime.datetime.now()
             - datetime.timedelta(minutes=max(1, int(window_min)))).isoformat(timespec="seconds")
    rows = db.get_conn().execute(
        "SELECT ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count "
        "FROM capacity_samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    items = [dict(r) for r in rows]
    n = len(items)
    waits = [int(x["lock_wait_ms"] or 0) for x in items]
    blocked = sum(int(x["lock_blocked"] or 0) for x in items)
    latest = items[-1] if items else None
    limit = db_limit_bytes()
    return {"window_min": max(1, int(window_min)), "n": n, "blocked": blocked,
            "blocked_rate": round(blocked / n, 4) if n else 0.0,
            "lock_wait_p95_ms": _p95(waits),
            "lock_wait_max_ms": max(waits) if waits else None,
            "db_bytes": int(latest["db_bytes"]) if latest else None,
            "wal_bytes": int(latest["wal_bytes"]) if latest else None,
            "db_limit_bytes": limit,
            "size_ratio": round(latest["db_bytes"] / limit, 4) if (latest and limit) else None}


def report(refresh: bool = False, days: int = 7) -> dict:
    """面板用：最新快照 + 近 7 天趋势 + 四项判定。"""
    from ..core import circuit as _circuit
    from ..core import ratelimit as _ratelimit

    conn = db.get_conn()
    latest = conn.execute(
        "SELECT ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count "
        "FROM capacity_samples ORDER BY ts DESC LIMIT 1").fetchone()
    stale = True
    if latest is not None:
        try:
            age = (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(latest["ts"])).total_seconds()
            stale = age > SAMPLE_INTERVAL_S * 2
        except (TypeError, ValueError):
            stale = True
    # 过期就顺手采一次：面板打开的第一次不该是"暂无数据"，而应该是"刚测的"。
    if refresh or latest is None or stale:
        sample()
        latest = conn.execute(
            "SELECT ts, db_bytes, wal_bytes, lock_wait_ms, lock_blocked, rows_total, load1, cpu_count "
            "FROM capacity_samples ORDER BY ts DESC LIMIT 1").fetchone()

    hist = history(days)
    rl = _ratelimit.stats()
    allowed, limited = int(rl.get("allowed") or 0), int(rl.get("limited") or 0)
    total = allowed + limited
    limited_ratio = round(limited / total, 4) if total else 0.0
    cur = dict(latest) if latest is not None else {}
    ev = evaluate(
        db_bytes=int(cur.get("db_bytes") or 0), limit_bytes=db_limit_bytes(),
        growth_bytes_per_day=hist["growth_bytes_per_day"], eta_days=hist["eta_days"],
        lock_wait_p95_ms=hist["lock_wait_p95_ms"],
        lock_blocked_rate=hist["lock_blocked_rate"], samples_n=hist["n"],
        api_replicas_n=api_replicas(), load1=cur.get("load1"),
        cpu_count=int(cur.get("cpu_count") or 0), limited_ratio=limited_ratio)
    c = _circuit.stats()
    return {
        "generated_at": _now(),
        "sample": cur,
        "history": hist,
        "gate": {"allowed": allowed, "limited": limited,
                 "limited_ratio": limited_ratio,
                 "circuit_open": int(c.get("open") or 0),
                 "circuit_rejected": int(c.get("rejected") or 0)},
        "limits": {"db_limit_bytes": db_limit_bytes(),
                   "db_limit_mb": round(db_limit_bytes() / 1024 / 1024),
                   "lock_wait_p95_trigger_ms": LOCK_WAIT_P95_TRIGGER_MS,
                   "lock_blocked_rate_trigger": LOCK_BLOCKED_RATE_TRIGGER,
                   "sample_interval_s": SAMPLE_INTERVAL_S,
                   "keep_days": KEEP_DAYS,
                   "api_replicas": api_replicas()},
        **ev,
    }
