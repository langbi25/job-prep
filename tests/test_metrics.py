"""指标聚合测试（零 LLM 调用）。

盯的是**口径**，不是数字好看：指标算错比没有指标更危险——它会让人得出
"系统很稳"或"这个工具最慢"的错误结论，然后按错的方向去优化。
四条规定：
1. 失败行计入分母（llm_usage 从 2026-11-12 起失败也落账）；
2. 失败行的耗时**不进**成功调用的分位数（"等 30 秒才报错"不是"模型变慢"）；
3. 分位数用最近秩：3 个样本的 P95 必须落在真实样本上，不能是插值造出来的数；
4. capped / timeout 不算失败，单独成栏——预算不够和实现有 bug 是两种处置。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.services import metrics_service  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="metrics-test-"))


@pytest.fixture()
def fresh_db():
    p = _TMP / f"kb-{time.time_ns()}.sqlite"
    db.close_thread_conns()
    db.DB_PATH = p
    db.init_db()
    yield p
    db.close_thread_conns()


def _llm(conn, *, scene="console", model="deepseek-chat", status="ok",
         latency=1000, cost=0.01, retried=0):
    conn.execute(
        "INSERT INTO llm_usage (ts, model, scene, prompt_tokens, completion_tokens, "
        "cost_cny, latency_ms, status, retried) VALUES (?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), model, scene, 100, 20,
         cost, latency, status, retried))


def _tool(conn, tool="search_kb", status="ok", error_kind=None, latency=50,
          retried=0, cached=0):
    conn.execute(
        "INSERT INTO tool_calls (ts, tool, source, skill, status, error_kind, "
        "latency_ms, retried, cached, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), tool, "console", "-", status,
         error_kind, latency, retried, cached, time.strftime("%Y-%m-%dT%H:%M:%S")))


def _plan(conn, status="done", cost=0.05, duration=20000, steps=None, intent=None):
    steps = steps or [{"id": "s1", "role": "researcher", "status": "done",
                       "model": "deepseek-chat", "retries": 0}]
    conn.execute(
        "INSERT INTO agent_plans (id, goal, plan_json, status, steps_json, result_json, "
        "tokens_used, cost_cny, duration_ms, mode, resume_count, intent, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"plan-{time.time_ns()}", "目标", "[]", status, json.dumps(steps, ensure_ascii=False),
         "{}", 100, cost, duration, "assist", 0, intent,
         time.strftime("%Y-%m-%dT%H:%M:%S"), time.strftime("%Y-%m-%dT%H:%M:%S")))


def test_llm_error_rate_counts_failed_rows(fresh_db):
    conn = db.get_conn()
    for _ in range(3):
        _llm(conn)
    _llm(conn, status="error", latency=30000)
    conn.commit()

    s = metrics_service.overview(7)["llm"]["summary"]
    assert s["calls"] == 4
    assert s["errors"] == 1
    assert s["error_rate"] == 0.25
    # 失败行的 30000ms 不能把成功调用的分位数带歪
    assert s["p95_ms"] == 1000, s["p95_ms"]


def test_percentile_uses_real_samples(fresh_db):
    conn = db.get_conn()
    for lat in (100, 200, 300):
        _llm(conn, latency=lat)
    conn.commit()

    s = metrics_service.overview(7)["llm"]["summary"]
    assert s["p50_ms"] in (100, 200, 300)
    assert s["p95_ms"] == 300
    assert s["max_ms"] == 300


def test_scene_and_model_split(fresh_db):
    conn = db.get_conn()
    _llm(conn, scene="orchestrate:analyst", model="deepseek-reasoner", cost=0.04)
    _llm(conn, scene="console", cost=0.01)
    conn.commit()

    out = metrics_service.overview(7)["llm"]
    scenes = {d["scene"]: d for d in out["by_scene"]}
    assert scenes["orchestrate:analyst"]["cost_cny"] == pytest.approx(0.04)
    assert scenes["console"]["cost_cny"] == pytest.approx(0.01)
    # 按成本降序：贵的场景排前面，面板第一眼看到的就是花钱的地方
    assert out["by_scene"][0]["scene"] == "orchestrate:analyst"
    models = {d["model"] for d in out["by_model"]}
    assert models == {"deepseek-reasoner", "deepseek-chat"}


def test_tool_metrics_and_error_kinds(fresh_db):
    conn = db.get_conn()
    for _ in range(3):
        _tool(conn, latency=40)
    _tool(conn, status="error", error_kind="invalid_args", latency=10)
    _tool(conn, tool="draft_cards", status="error", error_kind="timeout", latency=9000)
    conn.commit()

    out = metrics_service.overview(7)["tools"]
    assert out["summary"]["calls"] == 5
    assert out["summary"]["error_rate"] == 0.4
    kinds = {d["kind"]: d["n"] for d in out["error_kinds"]}
    assert kinds == {"invalid_args": 1, "timeout": 1}
    by_tool = {d["tool"]: d for d in out["by_tool"]}
    assert by_tool["search_kb"]["calls"] == 4
    assert by_tool["search_kb"]["errors"] == 1
    assert by_tool["draft_cards"]["p95_ms"] == 9000


def test_policy_denials_not_counted_as_failures(fresh_db):
    """assist 模式下模型试图写库被拦住 = 闸门在工作，不是工具坏了。

    实测数据里 6 次 remember 全被拒，如果算进失败率就是 100%——
    这类"假失败"会让新加的工具告警变成误报机器。
    """
    conn = db.get_conn()
    for _ in range(3):
        _tool(conn, tool="remember", status="error", error_kind="policy_denied")
    _tool(conn, tool="search_kb")
    _tool(conn, tool="search_kb", status="error", error_kind="timeout")
    conn.commit()

    out = metrics_service.overview(7)["tools"]
    assert out["summary"]["calls"] == 5
    assert out["summary"]["denied"] == 3
    assert out["summary"]["errors"] == 1
    assert out["summary"]["executed"] == 2          # 分母扣掉未执行的拒绝
    assert out["summary"]["error_rate"] == 0.5
    by_tool = {d["tool"]: d for d in out["by_tool"]}
    assert by_tool["remember"]["error_rate"] == 0.0
    # 但它仍出现在分类里：模型总想写库这件事本身要看得到
    kinds = {d["kind"]: d["n"] for d in out["error_kinds"]}
    assert kinds["policy_denied"] == 3


def test_orchestration_splits_soft_stops_from_failures(fresh_db):
    conn = db.get_conn()
    _plan(conn, "done", cost=0.10)
    _plan(conn, "done", cost=0.20)
    _plan(conn, "capped", cost=0.50)
    _plan(conn, "timeout", cost=0.30)
    _plan(conn, "failed", cost=0.02)
    conn.commit()

    out = metrics_service.overview(7)["orchestration"]
    assert out["plans"] == 5
    assert out["by_status"] == {"done": 2, "capped": 1, "timeout": 1, "failed": 1}
    assert out["success_rate"] == 0.4
    # 只有 failed 算"实现有问题"；capped/timeout 是预算与时间没给够
    assert out["hard_failure_rate"] == 0.2
    assert out["soft_stop_rate"] == 0.4
    assert out["avg_cost_cny"] == pytest.approx(0.224)


def test_orchestration_step_breakdown(fresh_db):
    conn = db.get_conn()
    _plan(conn, steps=[
        {"id": "s1", "role": "researcher", "status": "done", "model": "deepseek-chat"},
        {"id": "s2", "role": "analyst", "status": "failed", "model": "deepseek-reasoner",
         "retries": 1},
    ])
    conn.commit()

    out = metrics_service.overview(7)["orchestration"]
    pairs = {(d["role"], d["status"]): d["n"] for d in out["step_status"]}
    assert pairs[("analyst", "failed")] == 1
    assert out["retried_steps"] == 1
    models = {d["model"]: d["n"] for d in out["step_models"]}
    assert models == {"deepseek-chat": 1, "deepseek-reasoner": 1}


def test_subagents_merge_three_sources(fresh_db):
    """子 agent 视图三源合一：LLM 台账 + 工具台账 + 步骤快照按角色对上。

    对不上账的测试价值最大：role 在 llm_usage 里叫 orchestrate:analyst、
    在 tool_calls 里叫 agent:analyst、在 steps_json 里叫 analyst——
    三个打标点任何一个改了前缀，这里立刻红。
    """
    conn = db.get_conn()
    # analyst：LLM 2 次（1 失败）+ 1 次工具被拒 + 2 步（1 done 1 failed 带 retry）
    _llm(conn, scene="orchestrate:analyst", model="deepseek-reasoner", cost=0.04)
    _llm(conn, scene="orchestrate:analyst", model="deepseek-reasoner", cost=0.02,
         status="error", latency=3000)
    conn.execute(
        "INSERT INTO tool_calls (ts, tool, source, skill, status, error_kind, "
        "latency_ms, retried, cached, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), "search_kb", "agent:analyst", "-", "error",
         "policy_denied", 5, 0, 0, time.strftime("%Y-%m-%dT%H:%M:%S")))
    _plan(conn, steps=[
        {"id": "s1", "role": "analyst", "status": "done", "model": "deepseek-reasoner",
         "cost_cny": 0.04, "duration_ms": 4000},
        {"id": "s2", "role": "analyst", "status": "failed", "model": "deepseek-reasoner",
         "retries": 1, "cost_cny": 0.02, "duration_ms": 6000},
    ])
    # 主控：plan + synthesize 两行，归 main，不混进角色
    _llm(conn, scene="orchestrate:plan", cost=0.01)
    _llm(conn, scene="orchestrate:synthesize", cost=0.03)
    conn.commit()

    roles = {d["role"]: d for d in metrics_service.overview(7)["subagents"]["roles"]}
    a = roles["analyst"]
    assert a["llm_calls"] == 2 and a["llm_errors"] == 1
    assert a["cost_cny"] == pytest.approx(0.06)
    assert a["tool_calls"] == 1 and a["tool_denied"] == 1 and a["tool_errors"] == 0
    assert a["steps"] == 2 and a["steps_done"] == 1 and a["steps_failed"] == 1
    assert a["retries"] == 1
    assert a["step_cost_cny"] == pytest.approx(0.06)
    assert a["avg_step_ms"] == 5000
    assert a["main_model"] == "deepseek-reasoner"
    assert a["top_tools"] == [{"tool": "search_kb", "n": 1}]
    m = roles["main"]
    assert m["llm_calls"] == 2
    assert m["cost_cny"] == pytest.approx(0.04)
    assert m["tool_calls"] == 0 and m["steps"] == 0
    # 没跑过的角色不出空行：researcher/writer/critic 在窗口里没数据
    assert set(roles) == {"analyst", "main"}


def test_subagents_ignores_unknown_scene_suffix(fresh_db):
    """未知场景后缀不猜归属——宁可少一行也不能把费用串到角色上。"""
    conn = db.get_conn()
    _llm(conn, scene="orchestrate:typo_role", cost=0.5)
    _llm(conn, scene="console", cost=0.01)          # 非编排场景不进子 agent 视图
    conn.commit()
    assert metrics_service.overview(7)["subagents"]["roles"] == []


def test_intents_aggregate_and_keep_unknown_separate(fresh_db):
    """按意图聚合 + unknown 单列。

    早期计划没有意图列（NULL）。归 unknown 而不是回填 other 是刻意的：
    "没分过类"和"分出来是其他"是两回事，混在一起会把兜底类占比抬高。
    """
    conn = db.get_conn()
    _plan(conn, "done", cost=0.10, duration=10000, intent="analysis")
    _plan(conn, "done", cost=0.30, duration=30000, intent="analysis")
    _plan(conn, "failed", cost=0.02, duration=5000, intent="status_query")
    _plan(conn, "capped", cost=0.50, duration=40000)             # 老数据，无意图
    _plan(conn, "done", cost=0.01, duration=2000, intent="bogus")  # 幻觉类别按原样保留
    conn.commit()

    out = metrics_service.overview(7)["intents"]
    items = {d["intent"]: d for d in out["items"]}
    an = items["analysis"]
    assert an["label"] == "分析诊断"
    assert an["plans"] == 2 and an["done"] == 2
    assert an["success_rate"] == 1.0
    assert an["avg_cost_cny"] == pytest.approx(0.20)
    assert an["avg_duration_ms"] == 20000
    sq = items["status_query"]
    assert sq["hard_failure_rate"] == 1.0
    # NULL ≠ other：unknown 独立成行，不占用"其他"的口径
    assert items["unknown"]["plans"] == 1
    assert out["unclassified"] == 1
    # 幻觉意图原样透传（打上原始标签），label 回退为原始串
    assert items["bogus"]["plans"] == 1 and items["bogus"]["label"] == "bogus"


def test_empty_window_is_zero_not_crash(fresh_db):
    out = metrics_service.overview(7)
    assert out["llm"]["summary"]["calls"] == 0
    assert out["llm"]["summary"]["p95_ms"] is None      # 没有样本就不该编一个"很快"
    assert out["tools"]["summary"]["calls"] == 0
    assert out["orchestration"]["plans"] == 0
    assert out["subagents"]["roles"] == []
    assert out["intents"]["items"] == []
    assert out["budget"]["today_cost_cny"] == 0.0
