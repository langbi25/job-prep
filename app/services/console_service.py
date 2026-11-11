"""对话控制台：Agent 的唯一入口。

设计取舍（和一个"聊天框套壳"的区别）：

1. **它不是 RAG 问答**，是一个会改系统的执行器。每一轮都能连续调多个工具
   （投喂→抽取→入池→起草→跑地图），最后用人话汇报——用户不再充当编排器。
2. **写权限有开关**（auto / assist）。assist 模式下写工具在工具层直接拒绝，
   不靠 prompt 里"请不要写"这种软约束——模型不听话时，硬闸门才是保障。
3. **轨迹落库**（`console_messages.trace_json`）。每一轮调了什么工具、入参、
   结果预览都能回放，出问题不用翻日志猜。
4. **不用流式**：一轮里可能有 3~4 次 LLM 往返 + 2 次抽取，流式的收益不如
   "把每一步做了什么摊开给你看"。前端按 trace 逐条渲染即可。
5. **上下文由 assembler 统一出**（2026-11-05 改造）：历史走"窗口 + 关键轮锚定"，
   窗口外的压成摘要，再按需检索注入，超预算按 检索→摘要→最旧轮次 降级；
   轮内工具结果单独裁（8 步 × 2600 字比历史更容易爆）。每轮的 token 账本落在
   `ctx_traces`，本轮返回值里的 `ctx` 就是这份账本。
"""
import datetime
import json
import random

from ..core import llm, prompts
from ..core.db import get_conn
from . import agent_tools, harness
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
    """
    conn = get_conn()
    try:
        cards = conn.execute("SELECT COUNT(*) AS n FROM kb_cards WHERE status='active'").fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_actions WHERE status='pending'").fetchone()["n"]
        due = conn.execute(
            "SELECT COUNT(*) AS n FROM kb_cards WHERE status='active' AND srs_due IS NOT NULL "
            "AND srs_due <= ?", (datetime.date.today().isoformat(),)).fetchone()["n"]
        iv = conn.execute(
            "SELECT id, company, position, created_at FROM feed_interviews "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        ing = conn.execute(
            "SELECT id, status FROM ingests ORDER BY created_at DESC LIMIT 1").fetchone()
        lines = [
            f"- 知识卡 {cards} 张（活卡），今日到期复习 {due} 张",
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


def chat(message: str, session_id: str | None = None, mode: str = "auto") -> dict:
    """跑一轮对话。返回 {session_id, reply, trace, artifacts, iterations, ctx}。

    AGENT 根 span 在这里起：一轮对话触发的 assemble/retriever/llm/tool 全部挂在
    同一棵 span 树下，trace_id 写进 ctx 账本——页面上能对上 Phoenix/Langfuse 里的瀑布。
    session/轮次：一场会话（session_id）= 多轮对话；一轮（turn）= 用户一句话触发的
    完整 agent 循环。轮次号 = 历史用户消息数 + 1，span 自动带 session.id/session.turn，
    Phoenix 按 session.id 聚成 Sessions，面板做「会话 → 轮次 → trace」下钻。
    """
    from ..observability import tracing as tr
    tr.set_scene(f"console:{mode}")
    # input.value 让 Phoenix 的 Sessions 显示"这轮用户问了什么"
    with tr.agent_span("agent.console", {"agent.mode": mode,
                                         "input.value": (message or "")[:2000],
                                         "input.mime_type": "text"}) as trace_id:
        result = _chat_inner(message, session_id, mode, trace_id)
        if isinstance(result.get("ctx"), dict):
            result["ctx"]["trace_id"] = trace_id
        return result


def _chat_inner(message: str, session_id: str | None, mode: str, trace_id: str = "") -> dict:
    """原对话循环体（观测无关）。"""
    message = (message or "").strip()
    if not message:
        raise ValueError("消息不能为空")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"单条消息最多 {MAX_MESSAGE_CHARS} 字，请分段发")

    mode = mode if mode in MODE_DESC else "auto"
    sid = session_id or new_session_id()
    history_msgs = _history(sid)
    # 轮次号：客观侧以"用户消息数"为准——一句"继续"也是新的一轮
    turn = sum(1 for m in history_msgs if m.get("role") == "user") + 1
    from ..observability import tracing as tr
    tr.set_session(sid, turn)

    system, user_msg, prompt_ver = prompts.render(
        "console_agent",
        mode_desc=MODE_DESC[mode],
        today=datetime.date.today().isoformat(),
        env=_env_snapshot(),
        message=message,
    )
    # 上下文统一走 assembler：窗口 + 锚定 + 摘要 + 检索，超预算按固定顺序降级
    ctx = assembler.assemble(sid, system, user_msg, history_msgs)
    messages = ctx.messages

    tools = harness.TOOLS + agent_tools.SCHEMAS
    impl = {**harness._IMPL, **agent_tools.IMPL}
    token = agent_tools.MODE.set(mode)

    trace: list[dict] = []
    reply = ""
    inflight: list[dict] = []
    try:
        for i in range(MAX_ITERS):
            msg = llm._chat_json(None, None, tools=tools, messages=messages)
            calls = msg.get("tool_calls") or []
            if calls:
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": calls})
                for tc in calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args)
                    except (ValueError, TypeError):
                        args = {}
                    fn = impl.get(name)
                    from ..observability import tracing as tr
                    with tr.span(f"tool.{name}", tr.KIND_TOOL, {
                            "tool.name": name, "tool.source": "console",
                            "input.value": agent_tools.json_safe(args)}) as s:
                        try:
                            result = fn(args) if fn else {"ok": False, "error": f"未知工具 {name}"}
                        except Exception as e:  # noqa: BLE001  工具炸了要让模型看到，而不是整轮失败
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        if s is not None:
                            s.set_attribute("output.value", agent_tools.json_safe(result)[:800])
                            tr.record_tool_action(s, result)
                    payload = agent_tools.json_safe(result)
                    trace.append({
                        "iter": i, "tool": name,
                        "args": _short_args(args),
                        "ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
                        "preview": payload[:300],
                    })
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": payload})
                # 工具结果一轮轮堆进去最容易爆：进入下一轮前先按预算裁最旧的
                inflight.append(assembler.enforce_inflight(messages))
                continue

            reply = (msg.get("content") or "").strip()
            break
        else:
            reply = ("这一步我没在 8 轮工具调用内做完（通常是材料太大或工具反复报错）。"
                     "我已经执行的步骤在下方面板里，你可以让我分段继续。")
    finally:
        agent_tools.MODE.reset(token)

    if not reply:
        reply = "（模型没有返回内容）"

    _save(sid, "user", message, None, mode)
    _save(sid, "assistant", reply, trace, mode, {**ctx.as_dict(), "trace_id": trace_id})
    return {"session_id": sid, "turn": turn, "reply": reply, "trace": trace,
            "artifacts": _artifacts(trace), "iterations": len(trace),
            "prompt_version": prompt_ver, "mode": mode,
            "ctx": ctx.as_dict(), "inflight": inflight}


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
