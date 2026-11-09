"""MCP 接入层测试：注册表一致性 / 读写边界 / 调用日志 / 评测打分。

不依赖 pytest（项目里没装），pytest 与 `python tests/test_mcp.py` 双模式可跑。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app.api import mcp_board  # noqa: E402
from app.core.db import get_conn, init_db  # noqa: E402
from app.mcp import gateway, tools  # noqa: E402
from app.mcp.server import mcp  # noqa: E402


def test_registry_consistency():
    """注册表、暴露签名、spec 三者必须一一对应——不然会出现「列了工具但没有实现」。"""
    names = [fn.__name__ for fn in tools.REGISTERED]
    assert len(names) == 8, names
    assert set(names) == set(gateway.SPECS), set(names) ^ set(gateway.SPECS)
    listed = [t.name for t in asyncio.run(mcp.list_tools())]
    assert listed == names, (listed, names)
    # 每个 schema 必须有 properties 与 required（缺了模型不知道传什么）
    for t in asyncio.run(mcp.list_tools()):
        assert "properties" in t.input_schema
        assert t.description


def test_read_write_boundary():
    read = {n for n, s in gateway.SPECS.items() if s.read_only}
    write = set(gateway.SPECS) - read
    assert read == {"search_kb", "get_card", "get_daily_brief", "list_due_cards",
                    "get_weak_topics", "get_top_questions"}, read
    assert write == {"draft_cards", "ingest_text"}, write


def test_invoke_logs_and_unknown_tool():
    init_db()
    conn = get_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0]
    finally:
        conn.close()
    out = gateway.invoke("get_daily_brief", {}, client="test")
    assert out.ok and out.payload and out.chars > 2
    bad = gateway.invoke("no_such_tool", {}, client="test")
    assert not bad.ok and "未知工具" in bad.payload
    conn = get_conn()
    try:
        rows = conn.execute("SELECT tool, status FROM mcp_calls ORDER BY id DESC LIMIT 2").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0]["tool"] == "no_such_tool" and rows[0]["status"] == "error"
    assert rows[1]["tool"] == "get_daily_brief" and rows[1]["status"] == "ok"


def test_write_tool_blocked_from_ui():
    """页面试跑不能成为第二条写入口：写工具必须被挡在审批流程之外。"""
    for name in ("draft_cards", "ingest_text"):
        try:
            mcp_board.mcp_try(mcp_board.TryReq(tool=name, args={}))
            raise AssertionError(f"{name} 不该允许页面试跑")
        except HTTPException as e:
            assert e.status_code == 400


def test_eval_scorer():
    """参数打分只看关键值，不做全等比较——否则「多填一个 top_k」会被误判成错。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts import mcp_eval  # noqa: E402  # 走包路径，避免和 CLI 的 sys.path 技巧打架

    ok, bad = mcp_eval._args_ok({"query": ["反压"]}, {"query": "Flink 反压排查", "top_k": 5})
    assert ok and not bad
    ok, bad = mcp_eval._args_ok({"direction": ["backend"]}, {"direction": "bigdata"})
    assert not ok and bad == ["direction"]
    assert mcp_eval._args_ok({}, {"anything": 1}) == (True, [])


def test_board_payload_shape():
    b = mcp_board.mcp_board()
    assert b["status"]["tools"] == 8
    assert b["status"]["read_only"] == 6 and b["status"]["write"] == 2
    assert "kb://card/{card_id}" in b["status"]["resources"]
    assert {"name", "summary", "read_only"} <= set(b["tools"][0])
    stats = b["stats"]
    assert {"total", "ok_rate", "p50_ms", "p95_ms", "calls", "by_tool"} <= set(stats)
    json.dumps(b, ensure_ascii=False)  # 必须是可序列化的纯数据，前端才能直接吃


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} 通过")
    return passed == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
