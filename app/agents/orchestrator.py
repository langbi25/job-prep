"""多 Agent 编排：把一件事拆成有依赖的步骤，分派给不同角色的子 agent，再汇总。

和"多轮工具调用"的本质区别：
单 agent 的循环里，所有步骤共享**同一个上下文**——查了 5 个工具之后，第 6 步的模型
看到的是 5 份工具结果堆在一起，注意力被稀释，越到后面越容易跑偏。
编排给每一步一个**干净上下文**：只带目标、自己的职责、依赖步骤的结论，
其余一律看不见。这是"分工"真正的收益，不是把一次调用拆成四次。

四个角色不是提示词人设，是**工具权限 + 上下文可见性**的组合：
- researcher  只读工具，负责把事实取回来，不做判断（判断会污染后续步骤）
- analyst     只读工具，基于已有结论做推理，不许再取数据（防止无限查证）
- writer      无工具，只写字（有工具的写手会顺手改数据）
- critic      无工具，只挑错（让它能改就等于让它给自己打高分）

编排默认只读（mode=assist）。原因很直接：一条 plan 里 4 个步骤并行跑，
任何一步写库出问题都很难定位是谁写的，而编排的价值在分析类任务上——
真正要改数据时，单 agent + 人工审批那条路径更可控。
"""
import concurrent.futures
import datetime
import json
import random
import threading
import time
from dataclasses import dataclass, field

from ..core import budget, config, llm, pricing, prompts
from ..core.db import get_conn
from ..mcp import dispatch
from ..services import agent_tools, harness, harness_admin

MAX_STEPS = 5
MAX_PARALLEL = 3
STEP_TIMEOUT_S = 180

ROLE_TOOLS = {
    "researcher": ("get_daily_brief", "get_weak_topics", "get_top_questions", "search_kb",
                   "list_due_cards", "list_open_mistakes", "list_recent_ingests",
                   "recall_memory", "fetch_tool_result"),
    # analyst 故意不给工具：它的职责是"基于上游结论推理"，给了工具就会反复查证——
    # 实测一次分析步骤连调 4 次 recall_memory，上下文被自己撑爆，结论反而更散。
    "analyst": (),
    "writer": (),
    "critic": (),
}
ROLE_LABEL = {"researcher": "调研", "analyst": "分析", "writer": "撰写", "critic": "校验"}
ROLE_RULES = {
    "researcher": "你的任务是查证：调用只读工具把事实取回来（数字、id、清单、状态）。"
                  "不做判断、不给建议——判断留给下游步骤。查到就停，不要反复查同一个东西。",
    "analyst": "你只能基于上游结论推理，不能取新数据。职责是把事实变成判断：优先级、根因、取舍。"
               "每个判断都要指向具体事实（哪个数字、哪道题、哪张卡）。",
    "writer": "你没有工具，只负责把结论写成给人看的文字。不许新增事实、不许编数字；"
              "信息不足就如实写清楚缺什么。",
    "critic": "你没有工具，只负责挑错：无依据的断言、答非所问、被掩盖的失败。"
              "措辞和排版问题不要报。",
}


@dataclass
class PlanStep:
    id: str
    role: str
    goal: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    output: str = ""
    tokens: int = 0
    cost: float = 0.0
    duration_ms: int = 0
    error: str | None = None
    tools: list[str] = field(default_factory=list)


class Blackboard:
    """步骤间共享的最小状态：只放"结论"，不放原始数据。

    原始工具结果留在各自步骤的上下文里——搬过来会让后续步骤重新背上上下文包袱，
    那就退化成单 agent 了。

    seed 是续跑用的：上次跑完的步骤结论从 `steps_json` 回填进来，
    否则"断点续跑"会变成"从头重跑"（已完成的步骤拿不到自己的产出）。
    """

    def __init__(self, seed: dict[str, str] | None = None):
        self._done: dict[str, str] = {k: v for k, v in (seed or {}).items() if v}

    def put(self, step_id: str, output: str) -> None:
        self._done[step_id] = (output or "").strip()[:2000]

    def has(self, step_id: str) -> bool:
        return bool(self._done.get(step_id))

    def snapshot(self) -> dict[str, str]:
        return dict(self._done)

    def view(self, deps: list[str]) -> str:
        if not deps:
            return "（这是第一步，没有上游结论）"
        lines = []
        for d in deps:
            text = self._done.get(d)
            if text:
                lines.append(f"### 步骤 {d} 的结论\n{text}")
        return "\n\n".join(lines) or "（上游步骤没有产出可用结论）"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _plan_id() -> str:
    return f"plan-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(100, 999)}"


def _valid_plan(data: dict) -> tuple[list[PlanStep], list[str]]:
    """校验 plan：id 唯一、role 合法、依赖存在、无环、步数有界。

    这些检查不能省：模型生成的 DAG 出现过"依赖自己的步骤"和"引用不存在 id"，
    执行时会静默死循环或 KeyError，比直接拒绝更难查。
    """
    errs: list[str] = []
    raw = data.get("steps") or []
    if not isinstance(raw, list) or not raw:
        return [], ["plan 里没有 steps"]
    if len(raw) > MAX_STEPS:
        errs.append(f"步骤数 {len(raw)} 超过上限 {MAX_STEPS}，只取前 {MAX_STEPS} 步")
        raw = raw[:MAX_STEPS]

    steps: list[PlanStep] = []
    seen: set[str] = set()
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            errs.append(f"第 {i + 1} 步不是对象，已丢弃")
            continue
        sid = str(r.get("id") or f"s{i + 1}").strip()
        if sid in seen:
            errs.append(f"步骤 id 重复：{sid}，已跳过")
            continue
        seen.add(sid)
        role = str(r.get("role") or "researcher")
        if role not in ROLE_TOOLS:
            errs.append(f"未知角色 {role}，按 researcher 处理")
            role = "researcher"
        goal = str(r.get("goal") or "").strip()
        if not goal:
            errs.append(f"步骤 {sid} 没有目标，已跳过")
            continue
        deps = [str(d) for d in (r.get("depends_on") or []) if str(d).strip()]
        steps.append(PlanStep(id=sid, role=role, goal=goal[:500], depends_on=deps))
    return steps, errs


def _check_dag(steps: list[PlanStep]) -> list[str]:
    """清理非法依赖并检环。

    清理必须**重建列表**而不是边遍历边 remove：`for d in s.depends_on: remove(d)`
    会跳过被删元素的下一个，自依赖就漏掉了，紧接着拓扑排序报一个看不懂的环。
    这个坑第一版就踩了。
    """
    errs: list[str] = []
    ids = {s.id for s in steps}
    for s in steps:
        cleaned: list[str] = []
        for d in s.depends_on:
            if d == s.id:
                errs.append(f"步骤 {s.id} 依赖自己，已移除")
                continue
            if d not in ids:
                errs.append(f"步骤 {s.id} 依赖不存在的 {d}，已移除该依赖")
                continue
            if d not in cleaned:
                cleaned.append(d)
        s.depends_on = cleaned
    graph = {s.id: set(s.depends_on) for s in steps}
    resolved: set[str] = set()
    while len(resolved) < len(graph):
        ready = [k for k, v in graph.items() if k not in resolved and not (v - resolved)]
        if not ready:
            dead = sorted(set(graph) - resolved)
            raise ValueError(f"plan 存在循环依赖：{dead}")
        resolved.update(ready)
    return errs


def plan(goal: str, *, context: str = "", max_steps: int = MAX_STEPS) -> dict:
    """让模型出一份步骤 DAG。失败时退化成单步（宁可不编排，也要能跑）。"""
    from ..observability import tracing as tr
    # 场景标签决定 llm_usage.scene 归到哪一栏。原来编排全链路不设，
    # 台账里 49% 的调用挂在 unknown 下——"编排比单 agent 贵几倍"这种问题就答不出来。
    tr.set_scene("orchestrate:plan")
    with tr.span("agent.plan", tr.KIND_CHAIN, {"agent.goal": goal[:500]}) as s:
        try:
            system, user, _v = prompts.render(
                "orchestrator_plan", goal=goal, context=context or "（无额外背景）",
                max_steps=max_steps,
                roles="\n".join(f"- {k}：{ROLE_LABEL[k]}" for k in ROLE_TOOLS))
            data = llm.chat_parsed(system, user)
        except Exception as e:  # noqa: BLE001
            data = {"steps": [{"id": "s1", "role": "researcher", "goal": goal,
                               "depends_on": []}], "plan_error": str(e)[:200]}
        steps, errs = _valid_plan(data)
        if not steps:
            steps = [PlanStep(id="s1", role="researcher", goal=goal)]
        try:
            errs += _check_dag(steps)
        except ValueError as e:
            errs.append(str(e))
            steps = [PlanStep(id="s1", role="researcher", goal=goal)]
        if s is not None:
            s.set_attribute("agent.steps", len(steps))
            s.set_attribute("agent.plan_warnings", "; ".join(errs) or "-")
        return {"steps": steps, "warnings": errs}


def run_step(step: PlanStep, *, goal: str, upstream: str, mode: str = "assist",
             session_id: str | None = None, budget_tokens: int = 3000) -> PlanStep:
    """执行一个步骤。上下文只含：全局目标、本步职责、上游结论。"""
    from ..observability import tracing as tr
    from ..context import assembler
    from ..context.token_budget import Budget
    # 步骤在 worker 线程里跑：contextvar 是线程局部的，所以这里 set 正好只影响
    # 本步骤的调用——并行跑三步时各自的 scene 不会互相串。
    tr.set_scene(f"orchestrate:{step.role}")
    t0 = time.perf_counter()
    allowed = ROLE_TOOLS[step.role]
    all_tools, all_impl = harness_admin.all_tools()
    impl = {k: v for k, v in all_impl.items() if k in allowed}
    tools = [t for t in all_tools if t["function"]["name"] in allowed]
    schemas = dispatch.build_schemas(tools)
    budget = Budget(soft_cap=max(800, int(budget_tokens)))

    try:
        _sys, user, _v = prompts.render(
            "orchestrator_step", role=step.role, role_label=ROLE_LABEL[step.role],
            role_rules=ROLE_RULES[step.role],
            goal=goal, step_goal=step.goal, upstream=upstream)
        messages = [{"role": "system", "content": _sys}, {"role": "user", "content": user}]

        with tr.agent_span(f"agent.step.{step.id}", {
                "agent.role": step.role, "agent.step_id": step.id,
                "agent.step_goal": step.goal[:300], "agent.parent_goal": goal[:300]}) as _tid:
            content = ""
            max_iters = 3 if step.role in ("researcher", "analyst") else 1
            for _i in range(max_iters):
                # 没有工具时（writer/critic/analyst）必须关掉 json_mode：
                # DeepSeek 的 json_object 模式要求 prompt 里有 "json" 字样，
                # 否则返回 400。这个坑让第一版编排的撰写步骤整步失败。
                msg = llm._chat_json(None, None, tools=tools, messages=messages,
                                     json_mode=bool(tools))
                # 用量必须从 msg["_usage"] 取：llm.last_usage() 是进程级全局，
                # 这里三个步骤并行跑，全局值会互相覆盖，步费用会串到别的步上。
                use = msg.get("_usage") or {}
                step.tokens += int(use.get("prompt_tokens") or 0) + int(use.get("completion_tokens") or 0)
                step.cost += float(use.get("cost_cny") or 0.0)
                calls = msg.get("tool_calls") or []
                if not calls:
                    content = (msg.get("content") or "").strip()
                    break
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": calls})
                outs = dispatch.run_batch(
                    calls, impl, schemas,
                    readonly_fn=lambda n: n not in agent_tools.WRITE_TOOLS,
                    session_id=session_id, turn=0, source=f"agent:{step.role}",
                    skill=f"plan:{step.id}")
                for oc in outs:
                    step.tools.append(oc.name)
                    messages.append({"role": "tool", "tool_call_id": oc.call_id,
                                     "content": oc.payload})
                # 步内也要守预算：3 次工具往返足以把 8K 挤爆
                assembler.enforce_inflight(messages, budget)

            if not content:
                # 工具调完就直接结束的话，本步没有结论可用——再要一次纯文本总结。
                # 两个必须：不带 tools（否则它又去调工具，陷入循环）；
                # json_mode=False（DeepSeek/OpenAI 的 json_object 模式要求 prompt 里出现
                # "json" 字样，否则直接 400——这个坑让第一次真实编排整条挂掉）。
                msg = llm._chat_json(None, None, messages=messages + [{
                    "role": "user", "content": "基于上面查到的内容，用 3 句话给出本步结论，不要再调用工具。"}],
                    json_mode=False)
                use = msg.get("_usage") or {}
                step.tokens += int(use.get("prompt_tokens") or 0) + int(use.get("completion_tokens") or 0)
                step.cost += float(use.get("cost_cny") or 0.0)
                content = (msg.get("content") or "").strip()
        step.output = content[:3000] or "（本步没有产出文本）"
        step.status = "done"
    except Exception as e:  # noqa: BLE001  单步失败不该让整条编排崩掉
        step.status = "failed"
        step.error = f"{type(e).__name__}: {e}"[:300]
        step.output = f"（本步失败：{step.error}）"
    step.duration_ms = int((time.perf_counter() - t0) * 1000)
    return step


def _execute(steps: list[PlanStep], *, goal: str, mode: str, session_id: str | None,
             budget_tokens: int, board: Blackboard | None = None,
             emit=None) -> Blackboard:
    """拓扑调度：依赖满足即并行，最多 MAX_PARALLEL 个。

    board 传入时视为续跑：里面已有结论、且 status=done 的步骤不再执行。
    emit 逐步骤发事件——编排一次 20~35 秒，没有进度反馈时用户只能干等，
    而"卡在哪一步"恰恰是排查失败的第一手信息。
    """
    def _emit(ev: dict) -> None:
        # 事件是观测，不能反过来影响调度：emit 抛异常绝不能让 DAG 失败
        if emit is None:
            return
        try:
            emit(ev)
        except Exception:  # noqa: BLE001
            pass

    board = board or Blackboard()
    done: set[str] = {s.id for s in steps if s.status == "done" and board.has(s.id)}
    failed: set[str] = set()
    pending = [s for s in steps if s.id not in done]
    # 分母只算"还要跑的步骤"：续跑时如果按总步数分预算，已完成的那几步会把
    # 剩余步骤的预算摊薄，明明是续跑却比首跑更容易爆上下文。
    todo_n = max(1, len(pending))
    per_step_budget = max(800, budget_tokens // todo_n)

    # 并行步骤要共用同一个 trace / 父 span，先在这里抓一次（见 work() 里的 adopt）
    from ..observability import tracing as _tr
    _dag_ctx = _tr.capture()

    while pending:
        ready: list[PlanStep] = []
        for s in pending:
            # 上游失败 → 本步直接跳过。不要"带病继续"：下游拿不到上游结论，
            # 强行跑出来的东西是编的，比缺一块更危险。
            if any(d in failed for d in s.depends_on):
                s.status = "skipped"
                s.error = "上游步骤失败，本步跳过"
                failed.add(s.id)
                _emit({"type": "step_end", "id": s.id, "role": s.role,
                       "status": "skipped", "tokens": 0, "duration_ms": 0,
                       "error": s.error})
                continue
            if all(d in done for d in s.depends_on):
                ready.append(s)
        pending = [s for s in pending if s.status == "pending"]
        if not ready:
            break
        batch = ready[:MAX_PARALLEL]
        batch_ids = {s.id for s in batch}
        pending = [s for s in pending if s.id not in batch_ids]

        def work(st: PlanStep) -> PlanStep:
            # 线程池不继承 contextvar：不接回来，每个 step 会自成一条 trace
            # （agent_span 找不到父 id 就自己造根），一条编排在库里看着像 N 次独立任务。
            from ..observability import tracing as _tr
            _tr.adopt(_dag_ctx)
            _emit({"type": "step_start", "id": st.id, "role": st.role})
            t0 = time.perf_counter()
            try:
                return run_step(st, goal=goal, upstream=board.view(st.depends_on), mode=mode,
                                session_id=session_id, budget_tokens=per_step_budget)
            finally:
                # 成功、失败、抛异常都要发结束事件，否则前端会永远停在"进行中"
                _emit({"type": "step_end", "id": st.id, "role": st.role,
                       "status": st.status, "tokens": st.tokens,
                       "duration_ms": int((time.perf_counter() - t0) * 1000),
                       "error": st.error})

        if len(batch) == 1:
            work(batch[0])
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as ex:
                futs = {ex.submit(work, s): s for s in batch}
                try:
                    for f in concurrent.futures.as_completed(futs, timeout=STEP_TIMEOUT_S):
                        f.result()
                except concurrent.futures.TimeoutError:
                    for s in batch:
                        if s.status == "pending":
                            s.status = "failed"
                            s.error = "步骤超时"
                            _emit({"type": "step_end", "id": s.id, "role": s.role,
                                   "status": "failed", "tokens": 0,
                                   "duration_ms": STEP_TIMEOUT_S * 1000,
                                   "error": "步骤超时"})
        for s in batch:
            if s.status == "done":
                done.add(s.id)
                board.put(s.id, s.output)
            else:
                failed.add(s.id)
    return board


def synthesize(goal: str, steps: list[PlanStep], *, mode: str = "assist") -> str:
    from ..observability import tracing as tr
    tr.set_scene("orchestrate:synthesize")
    evidence = "\n\n".join(f"### {s.id}（{ROLE_LABEL.get(s.role, s.role)}）{'失败' if s.status != 'done' else ''}\n{s.output}"
                           for s in steps)
    with tr.span("agent.synthesize", tr.KIND_CHAIN, {"agent.goal": goal[:300]}) as s:
        try:
            system, user, _v = prompts.render(
                "orchestrator_synthesize", goal=goal, evidence=evidence or "（无）",
                mode=mode)
            data = llm.chat_parsed(system, user)
            answer = (data.get("answer") or "").strip()
            if s is not None:
                s.set_attribute("agent.answer_chars", len(answer))
        except Exception as e:  # noqa: BLE001
            answer = "汇总失败，原始步骤结论如下：\n\n" + evidence
            if s is not None:
                s.set_attribute("agent.synthesize_error", str(e)[:200])
    return answer or "（没有可汇总的结论）"


def critique(goal: str, answer: str, steps: list[PlanStep]) -> dict:
    """校验层：只找问题，不改答案。返回 {ok, issues, retry_steps}。"""
    from ..observability import tracing as tr
    tr.set_scene("orchestrate:critique")
    evidence = "\n\n".join(f"### {s.id}\n{s.output[:800]}" for s in steps)
    with tr.span("agent.critique", tr.KIND_CHAIN, {"agent.goal": goal[:300]}) as s:
        try:
            system, user, _v = prompts.render(
                "orchestrator_critique", goal=goal, answer=answer[:3000], evidence=evidence[:4000])
            data = llm.chat_parsed(system, user)
            out = {"ok": bool(data.get("ok")), "issues": data.get("issues") or [],
                   "retry_steps": [str(x) for x in (data.get("retry_steps") or [])]}
        except Exception:  # noqa: BLE001  校验失败就当通过，不能让校验器拖垮结果
            out = {"ok": True, "issues": [], "retry_steps": [], "critique_error": True}
        if s is not None:
            s.set_attribute("agent.critique_ok", out["ok"])
            s.set_attribute("agent.critique_issues", "; ".join(map(str, out["issues"]))[:500] or "-")
    return out


def _save(plan_id: str, *, session_id: str | None, goal: str, steps: list[PlanStep],
          status: str, result: dict, tokens: int, cost: float, duration_ms: int,
          mode: str = "assist", resume_count: int = 0, trace_id: str = "") -> None:
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO agent_plans (id,session_id,goal,plan_json,status,"
                "steps_json,result_json,tokens_used,cost_cny,duration_ms,mode,resume_count,"
                "trace_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, session_id, goal[:1000],
                 json.dumps([{"id": s.id, "role": s.role, "goal": s.goal,
                              "depends_on": s.depends_on} for s in steps], ensure_ascii=False),
                 status,
                 json.dumps([{"id": s.id, "role": s.role, "status": s.status,
                              "tokens": s.tokens, "cost_cny": round(s.cost, 6),
                              "duration_ms": s.duration_ms,
                              "tools": s.tools, "error": s.error,
                              "output": s.output[:1200]} for s in steps], ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False)[:20000],
                 tokens, round(cost, 6), duration_ms, mode, int(resume_count),
                 (trace_id or None), _now(), _now()))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001  台账写失败不该影响已经拿到的答案
        pass


def _retry_round(steps: list[PlanStep], verdict: dict, board: Blackboard, *,
                  goal: str, mode: str, session_id: str | None,
                  extras: list[dict]) -> tuple[str | None, dict, int]:
    """按 critique 点名的步骤重跑一轮。返回 (新答案 or None, 新 verdict, 轮数)。

    只重跑一次：重跑本身要花掉一整轮 LLM，收益递减，而循环重试最容易变成烧钱黑洞。
    orchestrate 与 resume 共用这段——两处各写一遍迟早会漂移。
    """
    if verdict.get("ok") or not verdict.get("retry_steps"):
        return None, verdict, 0
    targets = [s for s in steps if s.id in verdict["retry_steps"]]
    if not targets:
        return None, verdict, 0
    for s in targets:
        s.status = "pending"
        s.error = None
        run_step(s, goal=goal, upstream=board.view(s.depends_on), mode=mode,
                 session_id=session_id)
        if s.status == "done":
            board.put(s.id, s.output)
    with pricing.collect() as c:
        answer = synthesize(goal, steps, mode=mode)
        verdict = critique(goal, answer, steps)
    extras += c
    return answer, verdict, 1


def _steps_from_row(row) -> list[PlanStep]:
    """从落库的 plan_json + steps_json 重建 PlanStep（含上次的状态与产出）。"""
    try:
        plan = json.loads(row["plan_json"] or "[]")
    except (ValueError, TypeError):
        plan = []
    try:
        states = {s.get("id"): s for s in json.loads(row["steps_json"] or "[]") if isinstance(s, dict)}
    except (ValueError, TypeError):
        states = {}
    out: list[PlanStep] = []
    for p in plan if isinstance(plan, list) else []:
        if not isinstance(p, dict):
            continue
        sid = str(p.get("id") or "")
        if not sid:
            continue
        st = states.get(sid) or {}
        role = str(p.get("role") or "researcher")
        out.append(PlanStep(
            id=sid, role=role if role in ROLE_TOOLS else "researcher",
            goal=str(p.get("goal") or "")[:500],
            depends_on=[str(d) for d in (p.get("depends_on") or []) if str(d).strip()],
            status=str(st.get("status") or "pending"),
            output=str(st.get("output") or ""),
            tokens=int(st.get("tokens") or 0),
            cost=float(st.get("cost_cny") or 0),
            duration_ms=int(st.get("duration_ms") or 0),
            tools=list(st.get("tools") or []),
            error=st.get("error")))
    return out


# 同一 plan 并发续跑会互相覆盖台账（两条都读到旧状态，都从断点续跑一遍）。
# SQLite 没有行锁，单进程 uvicorn 下用内存锁最直接。
_RESUME_LOCKS: dict[str, threading.Lock] = {}
_RESUME_GUARD = threading.Lock()


def _plan_lock(plan_id: str) -> threading.Lock:
    with _RESUME_GUARD:
        return _RESUME_LOCKS.setdefault(plan_id, threading.Lock())


def resume(plan_id: str, *, retry: bool = True) -> dict:
    """从断点续跑一条失败/部分完成的编排。

    三条与"重新编排"不同的铁律：
    1. **复用原 DAG，不重新规划**。重新规划会让已完成步骤的结论与新步骤的假设对不上，
       前面花钱查到的事实全部作废。
    2. **已完成步骤的结论从库里回填**（不只是标个 status=done）。没有回填，
       下游步骤会收到"上游没有产出可用结论"，续跑等于重跑还多花一次汇总钱。
    3. **费用与 token 累加，不覆盖**。这条计划一共花了多少钱是它自己的属性，
       续跑只是又花了一笔。

    允许续跑 status=running 的记录：`_save` 只在结束时写库，库里出现 running
    只可能是进程中途挂了留下的残影——那是我们最需要续跑的情况。
    """
    import threading  # noqa: F401
    from ..observability import tracing as tr

    row = _load_row(plan_id)
    if not row:
        raise ValueError("计划不存在")
    if row["status"] == "done":
        raise ValueError("该计划已完成，无需续跑；要重做请发起新的编排")

    steps = _steps_from_row(row)
    if not steps:
        raise ValueError("计划里没有可恢复的步骤")

    lock = _plan_lock(plan_id)
    if not lock.acquire(blocking=False):
        raise ValueError("该计划正在续跑中，请稍后再试")
    try:
        goal = row["goal"]
        mode = row["mode"] if row["mode"] in ("auto", "assist") else "assist"
        session_id = row["session_id"]
        prev_cost = float(row["cost_cny"] or 0)
        prev_tokens = int(row["tokens_used"] or 0)
        resume_no = int(row["resume_count"] or 0) + 1
        prev_status = row["status"]

        # 已完成的步骤进 board；其余一律重置为 pending——上游恢复后下游要能重新够到，
        # 原来因为"上游失败"被 skip 的步骤尤其不能保持 skipped。
        board = Blackboard({s.id: s.output for s in steps if s.status == "done"})
        for s in steps:
            if not board.has(s.id):
                s.status = "pending"
                s.error = None
        base_tokens = sum(s.tokens for s in steps)
        base_cost = sum(s.cost for s in steps)
        done_ids = [s.id for s in steps if s.status == "done"]

        t0 = time.perf_counter()
        budget = max(2000, int(config.CTX_SOFT_CAP) * 2)
        # 接上首跑那次的 trace：不接的话，一次编排在调用树里会断成两棵互不相干的树，
        # "续跑到底补了什么"就永远对不上。start_new_root 由 _TRACE 是否为空决定，
        # 所以这里必须显式 set 而不是依赖调用方。
        tr.set_trace(str(row["trace_id"] or ""))
        with tr.agent_span("agent.resume", {
                "agent.plan_id": plan_id, "agent.goal": goal[:500],
                "agent.mode": mode, "agent.resume_no": resume_no,
                "agent.resume_done_steps": len(done_ids)}) as trace_id:
            extras: list[dict] = []
            _execute(steps, goal=goal, mode=mode, session_id=session_id,
                     budget_tokens=budget, board=board)
            with pricing.collect() as c:
                answer = synthesize(goal, steps, mode=mode)
                verdict = critique(goal, answer, steps)
            extras += c
            rounds = 0
            if retry:
                new_answer, verdict, rounds = _retry_round(
                    steps, verdict, board, goal=goal, mode=mode,
                    session_id=session_id, extras=extras)
                if new_answer is not None:
                    answer = new_answer

        extra_usage = pricing.accumulate(extras)
        # 只加"这次新花的部分"：base_* 是库里恢复出来的上次累计值
        added_cost = sum(s.cost for s in steps) - base_cost
        added_tokens = sum(s.tokens for s in steps) - base_tokens
        cost = round(prev_cost + added_cost + extra_usage["cost_cny"], 6)
        tokens = prev_tokens + added_tokens
        duration_ms = int((time.perf_counter() - t0) * 1000)
        failed = [s.id for s in steps if s.status not in ("done",)]
        status = "done" if not failed else ("partial" if any(s.status == "done" for s in steps) else "failed")

        result = {"plan_id": plan_id, "goal": goal, "answer": answer,
                  "steps": [{"id": s.id, "role": s.role,
                             "role_label": ROLE_LABEL.get(s.role, s.role),
                             "goal": s.goal, "status": s.status, "tools": s.tools,
                             "tokens": s.tokens, "cost_cny": round(s.cost, 6),
                             "duration_ms": s.duration_ms,
                             "error": s.error} for s in steps],
                  "critique": verdict, "retry_rounds": rounds,
                  "warnings": [], "tokens": tokens, "cost_cny": cost,
                  "cost_extra": extra_usage, "duration_ms": duration_ms,
                  "status": status, "trace_id": trace_id,
                  "resumed": True, "resume_count": resume_no,
                  "previous_status": prev_status,
                  "resumed_steps": [s.id for s in steps if s.id not in done_ids],
                  "skipped_done_steps": done_ids}
        _save(plan_id, session_id=session_id, goal=goal, steps=steps, status=status,
              result=result, tokens=tokens, cost=cost, duration_ms=duration_ms,
              mode=mode, resume_count=resume_no, trace_id=trace_id)
        return result
    finally:
        lock.release()


def _load_row(plan_id: str):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM agent_plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        conn.close()


@budget.guarded("orchestrate")
def orchestrate(goal: str, *, session_id: str | None = None, mode: str = "assist",
                max_steps: int = MAX_STEPS, retry: bool = True,
                on_event=None) -> dict:
    """跑一次完整编排：plan → 执行 DAG → 汇总 → 校验（必要时重跑失败/被点名步骤）。

    on_event 收到进度事件：plan / step_start / step_end / synthesize / critique。
    一次编排 20~35 秒，不给进度的话用户看不出是"在跑"还是"卡死了"。
    """
    from ..observability import tracing as tr
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("目标不能为空")
    if mode not in ("auto", "assist"):
        mode = "assist"

    def emit(ev: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001  进度事件是观测，不能改变编排结果
            pass

    t0 = time.perf_counter()
    plan_id = _plan_id()
    budget = max(2000, int(config.CTX_SOFT_CAP) * 2)

    with tr.agent_span("agent.orchestrate", {
            "agent.goal": goal[:800], "agent.mode": mode,
            "agent.plan_id": plan_id}) as trace_id:
        # 主线程的 LLM 调用（规划/合成/自检）不在任何 step 里，费用用收集器攒；
        # worker 线程里 run_step 走 msg["_usage"] 自记账（contextvar 不跨线程，
        # 收集器收不到它们——正好避免双算）。
        extras: list[dict] = []
        with pricing.collect() as c:
            planned = plan(goal, max_steps=max_steps)
        extras += c
        steps = planned["steps"]
        emit({"type": "plan", "plan_id": plan_id,
              "steps": [{"id": s.id, "role": s.role,
                         "role_label": ROLE_LABEL.get(s.role, s.role),
                         "goal": (s.goal or "")[:200],
                         "depends_on": list(s.depends_on)} for s in steps]})
        board = _execute(steps, goal=goal, mode=mode, session_id=session_id,
                         budget_tokens=budget, emit=emit)
        emit({"type": "synthesize"})
        with pricing.collect() as c:
            answer = synthesize(goal, steps, mode=mode)
            verdict = critique(goal, answer, steps)
        extras += c
        emit({"type": "critique", "ok": bool(verdict.get("ok")),
              "issues": [str(x)[:160] for x in (verdict.get("issues") or [])][:6]})
        rounds = 0
        if retry:
            new_answer, verdict, rounds = _retry_round(
                steps, verdict, board, goal=goal, mode=mode,
                session_id=session_id, extras=extras)
            if new_answer is not None:
                answer = new_answer

    extra_usage = pricing.accumulate(extras)
    tokens = sum(s.tokens for s in steps)
    cost = round(sum(s.cost for s in steps) + extra_usage["cost_cny"], 6)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    failed = [s.id for s in steps if s.status not in ("done",)]
    status = "done" if not failed else ("partial" if any(s.status == "done" for s in steps) else "failed")

    result = {"plan_id": plan_id, "goal": goal, "answer": answer,
              "steps": [{"id": s.id, "role": s.role, "role_label": ROLE_LABEL.get(s.role, s.role),
                         "goal": s.goal, "status": s.status, "tools": s.tools,
                         "tokens": s.tokens, "cost_cny": round(s.cost, 6),
                         "duration_ms": s.duration_ms,
                         "error": s.error} for s in steps],
              "critique": verdict, "retry_rounds": rounds,
              "warnings": planned["warnings"], "tokens": tokens,
              "cost_cny": cost, "cost_extra": extra_usage,
              "duration_ms": duration_ms, "status": status, "trace_id": trace_id}
    _save(plan_id, session_id=session_id, goal=goal, steps=steps, status=status,
          result=result, tokens=tokens, cost=cost, duration_ms=duration_ms,
          mode=mode, resume_count=0, trace_id=trace_id)
    return result


def recent_plans(limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, session_id, goal, status, tokens_used, cost_cny, duration_ms, "
            "resume_count, created_at "
            "FROM agent_plans ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def plan_detail(plan_id: str) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM agent_plans WHERE id=?", (plan_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("plan_json", "steps_json", "result_json"):
            try:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
            except (ValueError, TypeError):
                d[k.replace("_json", "")] = None
        return d
    finally:
        conn.close()
