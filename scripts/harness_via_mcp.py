"""用本地 DeepSeek harness 的循环，通过 MCP 协议调工具。

和 `app/services/harness.py` 的区别只有一处：工具来源。那边是进程内直连 `_IMPL`，
这边是 `tools/list` 取清单、`tools/call` 执行——模型、循环、消息格式完全一样。

用法：
    python scripts/harness_via_mcp.py "我今天该复习什么？顺便看看 Flink 反压我记过什么"
    python scripts/harness_via_mcp.py --bench     # 只测协议开销，不调模型
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import llm  # noqa: E402
from app.mcp.client import MCPToolbox  # noqa: E402
from app.services import harness  # noqa: E402

SYSTEM = ("你是候选人的求职助手。先调工具查真实数据，再回答；查不到就直说知识缺口，不要编。"
          "回答控制在 200 字以内。")
MAX_ITERS = 4


async def run_agent(question: str) -> dict:
    steps = []
    async with MCPToolbox() as box:
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
        answer = ""
        for i in range(MAX_ITERS):
            # llm 是同步 httpx，用线程包一层，别把事件循环堵住
            msg = await asyncio.to_thread(
                llm._chat_json, None, None, box.openai_tools(), msgs, 0.2)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = msg.get("content") or ""
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
            for tc in calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                ok, text = await box.call(name, args)
                steps.append({"iter": i, "tool": name, "args": args, "ok": ok,
                              "chars": len(text), "preview": text[:120]})
                msgs.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": text[:3000]})
        return {"question": question, "answer": answer, "steps": steps,
                "tools_available": box.names}


async def bench(rounds: int = 5) -> None:
    """协议开销：同一个工具，进程内直调 vs 走 MCP stdio。"""
    name, args = "get_daily_brief", {}
    t0 = time.perf_counter()
    for _ in range(rounds):
        harness._IMPL[name](args)
    direct = (time.perf_counter() - t0) / rounds * 1000

    async with MCPToolbox() as box:
        t0 = time.perf_counter()
        for _ in range(rounds):
            await box.call(name, args)
        viamcp = (time.perf_counter() - t0) / rounds * 1000

    print(f"进程内直调  {direct:7.2f} ms/次")
    print(f"走 MCP      {viamcp:7.2f} ms/次")
    print(f"协议开销    {viamcp - direct:7.2f} ms/次（+{viamcp / direct - 1:.0%}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*", default=[])
    ap.add_argument("--bench", action="store_true")
    a = ap.parse_args()
    if a.bench:
        asyncio.run(bench())
        return
    q = " ".join(a.question) or "我今天该复习什么？顺便看看 Flink 反压我记过什么"
    out = asyncio.run(run_agent(q))
    print(f"\nQ: {out['question']}")
    for s in out["steps"]:
        print(f"  [{s['iter']}] {s['tool']}({json.dumps(s['args'], ensure_ascii=False)[:60]}) "
              f"ok={s['ok']} {s['chars']}字")
    print(f"\nA: {out['answer'] or '（循环内没有给出最终回答）'}")
    print(f"\n工具来自 MCP tools/list：{len(out['tools_available'])} 个")


if __name__ == "__main__":
    main()
