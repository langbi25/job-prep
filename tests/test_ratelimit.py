"""入口限流测试（零网络、零 LLM）。

盯三件事：
1. **桶的数学**：突发放行 burst 个，第 burst+1 个被拒且 Retry-After 合理，
   时间过去后能补回来——用注入时钟，不 sleep；
2. **中间件只在 /api 生效**：静态资源与 /health 不能被限流（否则 healthcheck
   会把容器判死，刷新页面会被打成 429）；
3. **被拒请求带 Retry-After**：客户端能知道"等多久"，这是限流和"直接 500"的区别。
"""
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import ratelimit  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_RPS", "20")
    monkeypatch.setenv("RATE_LIMIT_BURST", "40")
    monkeypatch.setenv("RATE_LIMIT_IP_RPS", "10")
    monkeypatch.setenv("RATE_LIMIT_IP_BURST", "20")
    ratelimit.reset()
    yield
    ratelimit.reset()


def _limiter(**kw):
    return ratelimit.Limiter(**{"rps": 10, "burst": 3, "ip_rps": 10, "ip_burst": 3, **kw})


def test_burst_then_reject_then_refill():
    lim = _limiter()
    t0 = 1000.0
    for i in range(3):
        ok, wait = lim.check("1.1.1.1", now=t0)
        assert ok, f"第 {i+1} 个应放行（桶容量 3）"
    ok, wait = lim.check("1.1.1.1", now=t0)
    assert not ok
    # rate=10/s → 补 1 个令牌要 0.1s，Retry-After 至少 0.09
    assert 0.09 <= wait <= 0.11, wait
    # 0.1s 后正好补一个
    ok, _ = lim.check("1.1.1.1", now=t0 + 0.1)
    assert ok


def test_ip_buckets_are_independent():
    lim = _limiter(burst=100, ip_burst=2)
    assert lim.check("a", now=0.0)[0]
    assert lim.check("a", now=0.0)[0]
    assert not lim.check("a", now=0.0)[0]
    # 另一个 IP 有自己的桶（全局桶还有额度）
    assert lim.check("b", now=0.0)[0]


def test_idle_buckets_are_swept():
    lim = _limiter(burst=1000, ip_burst=1000)
    lim.check("idle-ip", now=0.0)
    assert "idle-ip" in lim._ip
    # 手动触发一次清扫（正常由每 256 次检查触发）
    lim._sweep(ratelimit.IDLE_S + 1.0)
    assert "idle-ip" not in lim._ip


def test_stats_shape():
    lim = _limiter()
    for _ in range(5):
        lim.check("2.2.2.2", now=0.0)
    st = lim.stats()
    assert st["limited"] == 2
    assert st["top_limited"][0] == {"ip": "2.2.2.2", "n": 2}
    assert st["config"]["enabled"] is True


def _app():
    app = FastAPI()
    app.add_middleware(ratelimit.RateLimitMiddleware)

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/assets/app.js")
    def asset():
        return {"js": True}

    return TestClient(app)


def test_middleware_rejects_over_limit_with_retry_after(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_RPS", "1")
    monkeypatch.setenv("RATE_LIMIT_BURST", "1")
    monkeypatch.setenv("RATE_LIMIT_IP_RPS", "1")
    monkeypatch.setenv("RATE_LIMIT_IP_BURST", "1")
    ratelimit.reset()
    c = _app()
    assert c.get("/api/ping").status_code == 200
    r = c.get("/api/ping")
    assert r.status_code == 429
    assert r.json()["retry_after"] >= 1
    assert int(r.headers["retry-after"]) >= 1
    assert "请求过于频繁" in r.json()["detail"]


def test_middleware_skips_health_and_static(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_BURST", "1")
    monkeypatch.setenv("RATE_LIMIT_RPS", "0.01")
    monkeypatch.setenv("RATE_LIMIT_IP_BURST", "1")
    monkeypatch.setenv("RATE_LIMIT_IP_RPS", "0.01")
    ratelimit.reset()
    c = _app()
    assert c.get("/api/ping").status_code == 200
    # /api 已耗尽
    assert c.get("/api/ping").status_code == 429
    # 静态与 health 永远放行：否则页面刷新/容器探活会被限流打死
    for _ in range(20):
        assert c.get("/health").status_code == 200
        assert c.get("/assets/app.js").status_code == 200


def test_disabled_switch(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("RATE_LIMIT_BURST", "1")
    ratelimit.reset()
    c = _app()
    for _ in range(10):
        assert c.get("/api/ping").status_code == 200
    assert ratelimit.stats() == {"config": {"enabled": False}}
