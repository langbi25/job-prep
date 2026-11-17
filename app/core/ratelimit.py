"""入口限流：令牌桶，全局 + 按 IP 两层，只拦 /api/*。

为什么需要它而"日预算 + 并发池"不够：
- 日预算管的是**钱**（一天 5 块），拦不住"1 秒内 200 个请求"这种瞬时打崩；
- 并发池管的是**在飞数**（LLM 4 路），请求可以在池外无限排队，队列本身就能吃满内存；
- 限流管的是**速率**，是唯一能在网关之前把洪水挡住的闸。

为什么手写而不是引 slowapi：
- 限流是几十行数学，引入框架只为省这点代码不值得，还多一个依赖面；
- 必须挂在 SSE 之前且不碰响应体，手写 ASGI 与 GuardMiddleware 同款（一个项目里
  中间件只有一种写法）；
- 单机语义，不需要 Redis。要跨实例时把 `_Bucket` 换成 Redis 的 INCR + EXPIRE，
  `Limiter.check()` 的签名不变。

三层口径：
- **全局桶**：保护整机（DB 单写者、4 路 LLM 池），防"一个脚本打满"；
- **按 IP 桶**：防单个客户端独占；内网/本机场景主要是防止前端 bug 造成请求风暴；
- **只拦 /api**：静态资源一次页面加载就是十几个文件，限流会把正常刷新打成 429；
  /health 也不拦，否则 healthcheck 会把容器判死。

空桶惰性回收：IP 维度不能无限建桶（扫端口/伪造 XFF 就是内存炸弹）。每 256 次检查
清一遍超过 `IDLE_S` 没动静的桶，成本摊薄到常数级。
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time

log = logging.getLogger("ratelimit")

IDLE_S = 300.0          # 桶闲置多久回收
SWEEP_EVERY = 256       # 每多少次检查清一次（摊薄成本）


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    return os.environ.get("RATE_LIMIT_ENABLED", "1").strip() not in ("0", "false", "off")


def trust_proxy() -> bool:
    """是否信任 X-Forwarded-For（nginx 反代场景还原真实客户端 IP）。

    默认关。开启的前提是入口收敛到反代：api 自己也监听端口时，直连的
    客户端可以伪造 XFF 冒充任意 IP 绕过按 IP 限流——本机自用无所谓，
    对外暴露 8765 时必须保持关闭或把 api 端口收进内网。
    """
    return os.environ.get("RATE_LIMIT_TRUST_PROXY", "0").strip() in ("1", "true", "True")


def _client_ip(scope) -> str:
    if trust_proxy():
        for k, v in scope.get("headers") or []:
            if k.lower() == b"x-forwarded-for":
                # nginx 用 $proxy_add_x_forwarded_for 追加，真实客户端在第一段
                first = v.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
                break
    return (scope.get("client") or ("?", 0))[0] or "?"


class _Bucket:
    """令牌桶：容量 burst，每秒补 rate 个。

    `last` 是上次补充时刻——所有计算都基于单调时钟，系统时间跳变不会让桶瞬间满
    或永久空。
    """

    __slots__ = ("rate", "burst", "tokens", "last")

    def __init__(self, rate: float, burst: int, now: float):
        self.rate = max(rate, 0.001)
        self.burst = max(int(burst), 1)
        self.tokens = float(self.burst)
        self.last = now

    def _refill(self, now: float) -> None:
        if now < self.last:
            # 时钟回拨（VM 挂起恢复、注入时钟）：以新的 now 为基准重算，
            # 既不凭空补令牌，也不让桶永远空着——直接 return 会永久卡死。
            self.last = now
            return
        if now == self.last:
            return
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def consume(self, now: float) -> tuple[bool, float]:
        """返回 (是否放行, 需要等待的秒数)。取不到时按缺额算等待时间，给 Retry-After。

        比较带 1e-9 容差：刚好补满一个令牌时浮点误差（0.1s × 10/s = 0.9999999…）
        会把人卡在门外，这种"差一位小数就被限流"的行为最难排查。
        """
        self._refill(now)
        if self.tokens >= 1.0 - 1e-9:
            self.tokens = max(0.0, self.tokens - 1.0)
            return True, 0.0
        return False, (1.0 - self.tokens) / self.rate


class Limiter:
    """两层桶 + 计数。线程安全：uvicorn 单进程多线程（SSE 线程池）会并发进来。"""

    def __init__(self, *, rps: float, burst: int, ip_rps: float, ip_burst: int):
        self._lock = threading.Lock()
        self.ip_rate = max(float(ip_rps), 0.001)
        self.ip_burst = max(int(ip_burst), 1)
        self._global = _Bucket(rps, burst, time.monotonic())
        self._ip: dict[str, tuple[_Bucket, float]] = {}
        self._since = time.time()
        self._checks = 0
        self.allowed = 0
        self.limited = 0
        self._limited_by_ip: dict[str, int] = {}
        self._last_log: dict[str, float] = {}

    def check(self, ip: str, now: float | None = None) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._checks += 1
            if self._checks % SWEEP_EVERY == 0:
                self._sweep(now)
            ok, wait = self._global.consume(now)
            if ok:
                ent = self._ip.get(ip)
                if ent is None:
                    ent = (_Bucket(self.ip_rate, self.ip_burst, now), now)
                    self._ip[ip] = ent
                b, _seen = ent
                ok, wait = b.consume(now)
                self._ip[ip] = (b, now)
            if ok:
                self.allowed += 1
            else:
                self.limited += 1
                self._limited_by_ip[ip] = self._limited_by_ip.get(ip, 0) + 1
            return ok, wait

    def _sweep(self, now: float) -> None:
        stale = [k for k, (_b, seen) in self._ip.items() if now - seen > IDLE_S]
        for k in stale:
            self._ip.pop(k, None)
        if len(self._last_log) > 1024:      # 日志节流表也要有上界
            self._last_log.clear()

    def note_rejection(self, ip: str, path: str) -> None:
        """打一条日志，同一 IP 30 秒最多一条——被刷时日志本身不能变成放大器。"""
        now = time.monotonic()
        if now - self._last_log.get(ip, 0.0) < 30.0:
            return
        self._last_log[ip] = now
        log.warning("rate limited: ip=%s path=%s（同 IP 30s 内只记一条）", ip, path)

    def stats(self) -> dict:
        with self._lock:
            return {
                "since": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._since)),
                "allowed": self.allowed,
                "limited": self.limited,
                "active_ips": len(self._ip),
                "top_limited": sorted(
                    ({"ip": k, "n": v} for k, v in self._limited_by_ip.items()),
                    key=lambda d: -d["n"])[:5],
                "config": {
                    "enabled": enabled(),
                    "global_rps": _env_float("RATE_LIMIT_RPS", 20.0),
                    "global_burst": _env_int("RATE_LIMIT_BURST", 40),
                    "ip_rps": round(self.ip_rate, 3),
                    "ip_burst": self.ip_burst,
                },
            }


_limiter: Limiter | None = None
_limiter_key: tuple = ()
_limiter_lock = threading.Lock()


def get_limiter() -> Limiter:
    """按 env 取单例；参数变了就重建（同 llm._get_pool 的做法，测试也靠这个隔离）。"""
    global _limiter, _limiter_key
    key = (_env_float("RATE_LIMIT_RPS", 20.0), _env_int("RATE_LIMIT_BURST", 40),
           _env_float("RATE_LIMIT_IP_RPS", 10.0), _env_int("RATE_LIMIT_IP_BURST", 20))
    with _limiter_lock:
        if _limiter is None or _limiter_key != key:
            _limiter = Limiter(rps=key[0], burst=key[1], ip_rps=key[2], ip_burst=key[3])
            _limiter_key = key
    return _limiter


def reset() -> None:
    """测试用：丢掉单例，下一轮按当前 env 重建。"""
    global _limiter, _limiter_key
    with _limiter_lock:
        _limiter, _limiter_key = None, ()


def stats() -> dict:
    return get_limiter().stats() if enabled() else {"config": {"enabled": False}}


class RateLimitMiddleware:
    """挂载：app.add_middleware(RateLimitMiddleware)。

    放在 GuardMiddleware 之外（最后添加），先限流再验 token：
    洪水不该享受"先解析 token 再拒绝"的待遇，鉴权也不该被刷爆。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if not path.startswith("/api") or not enabled():
            await self.app(scope, receive, send)
            return

        ip = _client_ip(scope)
        lim = get_limiter()
        ok, wait = lim.check(ip)
        if ok:
            await self.app(scope, receive, send)
            return

        lim.note_rejection(ip, path)
        retry_after = max(1, math.ceil(wait))
        body = json.dumps({
            "detail": f"请求过于频繁，请 {retry_after}s 后重试（限流：全局与按 IP 两层令牌桶）",
            "retry_after": retry_after,
        }, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"retry-after", str(retry_after).encode()),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
