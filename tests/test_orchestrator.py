"""多 Agent 编排测试（零 LLM 调用）。

编排里真正容易错的不是"调模型"，而是：plan 校验、DAG 拓扑、并行度、
上游结论的可见范围。这些用假 run_step 就能测干净，不需要真调 LLM。
跑法：pytest tests/test_orchestrator.py -q
"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents import orchestrator as orch  # noqa: E402
from app.core import db  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="orch-test-"))


def _fresh_db() -> None:
    p = _TMP / "kb.sqlite"
    for suffix in ("", "-wal", "-journal"):
        f = Path(str(p) + suffix)
        if f.exists():
            for _ in range(5):
                try:
                    f.unlink()
                    break
                except PermissionError:
                    time.sleep(0.05)
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def test_valid_plan_rejects_bad_steps():
    steps, errs = orch._valid_plan({"steps": [
        {"id": "s1", "role": "researcher", "goal": "查薄弱点"},
        {"id": "s1", "role": "writer", "goal": "重复 id"},
        {"id": "s2", "role": "不存在的角色", "goal": "写答案"},
        {"id": "s3", "role": "writer", "goal": ""},
    ]})
    ids = [s.id for s in steps]
    assert ids == ["s1", "s2"], ids
    assert steps[1].role == "researcher"          # 未知角色降级
    assert any("重复" in e for e in errs)
    assert any("没有目标" in e for e in errs)


def test_valid_plan_caps_steps():
    raw = {"steps": [{"id": f"s{i}", "role": "writer", "goal": f"g{i}"}
                     for i in range(orch.MAX_STEPS + 3)]}
    steps, errs = orch._valid_plan(raw)
    assert len(steps) == orch.MAX_STEPS
    assert any("超过上限" in e for e in errs)


def test_dag_rejects_cycle_and_missing_dep():
    a = orch.PlanStep("a", "writer", "A", ["b"])
    b = orch.PlanStep("b", "writer", "B", ["a"])
    try:
        orch._check_dag([a, b])
        raise AssertionError("环依赖应该抛错")
    except ValueError as e:
        assert "循环依赖" in str(e)

    c = orch.PlanStep("c", "writer", "C", ["nope", "c"])
    errs = orch._check_dag([c])
    assert c.depends_on == []                      # 不存在的依赖与自依赖都被清掉
    assert len(errs) == 2


def test_blackboard_visibility():
    board = orch.Blackboard()
    board.put("s1", "结论一")
    assert "结论一" in board.view(["s1"])
    assert "没有上游" in board.view([])
    assert "没有产出" in board.view(["s9"])


def _fake_run_factory(sink: list, delay: float = 0.0):
    def fake(step, *, goal, upstream, mode="assist", session_id=None, budget_tokens=3000):
        if delay:
            time.sleep(delay)
        sink.append({"id": step.id, "upstream": upstream})
        step.status = "done"
        step.output = f"out-{step.id}"
        return step
    return fake


def test_execute_topology_and_upstream_isolation():
    _fresh_db()
    orig = orch.run_step
    sink: list = []
    orch.run_step = _fake_run_factory(sink)
    try:
        steps = [
            orch.PlanStep("s1", "researcher", "查数据"),
            orch.PlanStep("s2", "researcher", "查另一份"),
            orch.PlanStep("s3", "analyst", "合并判断", ["s1", "s2"]),
        ]
        orch._execute(steps, goal="g", mode="assist", session_id=None, budget_tokens=9000)
    finally:
        orch.run_step = orig

    by_id = {x["id"]: x for x in sink}
    # 第三步只能看见它声明的依赖，看不见全局所有产出
    assert "out-s1" in by_id["s3"]["upstream"] and "out-s2" in by_id["s3"]["upstream"]
    assert by_id["s1"]["upstream"].startswith("（这是第一步")
    assert all(s.status == "done" for s in steps)


def test_execute_runs_independent_steps_in_parallel():
    _fresh_db()
    orig = orch.run_step
    orch.run_step = _fake_run_factory([], delay=0.15)
    try:
        steps = [orch.PlanStep(f"s{i}", "researcher", f"g{i}") for i in range(3)]
        t0 = time.perf_counter()
        orch._execute(steps, goal="g", mode="assist", session_id=None, budget_tokens=9000)
        elapsed = time.perf_counter() - t0
    finally:
        orch.run_step = orig
    assert elapsed < 0.35, f"三个无依赖步骤应并行，实际耗时 {elapsed:.2f}s"


def test_execute_skips_when_upstream_failed():
    _fresh_db()
    orig = orch.run_step

    def fake(step, **kw):
        if step.id == "s1":
            step.status = "failed"
            step.error = "boom"
        else:
            step.status = "done"
            step.output = "ok"
        return step

    orch.run_step = fake
    try:
        steps = [orch.PlanStep("s1", "researcher", "g1"),
                 orch.PlanStep("s2", "writer", "g2", ["s1"])]
        orch._execute(steps, goal="g", mode="assist", session_id=None, budget_tokens=6000)
    finally:
        orch.run_step = orig
    assert steps[0].status == "failed"
    assert steps[1].status == "skipped"
    assert "上游步骤失败" in steps[1].error


def test_save_and_read_plan():
    _fresh_db()
    steps = [orch.PlanStep("s1", "writer", "写答案", status="done", output="答案")]
    orch._save("plan-test-1", session_id="cs-x", goal="目标", steps=steps, status="done",
               result={"answer": "答案"}, tokens=123, cost=0.0042, duration_ms=456)
    rows = orch.recent_plans(5)
    assert rows and rows[0]["id"] == "plan-test-1" and rows[0]["tokens_used"] == 123
    assert abs(rows[0]["cost_cny"] - 0.0042) < 1e-9
    d = orch.plan_detail("plan-test-1")
    assert d["status"] == "done" and d["plan"][0]["id"] == "s1"
    assert d["result"]["answer"] == "答案"
    assert orch.plan_detail("nope") is None


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
