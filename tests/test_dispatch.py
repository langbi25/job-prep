"""工具调度测试（零 LLM 调用）。

测四件事：参数校验能不能在执行前拦下、只读段是否真并发、写操作是否严格串行、
大结果是否真的能翻页回来。
跑法：pytest tests/test_dispatch.py -q
"""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.mcp import dispatch  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="dispatch-test-"))


def _fresh_db() -> None:
    """每个用例一个干净库。Windows 下文件可能被上一个连接占着，先重试删除。"""
    p = _TMP / "kb.sqlite"
    for suffix in ("", "-wal", "-journal"):
        f = Path(str(p) + suffix)
        if f.exists():
            for _ in range(5):
                try:
                    f.unlink()
                    break
                except PermissionError:
                    time.sleep(0.05)
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def _schema(**props):
    return {"type": "object", "properties": props.get("properties", {}),
            "required": props.get("required", [])}


def test_validate_cases():
    params = _schema(properties={
        "query": {"type": "string"},
        "limit": {"type": "integer"},
        "mode": {"type": "string", "enum": ["a", "b"]},
    }, required=["query"])
    assert dispatch.validate(params, {"query": "x"}) == []
    assert "缺少必填参数 query" in dispatch.validate(params, {})[0]
    errs = dispatch.validate(params, {"query": "x", "limit": "3"})
    assert any("limit" in e and "integer" in e for e in errs)
    errs = dispatch.validate(params, {"query": "x", "mode": "c"})
    assert any("enum" in e or "之一" in e for e in errs)
    # 未声明的额外字段不算错（模型多给一个字段不该失败）
    assert dispatch.validate(params, {"query": "x", "extra": 1}) == []


def test_readonly_batch_runs_concurrently():
    _fresh_db()

    def slow(args):
        time.sleep(0.25)
        return {"ok": True, "v": args.get("k")}

    schemas = {"slow": {"type": "object", "properties": {"k": {"type": "string"}}}}
    calls = [{"id": f"c{i}", "function": {"name": "slow", "arguments": json.dumps({"k": str(i)})}}
             for i in range(3)]
    t0 = time.perf_counter()
    outs = dispatch.run_batch(calls, {"slow": slow}, schemas, readonly_fn=lambda n: True)
    elapsed = time.perf_counter() - t0
    assert len(outs) == 3
    assert [o.ok for o in outs] == [True, True, True]
    # 串行需 0.75s，并发应 ≈0.25s。阈值留到 0.55s：线程池首轮建线程在 CI/Windows 上有几十毫秒
    # 抖动，阈值贴着 0.3s 会变成随机失败——随机失败的测试比没有测试更糟（人会开始无视红）。
    assert elapsed < 0.55, f"耗时 {elapsed:.3f}s，疑似没有并发"


def test_write_stays_serial_and_ordered():
    _fresh_db()
    order = []

    def write(args):
        order.append(args["k"])
        time.sleep(0.05)
        return {"ok": True}

    schemas = {"w": {"type": "object", "properties": {"k": {"type": "string"}}}}
    calls = [{"id": f"c{i}", "function": {"name": "w", "arguments": json.dumps({"k": str(i)})}}
             for i in range(3)]
    outs = dispatch.run_batch(calls, {"w": write}, schemas, readonly_fn=lambda n: False)
    assert order == ["0", "1", "2"], order
    assert [o.call_id for o in outs] == ["c0", "c1", "c2"]


def test_batch_preserves_order_across_mixed_calls():
    """读并发 + 写串行 混合时，返回顺序必须与调用顺序一致，否则 tool_call_id 会串位。"""
    _fresh_db()

    def ro(args):
        time.sleep(0.1)
        return {"ok": True, "name": "ro"}

    def rw(args):
        return {"ok": True, "name": "rw"}

    schemas = {"ro": {"type": "object"}, "rw": {"type": "object"}}
    names = ["ro", "ro", "rw", "ro"]
    calls = [{"id": f"c{i}", "function": {"name": n, "arguments": "{}"}}
             for i, n in enumerate(names)]
    outs = dispatch.run_batch(calls, {"ro": ro, "rw": rw}, schemas,
                              readonly_fn=lambda n: n == "ro")
    assert [o.call_id for o in outs] == ["c0", "c1", "c2", "c3"]
    assert [json.loads(o.payload)["name"] for o in outs] == names


def test_dedup_same_args():
    _fresh_db()
    hits = {"n": 0}

    def once(args):
        hits["n"] += 1
        return {"ok": True, "n": hits["n"]}

    schemas = {"once": {"type": "object", "properties": {"q": {"type": "string"}}}}
    calls = [{"id": "c1", "function": {"name": "once", "arguments": '{"q":"same"}'}},
             {"id": "c2", "function": {"name": "once", "arguments": '{"q":"same"}'}}]
    outs = dispatch.run_batch(calls, {"once": once}, schemas, readonly_fn=lambda n: True)
    assert hits["n"] == 1                 # 只真跑了一次
    assert outs[0].cached is False and outs[1].cached is True
    assert outs[0].payload == outs[1].payload


def test_retry_only_readonly():
    _fresh_db()
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("网络抖动")
        return {"ok": True}

    schemas = {"flaky": {"type": "object"}}
    one = [{"id": "c1", "function": {"name": "flaky", "arguments": "{}"}}]
    out = dispatch.run_batch(one, {"flaky": flaky}, schemas, readonly_fn=lambda n: True)[0]
    assert out.ok and out.retried and calls["n"] == 2

    calls["n"] = 0
    out2 = dispatch.run_batch(one, {"flaky": flaky}, schemas, readonly_fn=lambda n: False)[0]
    assert not out2.ok and calls["n"] == 1     # 写工具不重试


def test_invalid_args_skips_execution():
    _fresh_db()
    ran = {"n": 0}

    def need(args):
        ran["n"] += 1
        return {"ok": True}

    schemas = {"need": {"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]}}
    call = {"id": "c1", "function": {"name": "need", "arguments": "{}"}}
    out = dispatch.run_batch([call], {"need": need}, schemas, readonly_fn=lambda n: True)[0]
    assert not out.ok and out.error == "invalid_args" and ran["n"] == 0
    payload = json.loads(out.payload)
    assert payload["details"]


def test_large_result_becomes_ref_and_pages():
    _fresh_db()
    big = {"ok": True, "items": [f"条目{i}" * 20 for i in range(200)]}

    def huge(args):
        return big

    schemas = {"huge": {"type": "object"}}
    out = dispatch.run_batch([{"id": "c1", "function": {"name": "huge", "arguments": "{}"}}],
                             {"huge": huge}, schemas, readonly_fn=lambda n: True)[0]
    assert out.ref, "大结果应落库换 ref"
    head = json.loads(out.payload)
    assert head["_ref"] == out.ref and head["_chars"] > dispatch.TOOL_REF_CHARS
    assert "_preview" in head

    page1 = dispatch.fetch_result(out.ref, 0, 500)
    page2 = dispatch.fetch_result(out.ref, 500, 500)
    assert page1["ok"] and len(page1["chunk"]) == 500
    assert page2["chunk"] != page1["chunk"]
    assert page2["offset"] == 500
    assert dispatch.fetch_result("tr-nope")["ok"] is False


def test_unknown_tool_and_bad_json():
    _fresh_db()
    out = dispatch.run_batch([{"id": "c1", "function": {"name": "nope", "arguments": "{}"}}],
                             {}, {}, readonly_fn=lambda n: True)[0]
    assert not out.ok and out.error == "unknown_tool"

    out2 = dispatch.run_batch([{"id": "c2", "function": {"name": "x", "arguments": "{bad"}}],
                              {}, {}, readonly_fn=lambda n: True)[0]
    assert not out2.ok and out2.error == "bad_json"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
