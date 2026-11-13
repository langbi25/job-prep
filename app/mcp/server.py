"""job-prep 的 MCP server：把个人知识库变成任何 LLM 宿主都能接的标准接口。

    python -m app.mcp.server                 # stdio（Claude Desktop / Cursor 本地接入）
    python -m app.mcp.server --http --port 8766   # streamable HTTP（远程，需 MCP_TOKEN）

三个原语各有分工，别都塞进 tools：
- tools：模型决定调用（9 个，读 6 写 3）；
- resources：客户端预取，不耗决策（kb://card/{id}、kb://brief/today）；
- prompts：用户显式触发（复盘、建卡）。

传输层的取舍：stdio 的进程边界就是权限边界，本地免鉴权；HTTP 暴露到网络，
默认要求 Bearer token——不设 token 就只监听 127.0.0.1，避免"图方便"变成公网裸奔。
"""
import argparse
import asyncio
import json
import os
import sys

from mcp.server.mcpserver import MCPServer

from ..core.db import init_db
from ..core import spec
from ..services import review_service
from . import gateway, tools


def _instructions() -> str:
    return (
        "这是候选人的个人求职知识库（后端 / 大数据 / Agent 方向的面经与知识卡）。\n"
        "回答「我记过什么」先 search_kb，需要全文再 get_card；回答「我今天学什么」先 get_daily_brief；\n"
        "回答「我哪里弱」用 get_weak_topics；需要真实考点分布用 get_top_questions。\n"
        "写操作（draft_cards / ingest_text）只产生待审批草稿，不会直接落库，调用前先跟用户确认。\n"
        "检索为空代表知识缺口，如实说「没记过」，不要用通用知识冒充个人笔记。"
    )


mcp = MCPServer(name="job-prep", version="1.0.0", instructions=_instructions())

for _fn in tools.REGISTERED:
    mcp.add_tool(_fn, description=gateway.SPECS[_fn.__name__].summary)


# ---------------------------------------------------------------- resources
@mcp.resource("kb://card/{card_id}", name="knowledge_card",
              description="单张知识卡的完整内容（JSON）", mime_type="application/json")
def card_resource(card_id: str) -> str:
    return gateway.invoke("get_card", {"card_id": card_id}, client="resource").payload


@mcp.resource("kb://brief/today", name="daily_brief",
              description="今日学习概况（到期卡/错题/面试均分/薄弱卡）", mime_type="application/json")
def brief_resource() -> str:
    return gateway.invoke("get_daily_brief", {}, client="resource").payload


# ---------------------------------------------------------------- prompts
@mcp.prompt(name="mock_review", description="把最近一场模拟面试按面试官视角复盘")
def mock_review_prompt(direction: str = spec.default()) -> str:
    return (
        f"请以资深面试官视角复盘我最近一场 {direction} 方向的模拟面试。\n"
        "先用 get_weak_topics 取我的薄弱主题，再用 search_kb 核对对应知识卡里的要点，"
        "然后按「答对了什么 / 缺了什么 / 下次怎么答」三段给出复盘，指出具体卡片 id 作为依据。\n"
        "不要泛泛而谈，不要编造我没记过的内容。"
    )


@mcp.prompt(name="card_from_question", description="针对一道无卡题起草知识卡")
def card_from_question_prompt(question_id: str) -> str:
    return (
        f"针对题目 {question_id}：先用 get_top_questions / search_kb 确认它有没有已覆盖的卡。\n"
        "如果确实没有覆盖，调用 draft_cards 起草一张卡（只会产生待审批动作），"
        "然后用 3 条要点 + 1 条常见坑的口吻告诉我你写了什么，等我确认后再去审批中心通过。"
    )


# ---------------------------------------------------------------- 传输层
class BearerGuard:
    """HTTP 传输的最小鉴权：设了 MCP_TOKEN 就必须带 Bearer。

    没设 token 时构造函数会拒绝非 127.0.0.1 绑定——宁可起不来，也不要静默裸奔。
    """

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if headers.get("authorization") != f"Bearer {self.token}":
                body = json.dumps({"error": "unauthorized"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def main() -> None:
    ap = argparse.ArgumentParser(description="job-prep MCP server")
    ap.add_argument("--http", action="store_true", help="用 streamable HTTP 而不是 stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()

    init_db()  # 工具要读库；stdio 由宿主拉起时也必须自己保证 schema 就绪
    if not args.http:
        mcp.run("stdio")
        return

    token = os.environ.get("MCP_TOKEN", "")
    if not token and args.host not in ("127.0.0.1", "localhost"):
        print("拒绝启动：绑定非本地地址必须设置 MCP_TOKEN", file=sys.stderr)
        sys.exit(2)

    import uvicorn
    app = BearerGuard(mcp.streamable_http_app(), token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
