"""LLM 上游并发池测试。

没有池时，"编排某步循环重试"会把上游并发打满，故障面是全局的。池要满足的
不变量（事故视角，不是功能视角）：
1. 占满时快速失败并明确报"并发已满"，不是让请求挂着转圈；
2. 失败调用也必须归还额度——漏一次池就永久缩水一格，几次之后"莫名全部超时"；
3. env 调整后池跟着重建（测试与热调都需要）；
4. 排队失败落失败账：没花 token，但"尝试过、没发出去"必须可见。
"""
import threading
import time

import pytest

from app.core import llm


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    """每个用例拿到干净池：旧池的计数不许跨用例泄漏。"""
    monkeypatch.setattr(llm, "_pool", None)
    monkeypatch.setattr(llm, "_pool_size", 0)


def _set_env(monkeypatch, size: int, timeout: float = 0.2) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", str(size))
    monkeypatch.setenv("LLM_ACQUIRE_TIMEOUT", str(timeout))


def test_pool_full_raises_fast_and_retryable(monkeypatch):
    _set_env(monkeypatch, 1)
    pool, size = llm._get_pool()
    assert size == 1
    assert pool.acquire(blocking=False), "先把唯一额度占住"
    t0 = time.perf_counter()
    with pytest.raises(llm.LLMConcurrencyError) as ei:
        llm._acquire_pool()
    assert time.perf_counter() - t0 < 2, "必须按 timeout 快速失败，而不是无限等"
    assert ei.value.retryable is True, "排队失败的语义与 429 一致，走同一条重试分支"
    pool.release()


def test_slot_released_after_exception(monkeypatch):
    _set_env(monkeypatch, 1)
    with pytest.raises(RuntimeError):
        with llm._pool_slot():
            raise RuntimeError("boom")
    pool, _ = llm._get_pool()
    assert pool.acquire(blocking=False), "异常路径必须归还额度，否则池永久缩水"
    pool.release()


def test_slot_released_normally(monkeypatch):
    _set_env(monkeypatch, 1)
    with llm._pool_slot():
        pass
    pool, _ = llm._get_pool()
    assert pool.acquire(blocking=False), "正常路径归还后额度必须回到满"
    pool.release()


def test_size_change_rebuilds_pool(monkeypatch):
    _set_env(monkeypatch, 2)
    llm._get_pool()
    _set_env(monkeypatch, 5)
    _, size = llm._get_pool()
    assert size == 5, "env 调整后要重建，而不是沿用旧池"


def test_concurrent_overflow_fails_not_hangs(monkeypatch):
    _set_env(monkeypatch, 2, timeout=0.3)
    got, full, lock = [], [], threading.Lock()

    def worker():
        try:
            llm._acquire_pool()
            with lock:
                got.append(1)
            time.sleep(0.5)                      # 故意占着额度，逼后面的排队
            llm._get_pool()[0].release()
        except llm.LLMConcurrencyError:
            with lock:
                full.append(1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "并发排队不能挂死线程"
    assert len(got) == 2 and len(full) == 2, "超出池的调用快速失败，不挤占别人"


def test_chat_json_pool_full_records_failure(monkeypatch):
    _set_env(monkeypatch, 1)
    monkeypatch.setattr(llm.config, "LLM_API_KEY", "test")
    monkeypatch.setattr(llm.budget, "check", lambda: None)   # 预算闸门不在本用例范围
    recorded: list = []
    monkeypatch.setattr(llm, "_usage_failed", lambda err, ms: recorded.append(err))
    pool, _ = llm._get_pool()
    assert pool.acquire(blocking=False)
    with pytest.raises(llm.LLMConcurrencyError):
        llm._chat_json("s", "u")
    assert recorded and isinstance(recorded[0], llm.LLMConcurrencyError), \
        "排队失败也落失败账——没花 token，但'尝试过、没发出去'必须可见"
    pool.release()
