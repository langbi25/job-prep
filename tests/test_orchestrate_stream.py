"""编排进度事件测试。

编排一次 20~35 秒、最多 4 步并行，用户点完按钮只能看转圈，分不清"在跑"和"卡死"。
事件协议要保证三件事，各对应一个测试：

1. 每个 step_start 都有配对的 step_end——否则前端会永远停在"进行中"；
2. 被跳过的步骤也要发 step_end（status=skipped），不能静默消失；
3. 发事件失败绝不能让 DAG 失败：进度是观测，不是业务。
"""
from app.agents import orchestrator as orch


def _steps() -> list[orch.PlanStep]:
    return [orch.PlanStep(id="s1", role="researcher", goal="查数据"),
            orch.PlanStep(id="s2", role="analyst", goal="分析", depends_on=["s1"])]


def test_each_start_has_matching_end(monkeypatch):
    steps = _steps()

    def fake_run_step(st, **kw):
        st.status = "done"
        st.output = f"{st.id} 的结论"
        st.tokens = 100
        return st

    monkeypatch.setattr(orch, "run_step", fake_run_step)
    evs: list[dict] = []
    orch._execute(steps, goal="g", mode="assist", session_id=None,
                  budget_tokens=4000, emit=evs.append)

    starts = [e["id"] for e in evs if e["type"] == "step_start"]
    ends = [e["id"] for e in evs if e["type"] == "step_end"]
    assert starts == ["s1", "s2"], "串行依赖必须依次开始"
    assert ends == ["s1", "s2"], "每个开始的步骤都要有结束事件"
    assert all(e["status"] == "done" for e in evs if e["type"] == "step_end")


def test_skipped_step_still_emits_end(monkeypatch):
    """上游失败时下游不执行，但前端必须知道它被跳过了——否则卡在"等待中"。"""
    steps = _steps()

    def fail(st, **kw):
        st.status = "failed"
        st.error = "上游模型 400"
        return st

    monkeypatch.setattr(orch, "run_step", fail)
    evs: list[dict] = []
    orch._execute(steps, goal="g", mode="assist", session_id=None,
                  budget_tokens=4000, emit=evs.append)

    ends = {e["id"]: e for e in evs if e["type"] == "step_end"}
    assert ends["s1"]["status"] == "failed"
    assert ends["s1"]["error"], "失败原因要带出来"
    assert ends["s2"]["status"] == "skipped"
    assert "上游" in ends["s2"]["error"]


def test_emit_exception_never_breaks_dag(monkeypatch):
    steps = _steps()

    def fake_run_step(st, **kw):
        st.status = "done"
        st.output = "ok"
        return st

    def boom(ev):
        raise RuntimeError("前端断线了")

    monkeypatch.setattr(orch, "run_step", fake_run_step)
    board = orch._execute(steps, goal="g", mode="assist", session_id=None,
                          budget_tokens=4000, emit=boom)
    assert [s.status for s in steps] == ["done", "done"], "进度事件失败不能影响执行"
    assert board.has("s1") and board.has("s2")


def test_step_end_carries_duration_and_tokens(monkeypatch):
    steps = _steps()[:1]

    def fake_run_step(st, **kw):
        st.status = "done"
        st.output = "ok"
        st.tokens = 321
        return st

    monkeypatch.setattr(orch, "run_step", fake_run_step)
    evs: list[dict] = []
    orch._execute(steps, goal="g", mode="assist", session_id=None,
                  budget_tokens=4000, emit=evs.append)
    end = [e for e in evs if e["type"] == "step_end"][0]
    assert end["tokens"] == 321
    assert isinstance(end["duration_ms"], int)
