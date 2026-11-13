"""编排断点续跑测试（零 LLM：run_step / synthesize / critique 全部打桩）。

要验的四件事：
1. 已完成的步骤**真的不再执行**，且它的结论**真的回填进了下游的 upstream**；
2. 失败/跳过的步骤会被重跑，不保持原状；
3. 费用与 token 是累加不是覆盖；
4. 同一 plan 不会被并发续跑两遍。

跑法：
    pytest tests/test_resume.py -q
    python tests/test_resume.py
"""
import contextlib
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents import orchestrator as o  # noqa: E402
from app.core import db  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="resume-test-"))
_calls: list[dict] = []


def _fresh_db() -> sqlite3.Connection:
    p = _TMP / "kb.sqlite"
    for _ in range(8):
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


def _fake_run_step(step, *, goal, upstream, mode="assist", session_id=None,
                   budget_tokens=3000):
    _calls.append({"id": step.id, "upstream": upstream, "mode": mode})
    step.output = f"结论-{step.id}"
    step.status = "done"
    step.tokens = 100
    step.cost = 0.001
    step.tools = ["search_kb"]
    return step


@contextlib.contextmanager
def _stubbed():
    _calls.clear()
    orig = (o.run_step, o.synthesize, o.critique)
    o.run_step = _fake_run_step
    o.synthesize = lambda goal, steps, mode="assist": "汇总：" + "|".join(s.output for s in steps)
    o.critique = lambda goal, answer, steps: {"ok": True, "issues": [], "retry_steps": []}
    try:
        yield
    finally:
        o.run_step, o.synthesize, o.critique = orig


def _make_plan(plan_id="plan-test-1", *, status="failed", cost=0.0, tokens=0,
               mode="assist", resume_count=0, steps=None):
    steps = steps or [
        o.PlanStep(id="s1", role="researcher", goal="查数据", status="done",
                   output="s1 的旧结论", tokens=200, cost=0.002),
        o.PlanStep(id="s2", role="analyst", goal="做判断", depends_on=["s1"],
                   status="failed", error="LLM 超时"),
    ]
    o._save(plan_id, session_id="cs-x", goal="判断最该补什么", steps=steps,
            status=status, result={"answer": "旧答案"}, tokens=tokens, cost=cost,
            duration_ms=500, mode=mode, resume_count=resume_count)
    return steps


# ---------- 重建 ----------

def test_steps_roundtrip():
    _fresh_db()
    _make_plan()
    row = o._load_row("plan-test-1")
    steps = o._steps_from_row(row)
    assert [s.id for s in steps] == ["s1", "s2"]
    assert steps[0].status == "done" and steps[0].output == "s1 的旧结论"
    assert steps[0].tokens == 200 and abs(steps[0].cost - 0.002) < 1e-9
    assert steps[1].depends_on == ["s1"] and steps[1].error == "LLM 超时"


def test_missing_plan_raises():
    _fresh_db()
    try:
        o.resume("plan-nope")
    except ValueError as e:
        assert "不存在" in str(e)
    else:
        raise AssertionError("不存在的计划应该报错")


def test_done_plan_rejected():
    _fresh_db()
    _make_plan("plan-done", status="done", steps=[
        o.PlanStep(id="s1", role="researcher", goal="g", status="done", output="ok")])
    try:
        o.resume("plan-done")
    except ValueError as e:
        assert "已完成" in str(e)
    else:
        raise AssertionError("已完成的计划不应续跑")


# ---------- 核心：只重跑未完成的，且回填上游结论 ----------

def test_resume_skips_done_and_backfills_upstream():
    _fresh_db()
    _make_plan()
    with _stubbed():
        res = o.resume("plan-test-1")

    assert [c["id"] for c in _calls] == ["s2"], f"只该重跑 s2，实际 {_calls}"
    # 这一条是整个功能的意义所在：s2 必须真的拿到 s1 的旧结论
    assert "s1 的旧结论" in _calls[0]["upstream"], f"上游结论没回填：{_calls[0]['upstream']}"
    assert res["status"] == "done"
    assert res["resumed"] is True and res["resume_count"] == 1
    assert res["skipped_done_steps"] == ["s1"]
    assert res["resumed_steps"] == ["s2"]


def test_resume_reruns_skipped_steps():
    """上次因上游失败被跳过的步骤，这次上游好了就必须能跑。"""
    _fresh_db()
    _make_plan("plan-skip", steps=[
        o.PlanStep(id="s1", role="researcher", goal="查", status="failed", error="boom"),
        o.PlanStep(id="s2", role="analyst", goal="判", depends_on=["s1"], status="skipped"),
    ])
    with _stubbed():
        res = o.resume("plan-skip")
    assert sorted(c["id"] for c in _calls) == ["s1", "s2"], f"两条都该重跑，实际 {_calls}"
    assert res["status"] == "done"


def test_resume_keeps_untouched_then_continues_downstream():
    """三个步骤、中间一个坏：只重跑坏的，最后一步仍然要跑（因为它的上游变了）。"""
    _fresh_db()
    _make_plan("plan-mid", steps=[
        o.PlanStep(id="s1", role="researcher", goal="查", status="done", output="s1 结论"),
        o.PlanStep(id="s2", role="analyst", goal="判", depends_on=["s1"], status="failed"),
        o.PlanStep(id="s3", role="writer", goal="写", depends_on=["s2"], status="skipped"),
    ])
    with _stubbed():
        res = o.resume("plan-mid")
    assert sorted(c["id"] for c in _calls) == ["s2", "s3"]
    s3 = [c for c in _calls if c["id"] == "s3"][0]
    assert "结论-s2" in s3["upstream"]
    assert res["status"] == "done"


# ---------- 费用 ----------

def test_cost_and_tokens_accumulate():
    _fresh_db()
    _make_plan(cost=0.5, tokens=5000)
    with _stubbed():
        res = o.resume("plan-test-1")
    # 旧 0.5 + s2 新增 0.001（s1 是历史值，不能重复计）
    assert res["cost_cny"] > 0.5, f"费用应累加，实际 {res['cost_cny']}"
    assert res["cost_cny"] < 0.52, f"不该把已完成步骤的费用重复计一次：{res['cost_cny']}"
    # 库里 tokens_used=5000 已经包含了 s1 的 200（真实场景下它就是 sum(s.tokens)），
    # 所以续跑只该加上 s2 新花的 100。这里是 5100 而不是 5200——
    # 若写成 5200 说明把已完成的步骤又计了一遍。
    assert res["tokens"] == 5100, f"只该加增量：{res['tokens']}"


def test_resume_count_increments():
    _fresh_db()
    _make_plan(resume_count=2)
    with _stubbed():
        res = o.resume("plan-test-1")
    assert res["resume_count"] == 3
    row = o._load_row("plan-test-1")
    assert int(row["resume_count"]) == 3


def test_mode_preserved():
    """续跑必须沿用原计划的权限：assist 的计划不能因为续跑拿到写权限。"""
    _fresh_db()
    _make_plan(mode="assist")
    with _stubbed():
        o.resume("plan-test-1")
    assert all(c["mode"] == "assist" for c in _calls), _calls


def test_running_status_can_resume():
    """库里出现 running 只可能是进程中途挂了——那正是最该续跑的情况。"""
    _fresh_db()
    _make_plan("plan-zombie", status="running")
    with _stubbed():
        res = o.resume("plan-zombie")
    assert res["previous_status"] == "running"
    assert res["status"] == "done"


# ---------- 并发 ----------

def test_concurrent_resume_blocked():
    _fresh_db()
    _make_plan()
    lock = o._plan_lock("plan-test-1")
    assert lock.acquire(blocking=False)
    try:
        try:
            o.resume("plan-test-1")
        except ValueError as e:
            assert "续跑中" in str(e)
        else:
            raise AssertionError("持锁时第二次续跑必须被拒")
    finally:
        lock.release()
    # 释放后可以正常续跑
    with _stubbed():
        assert o.resume("plan-test-1")["status"] == "done"


def test_plan_detail_exposes_resume_fields():
    _fresh_db()
    _make_plan()
    with _stubbed():
        o.resume("plan-test-1")
    d = o.plan_detail("plan-test-1")
    assert d["mode"] == "assist" and d["resume_count"] == 1
    assert isinstance(d["steps"], list) and isinstance(d["result"], dict)
    assert d["result"]["resumed"] is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
