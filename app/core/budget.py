"""成本预算与并发闸门。

**为什么需要有日预算**：这套系统会自己循环。技能最多 8 轮工具调用、编排会重跑失败步骤、
批量建卡一次 3 张、记忆每轮再抽一次——单次都很便宜，事故形态是"某一步反复重试"，
在无人盯着的时候把几分钱放大成几十块。事后看 llm_usage 能算清花了多少，
但那时钱已经花掉了。闸门必须在**调用之前**拦。

**放在 llm 咽喉而不是各业务入口**：所有 LLM 调用都从 llm._chat_json 过（这也是
台账唯一的写入点）。放在业务入口会漏掉编排 worker、后台起草、记忆抽取这些旁路。

**为什么缓存当天累计**：每次调用前查一次 SUM 太贵（一次 8ms，一轮对话十几次）。
但缓存窗口又不能太长——窗口内的调用全放行的话，5 元的额度可能被花到 6 元。
所以 pricing.record_usage 每记一笔就 budget.add_spend 同步累加，闸门看到的是
"库里已落账 + 本进程刚花掉"的实时值，只在进程刚启动（或跨天）时才真查库。

**并发闸门**：2C2G 单机上每个重任务 = 一个线程 + 一个 SQLite 连接 + 一个在飞的
LLM 请求。不设上限时十个并发流就是把机器拖到做不了别的事。可重入是必须的：
对话线程内部可能触发编排（worker 又要 slot），不可重入会自己把自己锁死。
"""
import contextlib
import datetime
import functools
import threading
import time

from . import config

CACHE_TTL_S = 30.0          # 除 add_spend 外，多久回库校准一次

_lock = threading.Lock()
_day: str | None = None
_spent: float = 0.0
_loaded_at: float = 0.0

_sem_lock = threading.Lock()
_sem: threading.BoundedSemaphore | None = None
_local = threading.local()


class BudgetExceeded(RuntimeError):
    """今日预算用完。调用方应当把它当成"今天到此为止"，不要重试。"""

    def __init__(self, message: str, *, spent: float, limit: float):
        super().__init__(message)
        self.spent = spent
        self.limit = limit


class Busy(RuntimeError):
    """并发已满且等待超时。这是"系统忙"，不是用户的问题，也不该重试太多次。"""


def _query(today: str) -> float:
    from .db import get_conn
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_cny), 0) FROM llm_usage WHERE substr(ts,1,10)=?",
            (today,)).fetchone()
        return float(row[0] or 0.0)
    except Exception:  # noqa: BLE001
        # 查不出来按 0 处理：账本读不到就不该把用户锁在门外
        return 0.0


def spent_today(force: bool = False) -> float:
    """今天已花的钱（含本进程刚发生、可能还没落库的部分）。"""
    global _day, _spent, _loaded_at
    today = datetime.date.today().isoformat()
    with _lock:
        if _day != today:                    # 跨天自动归零，不需要定时任务
            _day, _spent, _loaded_at = today, _query(today), time.monotonic()
            return _spent
        if force or time.monotonic() - _loaded_at > CACHE_TTL_S:
            _spent, _loaded_at = _query(today), time.monotonic()
        return _spent


def add_spend(cost: float) -> None:
    """记一笔已发生的费用。让闸门在缓存窗口内也能立刻看到新支出。"""
    global _day, _spent, _loaded_at
    today = datetime.date.today().isoformat()
    with _lock:
        if _day != today:
            _day, _spent, _loaded_at = today, _query(today), time.monotonic()
        _spent += max(float(cost or 0.0), 0.0)


def status() -> dict:
    """面板用：花了多少、还有多少、是否已拦。"""
    limit = float(config.LLM_DAILY_BUDGET_CNY or 0)
    spent = spent_today()
    return {"spent_cny": round(spent, 4), "limit_cny": limit,
            "remaining_cny": round(max(limit - spent, 0), 4) if limit > 0 else None,
            "ratio": round(spent / limit, 4) if limit > 0 else None,
            "blocked": bool(limit > 0 and spent >= limit),
            "enabled": limit > 0}


def check(cost_hint: float = 0.0) -> None:
    """调用前闸门。超预算抛 BudgetExceeded；limit<=0 表示不限，直接放行。"""
    limit = float(config.LLM_DAILY_BUDGET_CNY or 0)
    if limit <= 0:
        return
    spent = spent_today()
    if spent + max(cost_hint, 0.0) >= limit:
        raise BudgetExceeded(
            f"今日 LLM 预算已用完（已花 ¥{spent:.2f} / 上限 ¥{limit:.2f}）。"
            "这是硬闸门，用来拦住循环重试把成本放大——不要重试，"
            "把已经做完的部分讲清楚，明天自动恢复；"
            "确实需要继续就调高 LLM_DAILY_BUDGET_CNY（设 0 表示不限）。",
            spent=round(spent, 4), limit=limit)


# ---- 并发闸门 ----

def _semaphore() -> threading.BoundedSemaphore:
    global _sem
    if _sem is None:
        with _sem_lock:
            if _sem is None:
                _sem = threading.BoundedSemaphore(max(1, int(config.MAX_CONCURRENT_TASKS)))
    return _sem


def _depth() -> int:
    return getattr(_local, "depth", 0)


@contextlib.contextmanager
def slot(name: str = "task", timeout: float | None = None):
    """占用一个重任务名额。同一线程可重入（内层不再占额度）。

    可重入不是优化而是正确性：对话线程里可能直接调编排，编排的 worker 又要名额；
    不可重入时两个名额互相等，直接死锁。
    """
    if _depth() > 0:
        _local.depth = _depth() + 1
        try:
            yield
        finally:
            _local.depth = _depth() - 1
        return

    sem = _semaphore()
    wait = float(config.MAX_CONCURRENT_TASKS) * 40 if timeout is None else timeout
    ok = sem.acquire(timeout=max(wait, 0.1))
    if not ok:
        raise Busy(f"当前有 {config.MAX_CONCURRENT_TASKS} 个任务在跑（{name} 没排上）。"
                   "等前面的跑完再试，或调高 MAX_CONCURRENT_TASKS")
    _local.depth = 1
    try:
        yield
    finally:
        _local.depth = 0
        sem.release()


def guarded(name: str | None = None, timeout: float | None = None):
    """入口并发闸门装饰器。

    用装饰器而不是在函数体里 `with slot(...)`：那三个入口函数都上百行，
    为了包一层把整段缩进会淹掉真正的改动点，review 时看不出改了什么。
    """
    def deco(fn):
        label = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with slot(label, timeout=timeout):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def reset_cache() -> None:
    """测试与「切换预算」时用：丢掉进程内累计，下次重新查库。"""
    global _day, _spent, _loaded_at, _sem
    with _lock:
        _day, _spent, _loaded_at = None, 0.0, 0.0
    with _sem_lock:
        _sem = None
