"""编排的边界与韧性（零真实 LLM 调用）。

这组测试盯的是四件"平时看不见、出事才发现没有"的事：
1. 角色 → 模型路由：默认必须回落全局模型，配错不能把编排带崩；
2. 成本闸门：超限后**不再派发**新步骤，但已完成的结果必须留着；
3. 截止时间：整条计划的墙钟上限，不是每步各算各的；
4. 步骤重试：只有 retryable 的失败才重试，且只重试一次、第二次收窄上下文。

为什么这些必须单独测：它们都在 `_execute` 的调度循环里，用真实 LLM 跑
一次要几十秒且金额不可控；而错误形态（没有闸门 = 无限花钱）恰恰是最贵的。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents import orchestrator as orch  # noqa: E402
from app.core import config, db, llm  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="orch-bounds-"))


@pytest.fixture()
def fresh_db():
    p = _TMP / f"kb-{time.time_ns()}.sqlite"
    db.close_thread_conns()
    db.DB_PATH = p
    db.init_db()
    yield
    db.close_thread_conns()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ORCH_ROLE_MODEL", "ORCH_DEADLINE_S", "ORCH_MAX_COST_CNY"):
        monkeypatch.delenv(k, raising=False)
    yield


# ---------------------------------------------------------------- 角色 → 模型

def test_role_model_defaults_to_global(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-chat")
    assert orch.role_model("researcher") == "deepseek-chat"
    assert orch.role_model("analyst") == "deepseek-chat"


def test_role_model_reads_env(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("ORCH_ROLE_MODEL", '{"analyst": "deepseek-reasoner"}')
    assert orch.role_model("analyst") == "deepseek-reasoner"
    assert orch.role_model("writer") == "deepseek-chat"   # 没配的回落全局


def test_role_model_survives_bad_json(monkeypatch):
    """配错不能让编排起不来——退回全局默认，但角色名拼错要能看见。"""
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("ORCH_ROLE_MODEL", "{not json")
    assert orch.role_model("analyst") == "deepseek-chat"
    assert orch._route_warnings() == []

    monkeypatch.setenv("ORCH_ROLE_MODEL", '{"analyse": "deepseek-reasoner"}')
    warns = orch._route_warnings()
    assert any("analyse" in w for w in warns), warns


# ---------------------------------------------------------------- 闸门

def _patch_run(monkeypatch, fn):
    monkeypatch.setattr(orch, "run_step", fn)


def test_cost_gate_stops_dispatch_but_keeps_results(fresh_db, monkeypatch):
    monkeypatch.setenv("ORCH_MAX_COST_CNY", "0.1")
    ran: list[str] = []

    def fake(step, *, goal, upstream, mode="assist", session_id=None,
             budget_tokens=3000, shrink=False):
        ran.append(step.id)
        step.status = "done"
        step.output = f"out-{step.id}"
        step.cost = 0.2                      # 一步就超限
        return step

    _patch_run(monkeypatch, fake)
    # 串成两批：闸门是**派发粒度**的，同一批并行的步骤已经一起出发，
    # 拦不住其中一个——这也是为什么闸门只承诺"不再派发新的"。
    steps = [orch.PlanStep("s1", "writer", "甲"),
             orch.PlanStep("s2", "writer", "乙", ["s1"])]
    board = orch._execute(steps, goal="g", mode="assist", session_id=None,
                          budget_tokens=3000, max_cost=0.1)

    assert ran == ["s1"], "超限后不应再派发第二步"
    assert steps[0].status == "done" and board.has("s1")
    assert steps[1].status == "skipped"
    assert "成本闸门" in (steps[1].error or "")
    assert board.stop == "cost"
    assert orch._final_status(steps, board.stop) == "capped"


def test_deadline_stops_before_first_batch(fresh_db, monkeypatch):
    monkeypatch.setenv("ORCH_DEADLINE_S", "1")
    ran: list[str] = []

    def fake(step, **kw):
        ran.append(step.id)
        step.status = "done"
        return step

    _patch_run(monkeypatch, fake)
    steps = [orch.PlanStep("s1", "writer", "甲")]
    board = orch._execute(steps, goal="g", mode="assist", session_id=None,
                          budget_tokens=3000, deadline=time.time() - 1)

    assert ran == []
    assert steps[0].status == "skipped"
    assert board.stop == "deadline"
    assert orch._final_status(steps, board.stop) == "timeout"


# ---------------------------------------------------------------- 步骤重试

def test_retryable_failure_retries_once_with_shrink(fresh_db, monkeypatch):
    calls: list[dict] = []

    def fake(step, *, goal, upstream, mode="assist", session_id=None,
             budget_tokens=3000, shrink=False):
        calls.append({"shrink": shrink, "upstream": upstream})
        if len(calls) == 1:
            step.status = "failed"
            step.retryable = True
            step.error = "LLMConcurrencyError: 并发排队超时"
        else:
            step.status = "done"
            step.output = "重试成功"
        return step

    _patch_run(monkeypatch, fake)
    st = orch.PlanStep("s1", "writer", "甲")
    out = orch._run_with_retry(st, goal="g", upstream="上游结论" * 100,
                               mode="assist", session_id=None, budget_tokens=3000)

    assert out.status == "done"
    assert out.retries == 1
    assert [c["shrink"] for c in calls] == [False, True]
    # 第二次的上游被砍半：原样重来往往还是同样的超长/超时
    assert len(calls[1]["upstream"]) < len(calls[0]["upstream"])


def test_non_retryable_failure_does_not_retry(fresh_db, monkeypatch):
    calls: list[int] = []

    def fake(step, **kw):
        calls.append(1)
        step.status = "failed"
        step.retryable = False
        step.error = "LLMError: 400 参数不合法"
        return step

    _patch_run(monkeypatch, fake)
    st = orch.PlanStep("s1", "writer", "甲")
    out = orch._run_with_retry(st, goal="g", upstream="", mode="assist",
                               session_id=None, budget_tokens=3000)

    assert out.status == "failed"
    assert len(calls) == 1, "不可重试的错误重试只是把一次失败变两次"
    assert out.retries == 0


def test_retry_only_once(fresh_db, monkeypatch):
    calls: list[int] = []

    def fake(step, **kw):
        calls.append(1)
        step.status = "failed"
        step.retryable = True
        step.error = "超时"
        return step

    _patch_run(monkeypatch, fake)
    st = orch.PlanStep("s1", "writer", "甲")
    out = orch._run_with_retry(st, goal="g", upstream="", mode="assist",
                               session_id=None, budget_tokens=3000)
    assert out.status == "failed"
    assert len(calls) == 2      # 首次 + 一次重试，不会无限循环
    assert out.retries == 1


def test_halted_plan_skips_synthesis(fresh_db, monkeypatch):
    """闸门中止后不许再调合成/自检——那两次也是 LLM 调用。

    实测教训：第一版只拦步骤，capped 之后照样跑 synthesize+critique，
    比上限多花 4 倍，"成本闸门"只关了一半。
    """
    monkeypatch.setenv("ORCH_DEADLINE_S", "0")
    monkeypatch.setattr(orch, "plan", lambda *a, **k: {
        "steps": [orch.PlanStep("s1", "writer", "甲")], "warnings": []})
    monkeypatch.setattr(orch, "run_step",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应执行步骤")))
    called: list[int] = []
    monkeypatch.setattr(orch, "synthesize", lambda *a, **k: called.append(1) or "不该被调用")
    monkeypatch.setattr(orch, "critique", lambda *a, **k: called.append(2) or {"ok": True})

    res = orch.orchestrate("目标", mode="assist")

    assert res["status"] == "timeout"
    assert res["stopped_by"] == "deadline"
    assert called == []
    assert "截止时间到" in res["answer"]
    assert res["steps"][0]["status"] == "skipped"


# ---------------------------------------------------------------- 模型落到台账

class _FakeResp:
    status_code = 200

    def __init__(self, body: dict):
        self._body = body

    def json(self):
        return self._body


def test_chat_json_records_actual_model(fresh_db, monkeypatch):
    """路由之后台账必须按**实际模型**记账：单价差一倍，记成默认模型就算错钱。"""
    monkeypatch.setenv("LLM_STREAM", "0")
    seen: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["model"] = json["model"]
        return _FakeResp({
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        })

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.chat_parsed("sys", "user", model="deepseek-reasoner")

    assert seen["model"] == "deepseek-reasoner"
    assert out["_usage"]["model"] == "deepseek-reasoner"
    rows = db.get_conn().execute(
        "SELECT model, cost_cny FROM llm_usage ORDER BY id DESC LIMIT 1").fetchone()
    assert rows["model"] == "deepseek-reasoner"
    # reasoner 输入 4 元/1M、输出 16 元/1M：11*4/1e6 + 7*16/1e6 ≈ 0.000156
    assert abs(rows["cost_cny"] - 0.000156) < 1e-6, rows["cost_cny"]
