"""成本预算与并发闸门测试。

事故形态不是"一次调用很贵"，而是"某一步在无人盯着时反复重试"。所以闸门必须
在**调用之前**拦，并且要满足四个不变量：
1. 超限拒绝，0 表示不限（不能把默认值做成"谁都别用"）；
2. 刚花掉的钱立刻计入——等缓存过期再算，5 元额度能花到 6 元；
3. 跨天自动归零，不靠定时任务；
4. 同一线程可重入：对话里触发编排时，内层不能把自己锁死。
"""
import threading

import pytest

from app.core import budget, config


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    budget.reset_cache()
    monkeypatch.setattr(budget, "_query", lambda today: 0.0)   # 不碰真库
    yield
    budget.reset_cache()


def test_check_blocks_when_over_limit(monkeypatch):
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET_CNY", 1.0)
    budget.add_spend(0.6)
    budget.check()                       # 还没到
    budget.add_spend(0.5)
    with pytest.raises(budget.BudgetExceeded) as ei:
        budget.check()
    assert "预算已用完" in str(ei.value)
    assert ei.value.spent == 1.1 and ei.value.limit == 1.0


def test_zero_limit_means_unlimited(monkeypatch):
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET_CNY", 0)
    budget.add_spend(999)
    budget.check()                       # 不该抛
    st = budget.status()
    assert st["enabled"] is False and st["blocked"] is False
    assert st["remaining_cny"] is None, "不限时不该编一个剩余额"


def test_spend_is_visible_immediately(monkeypatch):
    """闸门必须看到"刚花掉的"，而不是等 30 秒缓存过期。"""
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET_CNY", 0.01)
    assert budget.spent_today() == 0.0
    budget.add_spend(0.009)
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET_CNY", 0.008)
    with pytest.raises(budget.BudgetExceeded):
        budget.check()


def test_day_rollover_resets(monkeypatch):
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET_CNY", 1.0)
    budget.add_spend(0.9)
    assert budget.spent_today() == 0.9
    # 模拟"昨天的累计残留在进程里"
    budget._day = "2000-01-01"
    assert budget.spent_today() == 0.0, "跨天必须归零"


def test_slot_is_reentrant_in_same_thread(monkeypatch):
    """对话线程里触发编排时，内层拿不到名额会死锁——可重入是正确性不是优化。"""
    monkeypatch.setattr(config, "MAX_CONCURRENT_TASKS", 1)
    budget.reset_cache()
    with budget.slot("outer"):
        with budget.slot("inner"):
            with budget.slot("inner2"):
                pass
    # 出来后名额应已释放
    with budget.slot("after"):
        pass


def test_slot_times_out_when_full(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_TASKS", 1)
    budget.reset_cache()
    held = threading.Event()
    release = threading.Event()

    def worker():
        with budget.slot("holder"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    assert held.wait(3)
    with pytest.raises(budget.Busy):
        with budget.slot("waiter", timeout=0.2):
            pass
    release.set()
    t.join(5)


def test_llm_call_is_refused_before_any_http(monkeypatch):
    """最关键的一条：超预算时不发请求。只"事后记账"等于没拦。"""
    from app.core import llm

    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET_CNY", 0.5)
    budget.add_spend(0.5)

    called = []
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: called.append(1))

    with pytest.raises(budget.BudgetExceeded):
        llm._chat_json("sys", "user")
    assert not called, "预算已用完时不能再发请求"
