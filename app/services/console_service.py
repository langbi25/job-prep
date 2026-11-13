"""对话控制台：Agent 的唯一入口。

设计取舍（和一个"聊天框套壳"的区别）：

1. **它不是 RAG 问答**，是一个会改系统的执行器。每一轮都能连续调多个工具
   （投喂→抽取→入池→起草→跑地图），最后用人话汇报——用户不再充当编排器。
2. **写权限有开关**（auto / assist）。assist 模式下写工具在工具层直接拒绝，
   不靠 prompt 里"请不要写"这种软约束——模型不听话时，硬闸门才是保障。
3. **轨迹落库**（`console_messages.trace_json`）。每一轮调了什么工具、入参、
   结果预览都能回放，出问题不用翻日志猜。
4. **流式只转发正文，不转发思考**：模型调工具前那句"我先查一下"也会以 delta 吐出来，
   但它不是回复。前端靠 turn_end(has_tools=true) 丢弃这段，判别依据只能是轮次结束时
   有没有 tool_calls，不能靠文字像不像过渡句。
5. **上下文由 assembler 统一出**（2026-11-05 改造）：历史走"窗口 + 关键轮锚定"，
   窗口外的压成摘要，再按需检索注入，超预算按 检索→摘要→最旧轮次 降级；
   轮内工具结果单独裁（8 步 × 2600 字比历史更容易爆）。每轮的 token 账本落在
   `ctx_traces`，本轮返回值里的 `ctx` 就是这份账本。
"""
import datetime
import json
import random
import time

from ..core import budget, config, llm, pricing, prompts
from ..core.db import get_conn
from ..mcp import dispatch
from ..skills import registry
from . import agent_tools, harness, harness_admin, review_service, turn_facts
from ..context import assembler

MAX_ITERS = 8
HISTORY_TURNS = 6          # 带给模型的历史轮数（一问一答算一轮）
HISTORY_FETCH_MSGS = 40    # 从库里捞多少条历史交给 assembler 去挑
MAX_MESSAGE_CHARS = 20000  # 单条用户输入上限，防把整本书粘进来

MODE_DESC = {
    "auto": "自动执行（可以直接改数据：入池、起草卡、跑地图、出计划）",
    "assist": "只查不改（只能读；所有写工具都会拒绝执行）",
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_session_id() -> str:
    return f"cs-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(1000, 9999)}"


def _env_snapshot() -> str:
    """把"当前状态"直接写进 system prompt。

    为什么不让模型自己调工具查：这几项几乎每轮都要用，查一次就多一轮往返
    （60~90 秒）。统计口径是纯 SQL，成本几乎为零，直接喂给它更划算。

    到期数必须走 review_service.due_stats，不允许在这里另写一条 SQL：
    这里曾经写的是 `srs_due IS NOT NULL AND srs_due<=今天`，只算逾期、不算新卡，
    于是 prompt 说「今日到期 3 张」而复习页有 25 张。模型据此回答用户，
    那 22 张新卡就永远进不了对话——口径不一致的代价不是显示难看，是行为错误。
    """
    conn = get_conn()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_actions WHERE status='pending'").fetchone()["n"]
        st = review_service.due_stats(conn=conn)
        iv = conn.execute(
            "SELECT id, company, position, created_at FROM feed_interviews "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        ing = conn.execute(
            "SELECT id, status FROM ingests ORDER BY created_at DESC LIMIT 1").fetchone()
        due_line = (f"- 知识卡 {st['active']} 张（活卡）：待首次学习 {st['new']} 张，"
                    f"逾期复习 {st['overdue']} 张")
        if st["overdue"]:
            due_line += f"（最久逾期 {st['oldest_overdue_days']} 天）"
        lines = [
            due_line,
            f"- 待审批动作 {pending} 条" + ("（建卡/合并都在审批中心）" if pending else ""),
            f"- 最近一份面经：{iv['id']}｜{iv['company'] or '未知公司'} {iv['position'] or ''}（{iv['created_at'][:10]}）"
            if iv else "- 还没有面经",
            f"- 最近一次采集：{ing['id']}（状态 {ing['status']}）" if ing else "- 还没有采集记录",
        ]
        return "\n".join(lines)
    finally:
        conn.close()


def _history(session_id: str) -> list[dict]:
    """捞最近一段历史**给 assembler 挑**，在这里截断等于替它做决定。

    原先只取 HISTORY_TURNS*2 条（= 窗口大小），结果窗口外的消息 assembler 根本看不见，
    摘要永远没有注入的机会——"摘要写了但没用上"就是这么来的。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, role, content FROM console_messages WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?", (session_id, HISTORY_FETCH_MSGS)).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in reversed(rows)]
    finally:
        conn.close()


def _save(session_id: str, role: str, content: str, trace: list | None = None,
          mode: str | None = None, ctx: dict | None = None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO console_messages (session_id,role,content,trace_json,ctx_json,mode,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, role, content, json.dumps(trace or [], ensure_ascii=False),
             json.dumps(ctx or {}, ensure_ascii=False), mode, _now()))
        conn.commit()
    finally:
        conn.close()


def history(session_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content, trace_json, ctx_json, mode, created_at FROM console_messages "
            "WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [{"role": r["role"], "content": r["content"],
                 "trace": json.loads(r["trace_json"] or "[]"),
                 "ctx": json.loads(r["ctx_json"] or "{}"),
                 "mode": r["mode"], "created_at": r["created_at"]} for r in rows]
    finally:
        conn.close()


def sessions(limit: int = 20) -> list[dict]:
    """会话列表：标题取该会话第一句用户输入。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT session_id, COUNT(*) AS n, MAX(created_at) AS last_at, "
            "MIN(CASE WHEN role='user' THEN content END) AS title "
            "FROM console_messages GROUP BY session_id ORDER BY last_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"session_id": r["session_id"], "n": r["n"], "last_at": r["last_at"],
                 "title": (r["title"] or "（空会话）")[:40]} for r in rows]
    finally:
        conn.close()


def _artifacts(trace: list[dict]) -> list[dict]:
    """从这一轮实际调过哪些工具，反推该给用户哪些跳转。

    不用模型生成链接：它会拼错路径；这里按工具名硬映射，保证点得通。
    """
    names = {s.get("tool") for s in trace}
    out: list[dict] = []
    if names & {"ingest_text", "confirm_ingest", "list_recent_ingests"}:
        out.append({"label": "看采集详情", "url": "#/ingests", "kind": "ingests"})
    if "analyze_coverage" in names:
        out.append({"label": "看作战地图", "url": "#/coverage", "kind": "coverage"})
    if names & {"draft_cards", "list_pending_actions"} or "ingest_text" in names:
        out.append({"label": "去审批中心", "url": "#/approvals", "kind": "approvals"})
    if "make_study_plan" in names:
        out.append({"label": "去总览", "url": "#/dashboard", "kind": "dashboard"})
    return out


@budget.guarded("chat")
def chat(message: str, session_id: str | None = None, mode: str = "auto",
         skill: str | None = None, on_event=None) -> dict:
    """跑一轮对话。返回 {session_id, reply, trace, artifacts, iterations, ctx, skill}。

    AGENT 根 span 在这里起：一轮对话触发的 assemble/retriever/llm/tool 全部挂在
    同一棵 span 树下，trace_id 写进 ctx 账本——页面上能对上 Phoenix/Langfuse 里的瀑布。
    session/轮次：一场会话（session_id）= 多轮对话；一轮（turn）= 用户一句话触发的
    完整 agent 循环。轮次号 = 历史用户消息数 + 1，span 自动带 session.id/session.turn，
    Phoenix 按 session.id 聚成 Sessions，面板做「会话 → 轮次 → trace」下钻。

    skill 为空时按用户这句话自动选技能（选不中就退回通用 agent）；
    显式传技能名则强制使用（调 API 的场景需要确定性）。

    on_event(ev)：过程事件回调（SSE 流式用）。事件序：
      segment（段开始）→ delta*（LLM 内容增量）→ tool*（工具结果）
      → turn_end（本段该轮结束；has_tools=true 表示这轮调了工具，
      它吐出的 delta 只是工具调用前的思考文字，调用方应丢弃）
      …… 循环 …… → 最终 chat() 返回完整结果。
    回调抛异常一律吞掉：流式是体验增强，绝不能因为它打断正在跑的对话。
    """
    from ..observability import tracing as tr
    tr.set_scene(f"console:{mode}")
    # input.value 让 Phoenix 的 Sessions 显示"这轮用户问了什么"
    with tr.agent_span("agent.console", {"agent.mode": mode,
                                         "agent.skill": skill or "auto",
                                         "input.value": (message or ""),
                                         "input.mime_type": "text"}) as trace_id:
        # 收集本轮的 token 与费用：技能记账要落"这次跑了多少钱"，而账只有
        # LLM 咽喉知道。collect 不跨线程——但它和 _chat_inner 在同一线程，
        # 所以 _record_skills 读 snapshot() 拿到的是本轮全量（含所有段）。
        with pricing.collect():
            result = _chat_inner(message, session_id, mode, trace_id, skill, on_event)
        if isinstance(result.get("ctx"), dict):
            result["ctx"]["trace_id"] = trace_id
        return result


HANDOFF_CHARS = 800        # 段间只传结论（不是原始工具结果），太多等于把上一段的上下文搬过来


def _with_handoff(message: str, prev_title: str, handoff: str) -> str:
    """给后续段拼上"上一段结论"。放 user 里而不是 system：
    它是本段的输入材料，和本轮用户原话同一性质。"""
    if not handoff:
        return message
    return (f"{message}\n\n【上一段（{prev_title}）的结论，作为本段输入】\n{handoff}\n\n"
            "请在此基础上完成本段，不要重复上一段已经做过的事，也不要重新查一遍。")


def _merge_ctx(acc: dict, new: dict) -> dict:
    """多段执行的上下文账本累加。

    total/token 相加是诚实的（这一轮确实花了这么多），但 budget 保留第一段的——
    它是"单次请求的预算"，不是"整轮的预算"。memory_n 取 max 而不是相加：
    同一个数字相加没有意义（那不是 token 数，是"召回了几个"）。
    """
    if not acc:
        return dict(new or {})
    if not new:
        return acc
    sections = dict(acc.get("sections") or {})
    for k, v in (new.get("sections") or {}).items():
        sections[k] = sections.get(k, 0) + v
    return {**acc, "sections": sections,
            "total": (acc.get("total") or 0) + (new.get("total") or 0),
            "memory_n": max(acc.get("memory_n") or 0, new.get("memory_n") or 0),
            "retrieved_n": max(acc.get("retrieved_n") or 0, new.get("retrieved_n") or 0),
            "dropped": (acc.get("dropped") or 0) + (new.get("dropped") or 0),
            "degraded": list(dict.fromkeys((acc.get("degraded") or []) + (new.get("degraded") or []))),
            "fallback": acc.get("fallback") or new.get("fallback")}


def _run_segment(*, sk, seg_goal: str, message: str, sid: str, mode: str,
                 history_msgs: list[dict], turn: int, position: tuple[int, int],
                 first: bool, trace_id: str = "", on_event=None) -> dict:
    """跑一段：完整的一轮 agent 循环，工具白名单与上下文都按**本段**技能重算。

    这是技能嵌套能成立的关键——如果几段共用一份 messages 和一份工具列表，
    第二段看到的是第一段查回的全部原始数据，等于单 agent 加了个"假装分工"的标签。
    on_event 由 _chat_inner 传入，已经做过异常兜底，这里可以直接调。
    """
    from ..observability import tracing as tr
    system, user_msg, prompt_ver = prompts.render(
        "console_agent",
        mode_desc=MODE_DESC[mode],
        today=datetime.date.today().isoformat(),
        env=_env_snapshot(),
        message=message,
    )
    if sk:
        # SOP 拼在 render 之后：它每次不同，且不该进 prompt 版本管理（版本管的是通用规则）
        system = system + "\n\n" + registry.describe(sk, position=position, goal=seg_goal)

    # 检索只在第一段做：后续段的材料由上一段交接，重复检索既费 token，
    # 又会让第二段拿着与它职责无关的卡片自说自话。
    # persist 同理——一轮对话只该有一条 ctx_traces，否则"轮次"统计会被撑成多行。
    ctx = assembler.assemble(sid, system, user_msg, history_msgs,
                             use_retrieval=first, use_memory=True, persist=first)
    messages = ctx.messages

    # 工具清单只从 harness_admin 取：面板显示的就是模型能用的，不再手工拼两遍
    all_tools, all_impl = harness_admin.all_tools()
    if sk:
        allowed = set(sk.tools) | harness_admin.EXEMPT_TOOLS
        tools = [t for t in all_tools if t["function"]["name"] in allowed]
        impl = {k: v for k, v in all_impl.items() if k in allowed}
    else:
        tools, impl = all_tools, all_impl
    max_iters = sk.max_iters if sk else MAX_ITERS
    tool_schemas = dispatch.build_schemas(tools)

    trace: list[dict] = []
    reply = ""
    error = None
    inflight: list[dict] = []
    n_llm = 0          # 本段真正问了几次模型：工具调用的次数不等于它，两者要分开记
    i_n, n_seg = position

    # 段开始事件：组合技能跑 20s+ 时用户不再是盲等，能看到"现在跑到第几段、干什么"
    if on_event is not None:
        on_event({"type": "segment", "index": i_n, "total": n_seg,
                  "skill": sk.name if sk else None,
                  "title": sk.title if sk else "通用 agent",
                  "goal": (seg_goal or "")[:200]})

    with tr.span(f"agent.segment.{i_n}", tr.KIND_CHAIN, {
            "agent.skill": sk.name if sk else "-",
            "agent.segment": f"{i_n}/{n_seg}",
            "agent.segment_goal": (seg_goal or "")[:200],
            "agent.parent_trace": trace_id or "-"}) as sp:
        try:
            for i in range(max_iters):
                n_llm += 1
                msg = llm._chat_json(
                    None, None, tools=tools, messages=messages,
                    on_delta=(lambda t: on_event({"type": "delta", "seg": i_n, "text": t}))
                    if on_event else None)
                calls = msg.get("tool_calls") or []
                # 该轮结束才通知"带没带工具调用"：带的话它先吐出的 delta 只是
                # 工具调用前的思考文字，前端要丢弃（最终回复一定是无工具那一轮）
                if on_event is not None:
                    on_event({"type": "turn_end", "seg": i_n, "iter": i,
                              "has_tools": bool(calls)})
                if calls:
                    messages.append({"role": "assistant", "content": msg.get("content") or "",
                                     "tool_calls": calls})
                    # 连续只读调用并发、写操作串行；参数校验/重试/大结果分片都在 dispatch 里
                    outcomes = dispatch.run_batch(
                        calls, impl, tool_schemas,
                        readonly_fn=lambda n: n not in agent_tools.WRITE_TOOLS,
                        session_id=sid, turn=turn, source="console",
                        skill=sk.name if sk else "-")
                    for oc in outcomes:
                        if on_event is not None:
                            on_event({"type": "tool", "seg": i_n, "tool": oc.name,
                                      "ok": oc.ok, "error": oc.error, "cached": oc.cached})
                        item = {"iter": i, "seg": i_n, "tool": oc.name,
                                "args": _short_args(oc.args), "ok": oc.ok,
                                "preview": oc.payload[:300]}
                        if oc.cached:
                            item["cached"] = True
                        if oc.retried:
                            item["retried"] = True
                        if oc.ref:
                            item["ref"] = oc.ref
                        if oc.error:
                            item["error"] = oc.error
                        trace.append(item)
                        messages.append({"role": "tool", "tool_call_id": oc.call_id,
                                         "content": oc.payload})
                    # 工具结果一轮轮堆进去最容易爆：进入下一轮前先按预算裁最旧的
                    inflight.append(assembler.enforce_inflight(messages))
                    continue

                reply = (msg.get("content") or "").strip()
                break
            else:
                reply = (f"这一段我没在 {max_iters} 轮工具调用内做完（通常是材料太大或工具反复报错）。"
                         "我已经执行的步骤在下方面板里，你可以让我分段继续。")
        except Exception as e:  # noqa: BLE001  一段挂掉不该把已经跑完的其他段也丢掉
            error = f"{type(e).__name__}: {e}"
            reply = f"（第 {i_n} 段执行失败：{error[:200]}）"
        if sp is not None:
            used = list(dict.fromkeys(t["tool"] for t in trace))
            sp.set_attribute("agent.tools_used", ",".join(used) or "-")
            sp.set_attribute("agent.reply_chars", len(reply))
            sp.set_attribute("agent.segment_error", (error or "-")[:200])

    return {"reply": reply or "（模型没有返回内容）", "trace": trace,
            "inflight": inflight, "ctx": ctx.as_dict(), "llm_calls": n_llm,
            "prompt_version": prompt_ver, "error": error}


def _record_skills(sk, chain: list[dict], trace: list[dict], sid: str, reply: str,
                   multi: bool, duration_ms: int, ctx: dict) -> dict | None:
    """技能记账与验收。

    组合技能只记一条 parent 记录，子技能不单独记：用户视角是"我用了一次
    diagnose_to_cards"，子技能的表现留在 segments 里。分开记会让"技能调用次数"
    因为一次组合就 +3，把统计读歪。
    """
    if sk is None:
        return None
    # 本轮的 token / 费用：由 chat() 开的收集器攒，跑完才知道总数。
    # 原来这里不传，skill_runs.tokens 恒为 0——面板上"技能成本"永远是 0 元，
    # 比显示得贵更危险：会让人误以为这些技能免费。
    usage = pricing.snapshot()
    if multi:
        sts = {c["status"] for c in chain}
        status = "ok" if sts == {"ok"} else ("failed" if sts <= {"failed"} else "partial")
        registry.record(sk.name, session_id=sid, status=status, steps=len(trace),
                        tools_used=[t["tool"] for t in trace], duration_ms=duration_ms,
                        tokens=usage["total_tokens"], cost_cny=usage["cost_cny"])
        return {"name": sk.name, "title": sk.title, "status": status,
                "missing": [m for c in chain for m in c["missing"]],
                "via_memory": any(c["via_memory"] for c in chain),
                "tools": [], "segments": chain, "usage": usage}

    verdict = registry.check(sk, [t["tool"] for t in trace],
                             memory_hits=ctx.get("memory_n", 0))
    status = "ok" if (verdict["ok"] and reply) else ("partial" if trace else "failed")
    registry.record(sk.name, session_id=sid, status=status, steps=len(trace),
                    tools_used=verdict["used"], duration_ms=duration_ms,
                    tokens=usage["total_tokens"], cost_cny=usage["cost_cny"])
    return {"name": sk.name, "title": sk.title, "status": status,
            "missing": verdict["missing"], "via_memory": verdict["via_memory"],
            "tools": list(sk.tools), "usage": usage}


def _chat_inner(message: str, session_id: str | None, mode: str, trace_id: str = "",
                skill: str | None = None, on_event=None) -> dict:
    """原对话循环体（观测无关）。组合技能在这里展开成多段串行执行。"""
    message = (message or "").strip()
    if not message:
        raise ValueError("消息不能为空")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"单条消息最多 {MAX_MESSAGE_CHARS} 字，请分段发")

    def emit(ev: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001  回调坏了不能打断正在跑的对话
            pass

    mode = mode if mode in MODE_DESC else "auto"
    sid = session_id or new_session_id()
    history_msgs = _history(sid)
    # 轮次号：客观侧以"用户消息数"为准——一句"继续"也是新的一轮
    turn = sum(1 for m in history_msgs if m.get("role") == "user") + 1
    from ..observability import tracing as tr
    tr.set_session(sid, turn)

    # 技能路由：先定"这类事该怎么做"，再决定给模型哪些工具
    sk, skill_board = registry.select(message, hint=skill)
    # 组合技能展开成多段；普通技能是单段；选不中是 [None]（通用 agent）
    segments: list = registry.expand(sk) if sk else [None]
    multi = len(segments) > 1

    all_trace: list[dict] = []
    inflight: list[dict] = []
    chain: list[dict] = []
    ctx_acc: dict = {}
    reply = ""
    handoff = ""
    prev_title = ""
    prompt_ver = ""
    llm_calls = 0      # 整轮问了几次模型（多段累加），用于判断"是不是打到上限还没收敛"
    need_tools = 0     # 本轮技能声明必须用的工具总数，为 0 时无法判断"盲答"
    seg_error = None
    t0 = time.perf_counter()

    # 写权限开关在整轮内有效（不按段重置）：assist 模式下任何一段都不该写
    token = agent_tools.MODE.set(mode)
    session_token = agent_tools.SESSION.set(sid)
    try:
        for i, seg in enumerate(segments, 1):
            seg_sk = seg.skill if seg else None
            seg_goal = seg.goal if seg else ""
            seg_msg = message if i == 1 else _with_handoff(message, prev_title, handoff)
            res = _run_segment(
                sk=seg_sk, seg_goal=seg_goal, message=seg_msg, sid=sid, mode=mode,
                history_msgs=history_msgs, turn=turn, position=(i, len(segments)),
                first=(i == 1), trace_id=trace_id, on_event=emit)
            all_trace += res["trace"]
            inflight += res["inflight"]
            ctx_acc = _merge_ctx(ctx_acc, res["ctx"])
            prompt_ver = res["prompt_version"]
            reply = res["reply"]
            llm_calls += res.get("llm_calls") or 0
            if seg_sk:
                need_tools += len(seg_sk.require_tools or ())
                verdict = registry.check(seg_sk, [t["tool"] for t in res["trace"]],
                                         memory_hits=res["ctx"].get("memory_n", 0),
                                         require_tools=seg.require_tools)
                chain.append({
                    "name": seg_sk.name, "title": seg_sk.title, "goal": seg_goal,
                    "tools": list(seg_sk.tools), "tools_used": verdict["used"],
                    "status": ("ok" if (verdict["ok"] and reply)
                               else ("partial" if res["trace"] else "failed")),
                    "missing": verdict["missing"], "via_memory": verdict["via_memory"]})
            if res.get("error"):
                # 某段挂了就停：后面的段拿不到输入，硬跑等于让它编
                seg_error = res["error"]
                reply = f"{reply}\n\n（第 {i}/{len(segments)} 段失败，后续段落未执行）"
                break
            handoff = (reply or "")[:HANDOFF_CHARS]
            prev_title = seg_sk.title if seg_sk else "通用对话"
    finally:
        agent_tools.MODE.reset(token)
        agent_tools.SESSION.reset(session_token)

    raw_reply = reply
    if not reply:
        reply = "（模型没有返回内容）"

    _save(sid, "user", message, None, mode)
    _save(sid, "assistant", reply, all_trace, mode, {**ctx_acc, "trace_id": trace_id})

    duration_ms = int((time.perf_counter() - t0) * 1000)
    skill_info = _record_skills(sk, chain, all_trace, sid, reply, multi,
                                duration_ms, ctx_acc)

    # 回合事实：自进化的唯一数据源（详见 turn_facts 模块头）。
    # 落的是**原始回复**长度而不是兜底文案——"（模型没有返回内容）"有 11 个字，
    # 用它算长度会让"空回复"这条规则永远命中不了。
    status = "ok"
    if skill_info and skill_info.get("status") in ("partial", "failed"):
        status = skill_info["status"]
    if seg_error:
        status = "failed"
    turn_facts.record(
        session_id=sid, turn=turn, mode=mode,
        skill=sk.name if sk else None, segs=len(segments), need_tools=need_tools,
        msg_chars=len(message), ctx=ctx_acc, llm_calls=llm_calls,
        trace=all_trace, reply=raw_reply, duration_ms=duration_ms,
        status=status, error=seg_error, usage=pricing.snapshot(), trace_id=trace_id,
        max_llm_calls=sum(((seg.skill.max_iters if seg else MAX_ITERS) for seg in segments)))

    # 记忆抽取放最后、异步：用户已经拿到答案，抽取失败也只是"这轮没记住"，不影响对话。
    if config.MEMORY_EXTRACT:
        try:
            from ..memory import manager as memory_manager
            memory_manager.extract_async(message, reply, session_id=sid, turn=turn,
                                         trace_id=trace_id)
        except Exception:  # noqa: BLE001
            pass

    return {"session_id": sid, "turn": turn, "reply": reply, "trace": all_trace,
            "artifacts": _artifacts(all_trace), "iterations": len(all_trace),
            "prompt_version": prompt_ver, "mode": mode, "skill": skill_info,
            "skill_candidates": skill_board if not sk else None,
            "ctx": ctx_acc, "inflight": inflight}


def _short_args(args: dict) -> dict:
    """入参摘要：长文本只留前 40 字，避免把整份面经塞进 trace。"""
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str):
            out[k] = v[:40] + ("…" if len(v) > 40 else "")
        elif isinstance(v, list):
            out[k] = f"[{len(v)} 项]"
        else:
            out[k] = v
    return out
