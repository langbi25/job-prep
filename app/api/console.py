import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ..core import config
from ..context import assembler, summarizer
from ..services import console_service
from .sse import hub, sse_response

router = APIRouter(prefix="/api/console", tags=["console"])

STREAM_IDLE_TIMEOUT_S = 300   # 单轮 agent 的硬上限兜底（LLM 单次 90s × 多轮），防客户端死等


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    mode: str = "auto"          # auto / assist
    skill: str | None = None    # 指定技能（不传=自动路由）


@router.post("/chat")
def chat(req: ChatRequest, background: BackgroundTasks):
    """一轮对话：Agent 自己决定调哪些工具，写完汇报。写权限由 mode 决定。"""
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，对话控制台不可用（见 .env）")
    try:
        res = console_service.chat(req.message, req.session_id, req.mode, req.skill)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # 摘要压缩放后台：它要走一次 LLM，让用户为一件看不见的事多等 2 秒不值得。
    # 拷贝失败不影响本轮结果，下一轮还会再试（covered_to_id 保证不重复压）。
    background.add_task(summarizer.summarize, res["session_id"])
    return res


@router.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """同 /chat，但过程以 SSE 推送：首字从 9s 级提前到模型的首 token。

    为什么后台线程 + 缓冲而不是直接在生成器里跑 chat()：
    chat() 的事件是回调式的（on_event），SSE 生成器需要的是可迭代序列，缓冲就是桥。
    更关键的是**生成器会被关掉**（客户端断开），而 agent 不该跟着停——它已经花了
    钱和模型往返，跑到一半丢掉最亏。所以任务挂在独立线程上写缓冲，谁读都可以。

    重连续传：响应头 X-Stream-Id 给出流 id，GET /chat/stream/{id}?after=<seq>
    从断点补齐。切标签页、锁屏、网络闪断回来都能接上。

    事件协议（data: {json, seq}）：
      segment  段开始 {index,total,skill,title,goal}
      delta    回复增量 {text}——带 tool_calls 的轮次也会先吐 delta，
               客户端收到 turn_end(has_tools=true) 时应丢弃已累计的 delta
      tool     工具结果 {tool,ok,error,cached}
      turn_end 本轮结束 {has_tools}
      done     完整结果，结构同 POST /chat 的返回体
      error    {message}
    """
    if not config.LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY 未配置，对话控制台不可用（见 .env）")

    stream_id, bus = hub.create("st")

    def job():
        # 首轮用户不传 session_id，真正的 id 在 chat() 的返回里——不能就地用 req.session_id
        sid = req.session_id
        try:
            res = console_service.chat(req.message, req.session_id, req.mode, req.skill,
                                       on_event=bus.publish)
            sid = res.get("session_id") or sid
            bus.publish({"type": "done", "result": res})
        except Exception as e:  # noqa: BLE001  错误也要以事件形式到达客户端
            bus.publish({"type": "error", "message": f"{type(e).__name__}: {e}"[:500]})
        finally:
            bus.close()
            # 摘要压缩：与 /chat 同语义，但不能用 BackgroundTasks——它在响应结束后才跑，
            # 而流式响应在第一次 yield 时就"已开始"，时序不可靠。这里起独立线程更直接。
            if sid:
                threading.Thread(target=_summarize_quietly, args=(sid,), daemon=True).start()

    threading.Thread(target=job, daemon=True).start()
    return sse_response(bus, stream_id)


def _summarize_quietly(session_id: str) -> None:
    try:
        summarizer.summarize(session_id)
    except Exception:  # noqa: BLE001  压缩失败不影响本轮结果，下一轮还会再试
        pass


@router.get("/chat/stream/{stream_id}")
def resume_stream(stream_id: str, after: int = 0):
    """断线续传：从 after 之后补齐事件。

    after 传客户端收到过的最后一个 seq。传 0 等于从头回放——既用于重连，
    也用于"这一轮跑完但我没看到"的回看。
    """
    bus = hub.get(stream_id)
    if bus is None:
        raise HTTPException(404, "这个流已过期（保留 10 分钟），请重新发送这一条")
    return sse_response(bus, stream_id, after)


@router.get("/skills")
def skills():
    """技能清单 + 实际使用统计。面板用来回答"有哪几个技能、哪个从来没跑过"。"""
    from ..skills import registry
    return {"skills": registry.catalog(), "stats": registry.stats()}


@router.get("/history")
def history(session_id: str):
    return {"session_id": session_id, "messages": console_service.history(session_id)}


@router.get("/sessions")
def sessions(limit: int = 20):
    return console_service.sessions(limit)


@router.get("/ctx/{session_id}")
def ctx_traces(session_id: str, limit: int = 20):
    """上下文账本：每轮组装的 token 分布、降级记录。"""
    return {"session_id": session_id,
            "traces": assembler.recent_traces(session_id, limit),
            "summary": summarizer.stats(session_id)}
