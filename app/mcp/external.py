"""外部 MCP server 接入（client 方向）：让内置 Agent 能用别人写好的工具。

此前这一层只有 server 方向——我们把知识库暴露给 Claude Desktop，但自己不去连别人的
MCP server，所以「网关」是单向的。这个模块补上另一半。

五条取舍，都是围绕「第三方代码不可信」来的：

1. **默认关闭**：读 `mcp_servers.json`，文件不存在就是空配置——不建连接、不耗时间。
   不能设计成"发现了就自动接"，那等于让一个配置文件的存在本身成为权限授予。
2. **白名单优先**：每个 server 必须显式写 `allow`，没列出的工具不暴露。
   外部工具能干什么我们无从判断（删文件、发请求、读别的目录），默认全开就是
   把未审计的入口直接塞进模型的工具列表。写 `["*"]` 才是"全开"，且要打字打出来。
3. **名字加前缀** `{server}__{tool}`：两个 server 都有 `read_file` 时不会互相覆盖。
   用双下划线而不是点号——OpenAI function name 只允许 `[a-zA-Z0-9_-]`。
4. **懒加载 + 失败隔离**：真正需要用到时才连接；连不上就把该 server 标记为不可用并跳过，
   绝不让一个起不来的第三方进程把 Agent 主流程拖住或带崩。
5. **记账不例外**：调用落 `mcp_calls`（`client=ext:<server>`），和本地工具同一张表。
   否则"外部工具被谁调了多少次、失败几次"永远查不清——这是可观测性的基本盘。

配置示例见 `mcp_servers.example.json`。
"""
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = ROOT / "mcp_servers.json"

CONNECT_TIMEOUT = float(os.getenv("MCP_EXT_CONNECT_TIMEOUT", "20") or 20)
CALL_TIMEOUT = float(os.getenv("MCP_EXT_CALL_TIMEOUT", "60") or 60)

WILDCARD = "*"


@dataclass
class ServerConf:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    allow: list[str] = field(default_factory=list)
    read_only: set = field(default_factory=set)


def _conf_path() -> Path:
    raw = os.getenv("MCP_SERVERS_FILE", "").strip()
    return Path(raw) if raw else DEFAULT_FILE


def load_conf() -> dict[str, ServerConf]:
    """读配置。任何结构问题都退回空配置并打一行说明——配置错不该让服务起不来，
    但也绝不能"猜一个意思"继续跑（猜错等于授予了没打算给的权限）。"""
    path = _conf_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[mcp.ext] 配置文件解析失败，已跳过全部外部 server: {e}", file=sys.stderr)
        return {}

    out: dict[str, ServerConf] = {}
    for name, c in (raw.get("servers") or {}).items():
        if not isinstance(c, dict) or not c.get("command"):
            print(f"[mcp.ext] 跳过 {name}：缺少 command", file=sys.stderr)
            continue
        allow = [str(x) for x in (c.get("allow") or [])]
        if not allow:
            # 没写 allow 视为"不暴露"，而不是"全部暴露"。这个默认值的方向很关键：
            # 配置写漏是常见事故，两种默认值的事故后果差一个量级。
            print(f"[mcp.ext] {name} 未声明 allow，不暴露任何工具", file=sys.stderr)
        # PATH 里的 python 通常是系统解释器，不带 mcp/pydantic——实测就是这样起的
        # 子进程直接 "ModuleNotFoundError: pydantic"。写 "python" 的本意是"用当前这个
        # 解释器"，所以这里照这个意思翻译；别的命令（npx/node/uvx）原样透传。
        cmd = str(c["command"])
        if cmd in ("python", "python3", "py"):
            cmd = sys.executable
        out[name] = ServerConf(
            name=name,
            command=cmd,
            args=[str(x) for x in (c.get("args") or [])],
            env={str(k): str(v) for k, v in (c.get("env") or {}).items()},
            allow=allow,
            read_only={str(x) for x in (c.get("read_only") or [])},
        )
    return out


class _Server:
    """一个外部 server 的连接与工具投影。连接失败只影响自己。"""

    def __init__(self, conf: ServerConf, box_factory: Callable | None = None):
        self.conf = conf
        self._box = None
        self._failed = False
        self._box_factory = box_factory
        self._tools: list[dict] = []
        self.error: str | None = None

    def _name_ok(self, tool: str) -> bool:
        return WILDCARD in self.conf.allow or tool in self.conf.allow

    def ensure(self) -> bool:
        """建立连接并缓存工具清单。失败后不再重试——重试只会让每次对话都卡在同一个坏 server 上。"""
        if self._box is not None:
            return True
        if self._failed:
            return False
        try:
            if self._box_factory is not None:
                box = self._box_factory(self.conf)
            else:
                from .client import SyncMCPToolbox
                box = SyncMCPToolbox(command=self.conf.command, args=self.conf.args,
                                     env=self.conf.env, timeout=CALL_TIMEOUT)
            self._tools = [
                {**t, "function": {**t["function"],
                                   "name": f"{self.conf.name}__{t['function']['name']}"}}
                for t in box.openai_tools() if self._name_ok(t["function"]["name"])]
            self._box = box
            return True
        except Exception as e:  # noqa: BLE001  第三方 server 起不来不是我们的错，但必须留痕
            self.error = f"{type(e).__name__}: {e}"[:300]
            self._failed = True
            print(f"[mcp.ext] {self.conf.name} 连接失败，本轮跳过: {self.error}", file=sys.stderr)
            return False

    def schemas(self) -> list[dict]:
        return list(self._tools) if self.ensure() else []

    def read_only(self, tool: str) -> bool:
        return tool in self.conf.read_only or WILDCARD in self.conf.read_only

    def call(self, tool: str, args: dict) -> tuple[bool, str]:
        if not self.ensure():
            return False, json.dumps(
                {"error": f"外部 server {self.conf.name} 不可用: {self.error}"}, ensure_ascii=False)
        return self._box.call_raw(tool, args)


_STATE: dict[str, _Server] | None = None


def _servers() -> dict[str, _Server]:
    global _STATE
    if _STATE is None:
        _STATE = {n: _Server(c) for n, c in load_conf().items()}
    return _STATE


def reset() -> None:
    """清掉连接与缓存（测试用；也用于运行时改了配置后重新加载）。"""
    global _STATE
    for s in (_STATE or {}).values():
        try:
            if s._box is not None:
                s._box.close()
        except Exception:  # noqa: BLE001
            pass
    _STATE = None


def schema_map() -> dict[str, dict]:
    """`{server}__{tool}` → 该工具的 OpenAI schema。连不上的 server 不出现在这里。"""
    out: dict[str, dict] = {}
    for s in _servers().values():
        for t in s.schemas():
            out[t["function"]["name"]] = t
    return out


def implementations() -> dict[str, Callable[[dict], str]]:
    """给 harness_admin.all_tools() 合并用：name → 同步可调用。

    返回值是**文本**而不是对象，和 `SyncMCPToolbox.call` 同一契约——dispatch 的结果
    截断与 `_ref` 分片逻辑都按文本处理，这里换个形状会悄悄绕过那层保护。
    """
    out: dict[str, Callable[[dict], str]] = {}
    for server_name, s in _servers().items():
        for t in s.schemas():
            full = t["function"]["name"]
            bare = full.split("__", 1)[1]

            def _call(args: dict, _s=s, _bare=bare, _full=full) -> str:
                return invoke(_full, args, server=_s, tool=_bare)

            out[full] = _call
    return out


def read_only_names() -> set[str]:
    return {t["function"]["name"] for s in _servers().values()
            for t in s.schemas() if s.read_only(t["function"]["name"].split("__", 1)[1])}


def invoke(full: str, args: dict, *, server: _Server | None = None,
           tool: str | None = None) -> str:
    """调一次外部工具并记账。返回文本（失败也是可读文本，不抛）。"""
    if server is None:
        server, tool = _split(full)
        if server is None:
            return json.dumps({"error": f"未配置的外部工具: {full}"}, ensure_ascii=False)
    assert tool is not None
    t0 = time.perf_counter()
    try:
        ok, text = server.call(tool, args or {})
    except Exception as e:  # noqa: BLE001
        ok, text = False, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    latency = int((time.perf_counter() - t0) * 1000)
    # 记账用完整名（demo__echo），判读写要用裸名（echo）——配置里的 read_only 列的是裸名。
    # 这两个名字差一个前缀，混用会让只读工具被记成写工具，面板上的读写比例直接失真。
    _log(full, tool, args or {}, ok, text, latency, server)
    return text


def _split(full: str) -> tuple[_Server | None, str | None]:
    name, _, tool = full.partition("__")
    return _servers().get(name), tool or None


def _log(full: str, bare: str, args: dict, ok: bool, text: str, latency_ms: int,
         server: _Server) -> None:
    """走 gateway 的同一张表。日志失败不影响调用——观测是副产物，不是前置条件。"""
    try:
        from . import gateway
        gateway._log(tool=full, client=f"ext:{server.conf.name}",
                     args_json=gateway._compact_args(args),
                     status="ok" if ok else "error",
                     error=None if ok else text[:300],
                     latency_ms=latency_ms, payload=text,
                     read_only=server.read_only(bare))
    except Exception:  # noqa: BLE001
        pass
