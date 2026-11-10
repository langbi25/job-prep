"""OTLP 导出链路冒烟：本地起一个最小 HTTP 接收器，验证 span 真能发出去。

不依赖 Phoenix/Langfuse——验证的是「配置 → TracerProvider → OTLP POST」这段管道。
后端装好后，同一份埋点把 endpoint 换掉即可（见 docs/Trace采集.md）。
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RECEIVED: list[tuple[int, str]] = []


class Sink(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append((len(body), self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):  # 静默
        pass


def main() -> None:
    srv = HTTPServer(("127.0.0.1", 7999), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    os_environ_patch = {"TRACING_ENDPOINT": "http://127.0.0.1:7999"}
    import os
    os.environ.update(os_environ_patch)
    from app.observability import tracing as tr
    assert tr.configure() is True, "configure 应读取 TRACING_ENDPOINT 启用"

    tr.set_scene("smoke")
    with tr.agent_span("agent.smoke", {"agent.mode": "smoke"}) as tid:
        with tr.span("llm.chat", tr.KIND_LLM, {tr.ATTR_MODEL: "fake-model"}) as s:
            s.set_attribute(tr.ATTR_PROMPT_TOKENS, 10)
            s.set_attribute(tr.ATTR_COMPLETION_TOKENS, 5)
        with tr.span("tool.search_kb", tr.KIND_TOOL, {"tool.name": "search_kb"}):
            pass

    tr._provider.force_flush()
    srv.shutdown()
    assert len(RECEIVED) >= 1, "OTLP 导出器应至少 POST 一次"
    print(f"收到 {len(RECEIVED)} 次 OTLP POST：")
    for size, path in RECEIVED:
        print(f"  POST {path}  {size} bytes")
    print(f"trace_id={tid}（AGENT 根 span 的 32 hex，落库关联用这个）")
    print("OTLP 管道 OK")


if __name__ == "__main__":
    main()
