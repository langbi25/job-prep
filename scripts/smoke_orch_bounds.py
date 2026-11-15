"""编排两道闸门的真实链路验证（成本 / 截止时间）。

用真实 LLM 跑，但把阈值调到"必然触发"，看的是行为而不是省钱：
1. 成本闸门：超限后是否**停止派发**新步骤、已完成的是否保留、终态是否 capped；
2. 截止时间：进函数即过期时是否正确整条跳过并标 timeout。

两次都只花规划那一次的钱（约 ¥0.01），因为闸门在第一批派发前后就会触发。
"""
import json
import os

from app.agents import orchestrator as orch

GOAL = ("按我的薄弱点，给我一个三天的补强安排：每天补什么、为什么先补它，"
        "并指出依据来自哪些数据。")


def show(tag: str, res: dict) -> None:
    steps = res.get("steps") or []
    print(json.dumps({
        "tag": tag,
        "status": res.get("status"),
        "stopped_by": res.get("stopped_by"),
        "cost_cny": res.get("cost_cny"),
        "n_steps": len(steps),
        "step_status": [(s["id"], s["role"], s["status"], s.get("model")) for s in steps],
        "errors": [s.get("error") for s in steps if s.get("error")],
        "answer_chars": len(res.get("answer") or ""),
        "plan_id": res.get("plan_id"),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    print("=== 1. 成本闸门（上限 ¥0.01，一步就会超） ===", flush=True)
    os.environ["ORCH_MAX_COST_CNY"] = "0.01"
    os.environ.pop("ORCH_DEADLINE_S", None)
    show("cap", orch.orchestrate(GOAL, mode="assist", retry=False))

    print("\n=== 2. 截止时间（0 秒，进函数即过期） ===", flush=True)
    os.environ["ORCH_MAX_COST_CNY"] = "0.5"
    os.environ["ORCH_DEADLINE_S"] = "0"
    show("deadline", orch.orchestrate(GOAL, mode="assist", retry=False))

    print("\n=== 3. 正常（对照，确认闸门没误伤） ===", flush=True)
    os.environ.pop("ORCH_DEADLINE_S", None)
    os.environ["ORCH_MAX_COST_CNY"] = "0.5"
    show("normal", orch.orchestrate(GOAL, mode="assist", retry=False))
