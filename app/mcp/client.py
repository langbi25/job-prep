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
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


class MCPToolbox:
    """stdio 连本地 MCP server，暴露 OpenAI function-calling 形状的工具清单。"""

    def __init__(self, command: str | None = None, args: list[str] | None = None):
        self.params = StdioServerParameters(
            command=command or sys.executable,
            args=args or ["-m", "app.mcp.server"],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self._stdio = None
        self._session: ClientSession | None = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "MCPToolbox":
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


async def _demo(question: str) -> None:
    """最小验证：取工具清单 → 打一次 → 打印协议层可见的东西。"""
    async with MCPToolbox() as box:
        print("tools:", box.names)
        ok, text = await box.call("get_daily_brief", {})
        print("call ok=", ok, "chars=", len(text))


if __name__ == "__main__":
    asyncio.run(_demo(" ".join(sys.argv[1:]) or "今天我该复习什么"))
