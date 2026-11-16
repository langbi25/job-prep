"""上游熔断：三态机（closed / open / half_open）+ 指数退避。

为什么熔断不能和重试混为一谈：
重试解决"这一次"，熔断解决"这一段时间"。没有熔断时，上游持续 5xx 的形态是——
每个请求都打满重试、把 90s 超时耗完，用户等一分半拿一个错误，同时把 4 路并发池
全部占死，连带把本来正常的请求也拖垮。**熔断把雪崩换成"快速失败 + 冷却后单探"**：
失败是立刻返回的，池子不被占用，上游恢复后第一个探针成功就自动闭合。

四条边界，每一条都对应一次踩坑：
1. **429 不算熔断失败**。它是上游"你太快了"，自带 Retry-After；算进熔断会把
   短期限流升级成整段不可用——这是最容易写错的一处。
2. **只统计上游健康类失败**：5xx / 超时 / 连接错误 / 408。400 是我们请求体不合法，
   重试必败但上游是健康的，让它触发熔断等于"自己写错字把上游封了"。
3. **半开只放一个探针**。放开全部 = 刚恢复就被打回原形。
4. **按 上游+模型 分键**，不是全局：换模型时不互相牵连，旧键自然冷却。

状态存内存（单机语义）。跨实例需要共享状态时，把 `_Registry` 换成 Redis 的
HASH + TTL，`allow/success/failure` 签名不变。
"""
from __future__ import annotations

import os
import threading
import time

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断打开期间的快速失败。

    **刻意不是 LLMError**：它不是"这次调用失败"，而是"现在别试"。
    上层按 retryable 决定是否重试的逻辑不能把它再送进去（重试只会继续被拒）。
    """

    def __init__(self, key: str, retry_after: float):
        super().__init__(f"上游熔断中（{key}），{retry_after:.0f}s 后半开探测")
        self.key = key
        self.retry_after = retry_after


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def config() -> dict:
    return {
        "enabled": os.environ.get("CIRCUIT_ENABLED", "1").strip() not in ("0", "false", "off"),
        "fail_threshold": max(1, _env_int("CIRCUIT_FAIL_THRESHOLD", 5)),
        "open_s": max(1.0, _env_float("CIRCUIT_OPEN_S", 30.0)),
        "max_open_s": max(1.0, _env_float("CIRCUIT_MAX_OPEN_S", 300.0)),
    }


def is_upstream_failure(err: BaseException) -> bool:
    """这个异常到底说明"上游不健康"，还是"我们的请求有问题"。

    只有前者能推动熔断。分不清这两类，熔断就会误伤：
    参数错（400）连续出现 5 次就封掉上游，是最典型的误伤形态。
    """
    status = getattr(err, "status", None)
    if status:
        try:
            code = int(status)
        except (TypeError, ValueError):
            return False
        if code == 429:            # 限流：上游是健康的，只是慢点
            return False
        if 500 <= code <= 599 or code == 408:
            return True
        return False
    # 无状态码：连接/超时/网络层错误都算上游不可达
    name = type(err).__name__.lower()
    if isinstance(err, (ConnectionError, TimeoutError, OSError)):
        return True
    return any(t in name for t in ("timeout", "connect", "transport", "remoteprotocol"))


class Breaker:
    __slots__ = ("key", "state", "failures", "opens", "rejects", "open_until",
                 "probe_taken", "last_error", "last_change", "_lock")

    def __init__(self, key: str):
        self.key = key
        self.state = STATE_CLOSED
        self.failures = 0
        self.opens = 0          # 连续打开次数：决定退避倍数，成功一次归零
        self.rejects = 0        # 被快速拒绝的请求数（不落到 llm 台账，只在这里）
        self.open_until = 0.0
        self.probe_taken = False
        self.last_error = ""
        self.last_change = time.time()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """调用前问一次。返回 True 代表"可以打上游"（可能是半开探针）。"""
        cfg = config()
        if not cfg["enabled"]:
            return True
        now = time.monotonic()
        with self._lock:
            if self.state == STATE_OPEN:
                if now < self.open_until:
                    self.rejects += 1
                    return False
                # 冷却结束 → 半开，本次请求就是探针
                self.state = STATE_HALF
                self.probe_taken = True
                self.last_change = time.time()
                return True
            if self.state == STATE_HALF:
                if self.probe_taken:
                    self.rejects += 1          # 探针在飞，其余一律拒绝（不做"陪跑"）
                    return False
                self.probe_taken = True
                return True
            return True

    def success(self) -> None:
        with self._lock:
            if self.state != STATE_CLOSED:
                self.last_change = time.time()
            self.state = STATE_CLOSED
            self.failures = 0
            self.opens = 0
            self.probe_taken = False
            self.open_until = 0.0

    def failure(self, err: BaseException | None = None) -> None:
        cfg = config()
        if not cfg["enabled"]:
            return
        if err is not None and not is_upstream_failure(err):
            return
        now = time.monotonic()
        with self._lock:
            self.last_error = f"{type(err).__name__}: {err}"[:200] if err else ""
            if self.state == STATE_HALF:
                # 探针失败：立刻再开，退避加倍（避免"恢复即被打回"的抖动）
                self.opens += 1
                self._open(now, cfg)
                return
            self.failures += 1
            if self.state == STATE_OPEN:
                return
            if self.failures >= cfg["fail_threshold"]:
                self.opens += 1
                self._open(now, cfg)

    def _open(self, now: float, cfg: dict) -> None:
        backoff = cfg["open_s"] * (2 ** max(0, self.opens - 1))
        self.state = STATE_OPEN
        self.open_until = now + min(backoff, cfg["max_open_s"])
        self.probe_taken = False
        self.failures = 0
        self.last_change = time.time()

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            "key": self.key,
            "state": self.state,
            "failures": self.failures,
            "opens": self.opens,
            "rejects": self.rejects,
            "open_remaining_s": round(max(0.0, self.open_until - now), 1)
            if self.state == STATE_OPEN else 0.0,
            "last_error": self.last_error,
        }


class _Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._b: dict[str, Breaker] = {}

    def get(self, key: str) -> Breaker:
        with self._lock:
            b = self._b.get(key)
            if b is None:
                if len(self._b) > 256:      # 键有上界：键里有模型名，不回收会慢慢涨
                    self._b = {k: v for k, v in self._b.items()
                               if v.state != STATE_CLOSED}
                b = self._b[key] = Breaker(key)
            return b

    def all(self) -> list[Breaker]:
        with self._lock:
            return list(self._b.values())

    def clear(self) -> None:
        with self._lock:
            self._b.clear()


_reg = _Registry()


def breaker(key: str) -> Breaker:
    return _reg.get(key)


def stats() -> dict:
    bs = [b.snapshot() for b in _reg.all()]
    return {
        "config": config(),
        "open": sum(1 for b in bs if b["state"] == STATE_OPEN),
        "half_open": sum(1 for b in bs if b["state"] == STATE_HALF),
        "rejected": sum(b["rejects"] for b in bs),
        "breakers": sorted(bs, key=lambda d: (-d["rejects"], d["key"]))[:10],
    }


def reset() -> None:
    """测试用。"""
    _reg.clear()
