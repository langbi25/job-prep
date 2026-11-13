"""MCP 客户端桥：让本地 harness 不直接调函数，而是通过 MCP 协议拿工具、调工具。

为什么需要这层：
- 内置 harness 是「进程内直连」——工具注册表在内存里，调 `_IMPL[name](args)`。
  快，但验证不到协议层：schema 形状、握手、错误包、传输。
- 走 MCP 客户端后，工具清单来自 `tools/list`、调用走 `tools/call`，
  和 Claude Desktop 看到的完全一致。同一个 Agent 循环，换一个工具来源而已。

代价是每次调用多一次进程内 IPC（stdio 管道），单次约几百微秒到几毫秒，
对"查一次知识库要 30ms"这个量级可以忽略；换来的是可替换性——宿主可以是
本地 Python、Claude Desktop、或任何别的 MCP 客户端。
"""
import asyncio
import concurrent.futures
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


class MCPToolbox:
    """stdio 连本地 MCP server，暴露 OpenAI function-calling 形状的工具清单。"""

    def __init__(self, command: str | None = None, args: list[str] | None = None,
                 env: dict | None = None):
        # env 用于接外部 server（app/mcp/external.py）：第三方进程可能需要自己的
        # token / 路径变量，但必须继承当前进程环境——否则 PATH 里的 npx/node 找不到。
        self.params = StdioServerParameters(
            command=command or sys.executable,
            args=args or ["-m", "app.mcp.server"],
            env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        )
        self._stdio = None
        self._session: ClientSession | None = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "MCPToolbox":
        # 注意：子进程的 stderr 直接继承父进程，无法在这里加前缀。MCP SDK 的
        # stdio_client(errlog=...) 最终走 anyio.open_process(stderr=errlog)，
        # 需要的是**文件描述符**——传一个 TextIO 包装器时 fileno() 会被转发到
        # 父进程的 fd 2，等于没包。所以外部 server 的 traceback 会原样出现在
        # 我们的日志里，靠路径区分归属（site-packages/mcp/** = 第三方输出）。
        self._stdio = stdio_client(self.params)
        read, write = await self._stdio.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        self._tools = (await self._session.list_tools()).tools
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.__aexit__(*exc)
        if self._stdio:
            await self._stdio.__aexit__(*exc)

    def openai_tools(self) -> list[dict]:
        """转成 DeepSeek / OpenAI function calling 需要的形状。

        MCP 的 inputSchema 是标准 JSON Schema，OpenAI 的 parameters 也是——
        所以这是个纯搬迁，不需要"翻译"，这也是当初选 MCP 的原因之一。
        """
        return [{"type": "function", "function": {
            "name": t.name, "description": t.description or "", "parameters": t.input_schema,
        }} for t in self._tools]

    @property
    def names(self) -> list[str]:
        return [t.name for t in self._tools]

    async def call(self, name: str, args: dict) -> tuple[bool, str]:
        """调一次工具，返回 (是否成功, 文本结果)。协议层错误也要变成可读文本，
        不能让异常直接炸穿 Agent 循环——循环里失败一次就整个任务挂掉，体验很差。"""
        try:
            res = await self._session.call_tool(name, args)
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            return (not res.is_error), text
        except Exception as e:  # noqa: BLE001
            return False, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


class SyncMCPToolbox:
    """同步门面：让**同步**代码（`app/services/harness.py` 的 Agent 循环）用同一个 MCP 会话。

    为什么不在每次工具调用时 `asyncio.run(...)`：
    - 每次新建会话都要重新握手 + `tools/list`，一个 Agent 循环有 4~6 次调用，开销翻倍；
    - stdio 会话背后是一个子进程，用完必须关，否则一次运行漏一个 python 进程。

    为什么整个生命周期必须待在**同一个协程**里：
    - MCP 的 stdio 客户端内部用 anyio 的 cancel scope，要求 enter/exit 在同一任务，
      跨任务退出会抛 "Attempted to exit cancel scope in a different task"。
      所以开一个后台线程跑常驻事件循环，生命周期协程只此一个，请求走队列进去。
    """

    def __init__(self, timeout: float = 60.0, command: str | None = None,
                 args: list[str] | None = None, env: dict | None = None):
        self._timeout = timeout
        self._params = {"command": command, "args": args, "env": env}
        self._loop = asyncio.new_event_loop()
        self._req: asyncio.Queue | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name="mcp-stdio")
        self._thread.start()
        ready: concurrent.futures.Future = concurrent.futures.Future()
        asyncio.run_coroutine_threadsafe(self._serve(ready), self._loop)
        try:
            self._tools: list[Any] = ready.result(self._timeout)
        except Exception:
            # 握手失败（外部 server 秒退、依赖缺失、命令写错）时必须把后台线程和事件循环
            # 关掉再抛：不关的话循环一直转、子进程不收尸，还会留下
            # "Task exception was never retrieved" 的噪音，把真正的错误埋掉。
            self.close()
            raise

    async def _serve(self, ready: concurrent.futures.Future) -> None:
        req: asyncio.Queue = asyncio.Queue()
        self._req = req
        try:
            async with MCPToolbox(**self._params) as box:
                ready.set_result(box.openai_tools())
                while True:
                    item = await req.get()
                    if item is None:  # 关闭信号
                        return
                    name, args, fut = item
                    try:
                        ok, text = await box.call(name, args)
                    except Exception as e:  # noqa: BLE001  单次失败不能带走整个会话
                        ok, text = False, json.dumps(
                            {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                    if not fut.done():
                        fut.set_result((ok, text))
        except Exception as e:  # noqa: BLE001  握手失败要传出去，别让主线程傻等
            if not ready.done():
                ready.set_exception(e)
        finally:
            self._done.set()

    def openai_tools(self) -> list[dict]:
        return self._tools

    @property
    def names(self) -> list[str]:
        return [t["function"]["name"] for t in self._tools]

    def call(self, name: str, args: dict) -> str:
        """返回**文本**结果，和 `_LocalToolbox.call` 同一契约。

        MCP 协议层是 (is_error, text) 二元组，但 harness 的循环只认一段文本：
        直接把元组塞进 messages，httpx 会把它序列化成 [true, "..."] 数组，
        DeepSeek 报 422 "invalid type: boolean true, expected ChatCompletionRequestContentBlock"。
        —— 接口形状不一致不会在类型检查里暴露，只会在服务端反序列化时报错。
        失败也压成可读文本，不抛异常：一次工具失败不该炸穿整个 Agent 循环。
        """
        ok, text = self.call_raw(name, args)
        return text[:6000] if ok else text

    def call_raw(self, name: str, args: dict) -> tuple[bool, str]:
        if self._req is None:
            return False, json.dumps({"error": "MCP 会话未就绪"}, ensure_ascii=False)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._req.put_nowait, (name, dict(args or {}), fut))
        try:
            return fut.result(self._timeout)
        except concurrent.futures.TimeoutError:
            return False, json.dumps({"error": f"工具 {name} 超时（>{self._timeout}s）"},
                                     ensure_ascii=False)

    def close(self) -> None:
        if self._req is not None:
            self._loop.call_soon_threadsafe(self._req.put_nowait, None)
            self._done.wait(5)
        else:
            # 会话没建起来（_serve 在第一次 await 之前就挂了）：给它半秒收尾，
            # 不要死等 5 秒——失败路径上每一秒都算在用户的等待时间里。
            self._done.wait(0.5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()

    def __enter__(self) -> "SyncMCPToolbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


async def _demo(question: str) -> None:
    """最小验证：取工具清单 → 打一次 → 打印协议层可见的东西。"""
    async with MCPToolbox() as box:
        print("tools:", box.names)
        ok, text = await box.call("get_daily_brief", {})
        print("call ok=", ok, "chars=", len(text))


if __name__ == "__main__":
    asyncio.run(_demo(" ".join(sys.argv[1:]) or "今天我该复习什么"))
