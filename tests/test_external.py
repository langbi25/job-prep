"""外部 MCP server 接入测试。

对端是 `scripts/demo_mcp_server.py`——**真 stdio 协议**，不是 mock。
这一层要验的恰恰是跨进程才暴露的问题：握手、inputSchema 传递、参数是否被改写、
外部异常是否被压成可读文本、连接失败是否只影响自己。

不依赖 pytest 之外的插件；直接 `python tests/test_external.py` 也能跑（部分用例需要 pytest 的 fixture）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.core import db  # noqa: E402
from app.mcp import external  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEMO = str(ROOT / "scripts" / "demo_mcp_server.py")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


@pytest.fixture
def write_conf(tmp_path, monkeypatch):
    """写一份临时 mcp_servers.json 并指向它。"""

    def _w(servers: dict) -> Path:
        p = tmp_path / "servers.json"
        p.write_text(json.dumps({"servers": servers}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("MCP_SERVERS_FILE", str(p))
        external.reset()
        return p

    yield _w
    external.reset()


def _demo(allow=None, read_only=None, **over):
    c = {"command": "python", "args": [DEMO],
         "allow": ["echo", "now", "explode"] if allow is None else allow,
         "read_only": ["echo", "now"] if read_only is None else read_only}
    c.update(over)
    return {"demo": c}


def test_no_config_file_means_zero_connection(tmp_path, monkeypatch):
    """没有配置文件就该什么都不做——不连接、不报错、不耗时间。

    这一条是"默认关闭"的实际保证：不能因为某个目录下碰巧有个 json 就把
    未审计的工具塞进模型的工具列表。
    """
    monkeypatch.setenv("MCP_SERVERS_FILE", str(tmp_path / "nope.json"))
    external.reset()
    assert external.load_conf() == {}
    assert external.schema_map() == {}
    assert external.implementations() == {}


def test_broken_config_is_skipped_not_fatal(write_conf, tmp_path):
    """配置错不该让服务起不来，但也不能猜一个意思继续跑。"""
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    import os
    os.environ["MCP_SERVERS_FILE"] = str(p)
    external.reset()
    assert external.load_conf() == {}

    # 缺 command 的条目单独跳过，不连累同文件里正常的条目
    write_conf({"nope": {"args": []}, "demo": {"command": "python", "args": [DEMO],
                                               "allow": ["echo"]}})
    conf = external.load_conf()
    assert set(conf) == {"demo"}


def test_python_command_resolves_to_current_interpreter(write_conf):
    """配置里写 "python" 时用当前解释器。

    实测踩过：PATH 里的 python 是系统解释器，没装 mcp/pydantic，子进程直接
    ModuleNotFoundError。写 "python" 的人本意一定是"当前这个 python"。
    """
    write_conf(_demo())
    conf = external.load_conf()["demo"]
    assert conf.command == sys.executable


def test_whitelist_and_empty_allow(write_conf):
    """白名单过滤；没写 allow 时一个都不暴露（而不是全暴露）。"""
    write_conf(_demo(allow=["echo"]))
    assert sorted(external.schema_map()) == ["demo__echo"]

    write_conf(_demo(allow=[]))
    assert external.schema_map() == {}, "allow 为空必须是零暴露，不能退化成全开"


def test_real_call_roundtrip(write_conf, fresh_db):
    """真协议往返：参数原样、零参可用、异常变可读文本、未知工具给明确错误。"""
    write_conf(_demo())
    tools = external.schema_map()
    assert sorted(tools) == ["demo__echo", "demo__explode", "demo__now"]
    # schema 必须被完整搬运，否则模型看到的是残缺参数表
    assert "text" in tools["demo__echo"]["function"]["parameters"]["properties"]

    assert external.invoke("demo__echo", {"text": "原样 返回"}) == "原样 返回"
    assert len(external.invoke("demo__now", {})) >= 16
    boom = external.invoke("demo__explode", {"reason": "故意失败"})
    assert "explode" in boom and "error" in boom.lower(), boom
    assert "未配置" in external.invoke("nope__x", {})


def test_calls_are_logged_with_readonly_flag(write_conf, fresh_db):
    """外部调用落同一张 mcp_calls，且读写标记来自配置。"""
    write_conf(_demo())
    external.invoke("demo__echo", {"text": "记账测试"})
    external.invoke("demo__explode", {"reason": "x"})

    conn = db.get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT tool, client, status, read_only FROM mcp_calls WHERE client='ext:demo' "
            "ORDER BY id")]
    finally:
        conn.close()
    assert [r["tool"] for r in rows] == ["demo__echo", "demo__explode"]
    assert rows[0]["read_only"] == 1 and rows[1]["read_only"] == 0
    assert rows[0]["status"] == "ok" and rows[1]["status"] == "error"


def test_broken_server_does_not_take_down_others(write_conf):
    """一个起不来的第三方 server 不能影响别的 server，更不能抛到主流程。"""
    servers = _demo()
    servers["ghost"] = {"command": "definitely-not-a-real-binary-xyz", "args": [],
                        "allow": ["whatever"]}
    write_conf(servers)

    names = external.schema_map()
    assert sorted(names) == ["demo__echo", "demo__explode", "demo__now"]
    err = external.invoke("ghost__whatever", {})
    assert "不可用" in err or "error" in err.lower()
