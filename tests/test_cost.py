"""成本核算测试（零 LLM 调用）。

覆盖：单价表与估算标记、缓存命中折扣、脏数据兜底（cached > prompt）、
台账写入与聚合（按天/场景/模型/会话）、collect 收集器、
agent_plans.cost_cny 存量迁移、编排 _save 的费用落库。

跑法：
    pytest tests/test_cost.py -q
    python tests/test_cost.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db, pricing  # noqa: E402
from app.agents import orchestrator  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="cost-test-"))


def _fresh_db() -> sqlite3.Connection:
    p = _TMP / "kb.sqlite"
    for _ in range(5):
        try:
            if p.exists():
                p.unlink()
            break
        except PermissionError:
            import time
            time.sleep(0.05)
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def test_price_known_model_exact():
    p = pricing.price_for("deepseek-chat")
    assert p["estimated"] is False
    assert p["input"] == 2.0 and p["cached_input"] == 0.5 and p["output"] == 8.0


def test_price_unknown_model_estimated():
    """未登记模型按默认价估算并打标——宁可标估算也不返回 0（0 元比贵了更危险）。"""
    p = pricing.price_for("some-future-model")
    assert p["estimated"] is True
    assert p["input"] > 0 and p["output"] > 0


def test_cost_math_with_cache_discount():
    # 1000 输入（200 命中缓存）+ 500 输出
    # = (800*2.0 + 200*0.5 + 500*8.0) / 1e6 = (1600+100+4000)/1e6 = 0.0057
    c = pricing.cost_cny("deepseek-chat", 1000, 500, cached_tokens=200)
    assert abs(c - 0.0057) < 1e-9


def test_cost_dirty_cached_clamped():
    """cached_tokens 超过 prompt_tokens 要截断，负费用会悄悄拉低总账。"""
    full = pricing.cost_cny("deepseek-chat", 1000, 0, cached_tokens=1000)
    dirty = pricing.cost_cny("deepseek-chat", 1000, 0, cached_tokens=9999)
    assert full == dirty == 0.5 * 1000 / 1e6
    assert pricing.cost_cny("deepseek-chat", 1000, 0, cached_tokens=-5) \
        == 2.0 * 1000 / 1e6


def test_record_usage_writes_ledger():
    conn = _fresh_db()
    conn.close()
    row = pricing.record_usage("deepseek-chat", prompt_tokens=1000,
                               completion_tokens=200, scene="test_scene",
                               session_id="s-1", turn=3)
    assert row["cost_cny"] > 0 and row["price_estimated"] == 0
    c = pricing.session_turn_cost("s-1")
    assert c["calls"] == 1
    assert c["prompt_tokens"] == 1000 and c["completion_tokens"] == 200
    assert abs(c["cost_cny"] - row["cost_cny"]) < 1e-9


def test_collect_accumulates_scope_only():
    """collect 只收块内主线程调用；块外的不算，聚合数字对得上。"""
    conn = _fresh_db()
    conn.close()
    pricing.record_usage("deepseek-chat", prompt_tokens=100, completion_tokens=10)
    with pricing.collect() as acc:
        pricing.record_usage("deepseek-chat", prompt_tokens=1000, completion_tokens=100)
        pricing.record_usage("deepseek-reasoner", prompt_tokens=500, completion_tokens=50)
    pricing.record_usage("deepseek-chat", prompt_tokens=100, completion_tokens=10)
    out = pricing.accumulate(acc)
    assert out["calls"] == 2
    assert out["prompt_tokens"] == 1500 and out["completion_tokens"] == 150
    assert out["cost_cny"] > 0


def test_summary_aggregates_by_day_scene_model():
    conn = _fresh_db()
    conn.close()
    for scene in ("a", "a", "b"):
        pricing.record_usage("deepseek-chat", prompt_tokens=1000,
                             completion_tokens=100, scene=scene)
    s = pricing.summary(days=14)
    assert s["summary"]["calls"] == 3
    assert {r["scene"]: r["calls"] for r in s["by_scene"]} == {"a": 2, "b": 1}
    assert len(s["by_day"]) == 1
    assert s["by_model"][0]["model"] == "deepseek-chat"
    # 聚合守恒：各场景费用加总 = 总费用
    assert abs(sum(r["cost_cny"] for r in s["by_scene"]) - s["summary"]["cost_cny"]) < 1e-9


def test_agent_plans_cost_column_migration():
    """存量库没有 cost_cny 列时，_ensure_columns 要补上。"""
    conn = _fresh_db()
    conn.execute("CREATE TABLE agent_plans_bak AS SELECT * FROM agent_plans")
    conn.execute("DROP TABLE agent_plans")
    # 模拟老库：不带 cost_cny 列的旧表结构
    conn.execute(
        "CREATE TABLE agent_plans (id TEXT PRIMARY KEY, session_id TEXT, goal TEXT NOT NULL,"
        " plan_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',"
        " steps_json TEXT DEFAULT '[]', result_json TEXT,"
        " tokens_used INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    db._ensure_columns(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_plans)")}
    assert "cost_cny" in cols
    conn.close()


def test_orchestrator_save_persists_cost():
    """_save 要把 plan 级费用写进 agent_plans，recent_plans 能读回来。"""
    conn = _fresh_db()
    conn.close()
    steps = [orchestrator.PlanStep(id="s1", role="researcher", goal="g",
                                   tokens=100, cost=0.003),
             orchestrator.PlanStep(id="s2", role="writer", goal="g2",
                                   status="done", tokens=50, cost=0.002)]
    orchestrator._save("plan-cost-1", session_id=None, goal="费用测试",
                       steps=steps, status="done", result={"answer": "x"},
                       tokens=150, cost=0.005, duration_ms=123)
    rows = orchestrator.recent_plans(limit=5)
    hit = next(r for r in rows if r["id"] == "plan-cost-1")
    assert abs(hit["cost_cny"] - 0.005) < 1e-9
    d = orchestrator.plan_detail("plan-cost-1")
    step_costs = {s["id"]: s["cost_cny"] for s in d["steps"]}
    assert abs(step_costs["s1"] - 0.003) < 1e-9


def test_llm_usage_schema_columns():
    """台账表字段齐全（索引建在 substr(ts,1,10) 上，聚合 SQL 依赖这个形状）。"""
    conn = _fresh_db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_usage)")}
    assert {"ts", "model", "scene", "session_id", "turn", "prompt_tokens",
            "completion_tokens", "cached_tokens", "cost_cny",
            "price_estimated", "latency_ms"} <= cols
    conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
