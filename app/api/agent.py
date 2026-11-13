import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..agents import orchestrator
from ..core import config
from ..services import harness
from .sse import emit_safe, hub, sse_response

router = APIRouter(prefix="/api/agent", tags=["agent"])


class PlanRequest(BaseModel):
    direction: str = config.DEFAULT_DIRECTION


@router.post("/plan")
def study_plan(payload: PlanRequest):
    """学习教练 Agent：自主调工具查数据 → 产出学习计划，全程 trace 落库。"""
    return harness.run_study_plan_agent(payload.direction)


class OrchestrateRequest(BaseModel):
    goal: str
    session_id: str | None = None
    mode: str = "assist"        # assist 只读（默认）/ auto 允许写工具
    max_steps: int = 4
    retry: bool = True


@router.post("/orchestrate")
def orchestrate(req: OrchestrateRequest):
    """多 Agent 编排：拆解 → 并行执行 → 汇总 → 自检。

    默认只读。要允许子 agent 写库必须显式传 mode=auto——编排里并行写库
    出问题无法定位是谁写的，这条默认值是安全边界，不是保守。
    """
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，编排不可用（见 .env）")
    try:
        return orchestrator.orchestrate(req.goal, session_id=req.session_id,
                                        mode=req.mode, max_steps=req.max_steps,
                                        retry=req.retry)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/orchestrate/stream")
def orchestrate_stream(req: OrchestrateRequest):
    """同 /orchestrate，但把进度以 SSE 推出来。

    为什么值得单独一条：一次编排 20~35 秒、最多 4 步并行，用户点完按钮只能看转圈，
    分不清"在跑"和"卡死"。事件里带每一步的开始/结束、耗时、token、错误，
    失败时能直接看出断在哪一步。

    事件协议：
      plan      规划完成 {plan_id, steps:[{id,role,role_label,goal,depends_on}]}
      step_start 某步开始 {id, role}
      step_end   某步结束 {id, status, duration_ms, tokens, error}
      synthesize 开始汇总
      critique   自检结果 {ok, issues}
      done       完整结果，结构同 POST /orchestrate
      error      {message}

    断线续传：响应头 X-Stream-Id，GET /orchestrate/stream/{id}?after=<seq> 补齐。
    """
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，编排不可用（见 .env）")
    if not (req.goal or "").strip():
        raise HTTPException(400, "目标不能为空")

    stream_id, bus = hub.create("orch")
    emit = emit_safe(bus)

    def job():
        try:
            res = orchestrator.orchestrate(req.goal, session_id=req.session_id,
                                           mode=req.mode, max_steps=req.max_steps,
                                           retry=req.retry, on_event=emit)
            bus.publish({"type": "done", "result": res})
        except Exception as e:  # noqa: BLE001  错误也要以事件到达，不能只断流
            bus.publish({"type": "error", "message": f"{type(e).__name__}: {e}"[:500]})
        finally:
            bus.close()

    threading.Thread(target=job, daemon=True).start()
    return sse_response(bus, stream_id)


@router.get("/orchestrate/stream/{stream_id}")
def resume_orchestrate(stream_id: str, after: int = 0):
    """编排断线续传。after 传已收到的最后一个 seq，传 0 等于从头回放。"""
    bus = hub.get(stream_id)
    if bus is None:
        raise HTTPException(404, "这个流已过期（保留 10 分钟），请重新发起编排")
    return sse_response(bus, stream_id, after)


@router.get("/plans")
def plans(limit: int = 20):
    return {"plans": orchestrator.recent_plans(limit)}


@router.get("/memory")
def memory(session_id: str | None = None):
    """长期记忆全貌：统计 + 当前有效的记忆条目。

    面板要能回答"它记住了什么、为什么这条被替代了"——所以统计里带 events 计数。
    """
    from ..memory import manager as memory_manager
    return memory_manager.dump(session_id)


@router.get("/cost")
def cost(days: int = 14):
    """LLM 成本核算：总览 + 按天/场景/模型/会话聚合（详见 app/core/pricing.py）。

    带 budget：当天预算进度与是否已触发硬闸门。面板必须能看到"今天还剩多少"，
    否则用户只知道"突然不能用了"，不知道是钱花完了。
    """
    from ..core import budget, pricing
    return {**pricing.summary(days), "budget": budget.status()}


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: str):
    d = orchestrator.plan_detail(plan_id)
    if not d:
        raise HTTPException(404, f"编排记录不存在: {plan_id}")
    return d


@router.post("/plans/{plan_id}/resume")
def resume_plan(plan_id: str, retry: bool = True):
    """从断点续跑：复用原 DAG，只重跑未完成的步骤。

    不重新规划是有意的——重规划会让已完成步骤的结论与新步骤的假设对不上，
    之前花钱查到的事实全部作废。
    """
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，续跑不可用（见 .env）")
    try:
        return orchestrator.resume(plan_id, retry=retry)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/runs")
def list_runs(limit: int = 10):
    from ..core.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, kind, status, created_at, steps_json FROM runs "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"steps": __import__("json").loads(r["steps_json"])} for r in rows]
    finally:
        conn.close()
