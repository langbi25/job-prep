"""MCP stdio 冒烟：用官方 SDK 客户端连本地 server，走 initialize → tools/list → tools/call。

等价于 Claude Desktop / Cursor 的接入路径。协议层的问题（握手、schema 形状、
stdio 被 print 污染）在这里暴露最快——比在宿主里点半天强。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as ses:
            init = await ses.initialize()
            print("server:", init.server_info.name, init.server_info.version,
                  "| protocol:", init.protocol_version)
            tools = await ses.list_tools()
            print("tools:", len(tools.tools), [t.name for t in tools.tools])
            prompts = await ses.list_prompts()
            print("prompts:", [p.name for p in prompts.prompts])
            res = await ses.call_tool("get_daily_brief", {})
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            data = json.loads(text) if text.startswith("{") else {}
            print("call get_daily_brief: ok=", not res.is_error,
                  "| keys:", list(data)[:6], "| chars:", len(text))
            res2 = await ses.call_tool("search_kb", {"query": "Flink 反压", "top_k": 2})
            text2 = "".join(c.text for c in res2.content if getattr(c, "type", "") == "text")
            hits = json.loads(text2).get("hits", []) if text2.startswith("{") else []
            print("call search_kb: hits=", len(hits), "| first=",
                  hits[0]["card_id"] if hits else None)


if __name__ == "__main__":
    asyncio.run(main())
