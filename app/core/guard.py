"""API 鉴权 + 写操作审计（差距清单"鉴权+审计"项）。

为什么手写 ASGI 中间件而不用 BaseHTTPMiddleware：
项目有 SSE 端点（/api/agent/orchestrate/stream），BaseHTTPMiddleware 的响应包装
会破坏流式传输（缓冲/断流），手写 ASGI 透传零干扰——与 mcp/server.py 的
TokenMiddleware 同款做法，一个项目里鉴权只有一种写法。

鉴权边界刻意收窄：只拦 /api/*。
- /health 放行：容器 healthcheck 每 30s 打一次，不能要求它带 token；
- 前端静态页放行：页面本身没有数据，数据全在 /api 后面——token 是"谁能调业务
  API"的边界，不是"谁能打开页面"的边界；
- /docs、/openapi.json 放行：个人内网，暴露 API 结构换取调试便利，划算。
- MCP 不经过这里：stdio 靠进程边界，HTTP 侧有自己的 MCP_TOKEN。

token 来源（APP_TOKEN 为空 = 整个模块退化成只写审计，向后兼容本地脚本/测试）。

审计只记两类，GET 不记（个人项目读没有追责价值）；body 一律不落库——
审计表不该成为第二个简历副本，排查业务问题去 spans 表。

actor 语义（单 token 设计）：带有效 token = owner；401 = anonymous；
鉴权关闭时记 local，明确表示"没有身份边界在生效"，而不是留下一个看似
有主其实无主的记录。
"""
from __future__ import annotations

import datetime
import time

from . import config
from .db import get_conn

_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _extract_token(headers) -> str | None:
    """从 ASGI headers（小写键的 (k, v) 序列）取 token。"""
    raw = {}
    for k, v in headers:
        key = k.decode("latin-1").lower()
        if key in ("authorization", "x-api-token") and key not in raw:
            raw[key] = v.decode("latin-1").strip()
    tok = raw.get("x-api-token")
    if not tok:
        auth = raw.get("authorization", "")
        # 容忍三种写法：Bearer xxx / bearer xxx / 裸 token
        parts = auth.split(" ", 1)
        tok = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else auth
    return tok or None


def _audit(action: str, path: str, query: str, ip: str, status: int,
           duration_ms: float, actor: str) -> None:
    """审计写入。失败绝不影响请求本身——审计挂了业务照跑，和 Langfuse 双写同一原则。"""
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, path, query, ip, status, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.datetime.now().isoformat(timespec="milliseconds"), actor,
             action, path, query or None, ip or None, status, round(duration_ms, 1)))
        conn.commit()
    except Exception:  # noqa: BLE001  表未建/库锁竞争/测试环境无库，都吞掉
        pass


class GuardMiddleware:
    """挂载：app.add_middleware(GuardMiddleware)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        method = scope["method"].upper()
        guarded = path.startswith("/api") and bool(config.APP_TOKEN)

        started = time.perf_counter()
        if guarded:
            supplied = _extract_token(scope.get("headers") or [])
            if supplied != config.APP_TOKEN:
                ip = (scope.get("client") or ("?", 0))[0]
                query = scope.get("query_string", b"").decode("latin-1", "ignore")
                _audit(method, path, query, ip, 401, 0.0, "anonymous")
                body = '{"detail":"缺少或无效的访问令牌（X-API-Token 或 Authorization: Bearer）"}'.encode("utf-8")
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                        (b"www-authenticate", b"Bearer"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return

        # 包装 send 抓响应码；请求/响应照常透传，SSE 不受影响。
        status_holder = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if method in _WRITE_METHODS or status_holder["code"] == 401:
                duration = (time.perf_counter() - started) * 1000
                query = scope.get("query_string", b"").decode("latin-1", "ignore")
                ip = (scope.get("client") or ("?", 0))[0]
                actor = "owner" if guarded else ("local" if not config.APP_TOKEN else "owner")
                _audit(method, path, query, ip, status_holder["code"], duration, actor)
