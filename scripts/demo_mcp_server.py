"""最小外部 MCP server：验证「网关作为 client」这条路能真正跑通。

它不是玩具，有两个用处：

1. `mcp_servers.json` 的参考实现——照这个形状写，include 就对了；
2. `tests/test_external.py` 的真实对端——走真 stdio 协议而不是 mock，
   这样握手、inputSchema 传递、错误包这些只有跨进程才暴露的问题能被测到。

    python scripts/demo_mcp_server.py     # 由宿主按 stdio 拉起，不用手动跑
"""
from typing import Annotated

from pydantic import Field

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="demo", version="0.1.0", instructions="接入自检用的最小 server")


def echo(text: Annotated[str, Field(description="要回显的文本")]) -> str:
    """原样返回输入。用来确认参数在跨进程传递中没有被改写、截断或转义。"""
    return text


def now() -> str:
    """返回本机当前时间（ISO 8601，秒级）。用来确认零参数工具也能被正确调用。"""
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def explode(reason: Annotated[str, Field(description="错误原因")] = "boom") -> str:
    """故意抛异常。用来确认外部工具失败会变成可读文本，而不是炸穿 Agent 循环。"""
    raise ValueError(reason)


mcp.add_tool(echo, description="原样返回输入，用于验证参数传递")
mcp.add_tool(now, description="返回服务器当前时间")
mcp.add_tool(explode, description="故意失败，用于验证错误处理")

if __name__ == "__main__":
    mcp.run("stdio")
