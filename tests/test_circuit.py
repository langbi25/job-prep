"""熔断器测试（零网络、零 LLM）。

四条边界逐条钉住，因为它们正是熔断最容易写错的地方：
1. 连续上游失败到阈值 → 打开 → 立刻快速失败（不占池、不耗超时）；
2. 429 不计入失败（上游健康，只是限速）；400 也不计入（我们自己的请求不合法）；
3. 冷却结束 → 半开 → **只放一个探针**，探针成功闭合、失败再开且退避加倍；
4. 键按 上游+模型 隔离，换模型不互相牵连。
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import circuit  # noqa: E402
from app.core.llm import LLMError  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("CIRCUIT_ENABLED", "1")
    monkeypatch.setenv("CIRCUIT_FAIL_THRESHOLD", "3")
    monkeypatch.setenv("CIRCUIT_OPEN_S", "10")
    monkeypatch.setenv("CIRCUIT_MAX_OPEN_S", "60")
    circuit.reset()
    yield
    circuit.reset()


def _err(status=503):
    return LLMError("boom", status=status, retryable=True)


def test_opens_after_threshold_and_rejects():
    b = circuit.breaker("up|m")
    for _ in range(3):
        assert b.allow()
        b.failure(_err())
    assert b.state == circuit.STATE_OPEN
    assert not b.allow()          # 立刻快速失败
    assert not b.allow()
    assert b.snapshot()["rejects"] == 2


def test_429_and_400_do_not_trip_breaker():
    b = circuit.breaker("up|m")
    for _ in range(10):
        b.allow()
        b.failure(_err(429))       # 上游限速：健康
        b.failure(_err(400))       # 我们的请求不合法：更不该封上游
    assert b.state == circuit.STATE_CLOSED
    assert b.failures == 0


def test_success_resets_failures():
    b = circuit.breaker("up|m")
    b.allow(); b.failure(_err())
    b.allow(); b.failure(_err())
    b.success()
    assert b.failures == 0
    b.allow(); b.failure(_err())
    assert b.state == circuit.STATE_CLOSED   # 没有连续 3 次


def test_half_open_single_probe_then_close():
    b = circuit.breaker("up|m")
    for _ in range(3):
        b.allow(); b.failure(_err())
    b.open_until = time.monotonic() - 0.01     # 冷却结束（不 sleep）
    assert b.allow()                            # 这就是那个探针
    assert b.state == circuit.STATE_HALF
    assert not b.allow()                        # 探针在飞，其余一律拒绝
    b.success()
    assert b.state == circuit.STATE_CLOSED
    assert b.allow()


def test_half_open_probe_failure_reopens_with_backoff():
    b = circuit.breaker("up|m")
    for _ in range(3):
        b.allow(); b.failure(_err())
    first_open = b.open_until - time.monotonic()
    b.open_until = time.monotonic() - 0.01
    assert b.allow()
    b.failure(_err())                          # 探针也失败
    assert b.state == circuit.STATE_OPEN
    second_open = b.open_until - time.monotonic()
    # 退避加倍：10s → 20s（上限 60s）
    assert second_open > first_open * 1.5


def test_keys_are_isolated():
    a = circuit.breaker("up|model-a")
    for _ in range(3):
        a.allow(); a.failure(_err())
    assert a.state == circuit.STATE_OPEN
    other = circuit.breaker("up|model-b")
    assert other.allow()
    assert other.state == circuit.STATE_CLOSED


def test_network_errors_count_but_other_exceptions_do_not():
    b = circuit.breaker("up|m")
    assert circuit.is_upstream_failure(ConnectionError("reset"))
    assert circuit.is_upstream_failure(TimeoutError("timeout"))
    assert circuit.is_upstream_failure(LLMError("x", status=500))
    assert not circuit.is_upstream_failure(LLMError("x", status=400))
    assert not circuit.is_upstream_failure(ValueError("bad json"))
    # ValueError 不该推动计数
    for _ in range(10):
        b.allow(); b.failure(ValueError("bad"))
    assert b.state == circuit.STATE_CLOSED


def test_stats_shape():
    a = circuit.breaker("up|a")
    for _ in range(3):
        a.allow(); a.failure(_err())
    a.allow()                                   # 被拒一次
    st = circuit.stats()
    assert st["open"] == 1
    assert st["rejected"] == 1
    assert st["breakers"][0]["key"] == "up|a"


def test_disabled_switch(monkeypatch):
    monkeypatch.setenv("CIRCUIT_ENABLED", "0")
    b = circuit.breaker("up|m")
    for _ in range(10):
        assert b.allow()
        b.failure(_err())
    assert b.state == circuit.STATE_CLOSED


def test_chat_json_fails_fast_when_open(monkeypatch):
    """集成点：熔断打开时 _chat_json 必须在上游之前失败，且不落 token 账。"""
    import tempfile

    from app.core import config, db, llm

    p = Path(tempfile.mkdtemp(prefix="cb-test-")) / "kb.sqlite"
    db.close_thread_conns()
    db.DB_PATH = p
    db.init_db()
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key", raising=False)

    b = circuit.breaker(f"{config.LLM_BASE_URL}|{config.LLM_MODEL}")
    for _ in range(3):
        b.allow(); b.failure(_err())
    with pytest.raises(circuit.CircuitOpenError):
        llm._chat_json("sys", "user")
    conn = db.get_conn()
    n = conn.execute("SELECT count(*) FROM llm_usage").fetchone()[0]
    conn.close()
    db.close_thread_conns()
    # 被熔断拒绝的请求没有到达上游，不该产生台账行（否则 token 账虚高）
    assert n == 0
