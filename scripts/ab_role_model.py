"""编排的角色模型路由 A/B（真实 LLM，两次调用量）。

同一个目标跑两遍：
  A. 全部 deepseek-chat（默认）
  B. analyst/critic 用 deepseek-reasoner
比较：费用、时长、critic 问题数、答案长度与要点覆盖。

为什么值得真跑：路由的价值是"贵的那部分只在需要的地方贵"，
但 reasoner 的输出质量是否真的更好、费用差多少，靠猜没有意义。
"""
import json
import os
import time

from app.agents import orchestrator as orch

GOAL = ("基于我的知识库和最近的面经，判断我当前最薄弱的两个主题，"
        "给出这周该补什么、按什么顺序补，并说明判断依据。不要泛泛而谈。")


def run(tag: str, mapping: dict | None) -> dict:
    if mapping is None:
        os.environ.pop("ORCH_ROLE_MODEL", None)
    else:
        os.environ["ORCH_ROLE_MODEL"] = json.dumps(mapping)
    orch.role_model("-")                     # 让解析缓存刷新到新配置
    t0 = time.perf_counter()
    res = orch.orchestrate(GOAL, mode="assist", retry=True)
    dur = time.perf_counter() - t0
    steps = res.get("steps") or []
    out = {
        "tag": tag,
        "status": res.get("status"),
        "stopped_by": res.get("stopped_by"),
        "cost_cny": res.get("cost_cny"),
        "tokens": res.get("tokens"),
        "duration_s": round(dur, 1),
        "n_steps": len(steps),
        "models": [s.get("model") for s in steps],
        "step_cost": [round(float(s.get("cost_cny") or 0), 6) for s in steps],
        "critique_ok": (res.get("critique") or {}).get("ok"),
        "critique_issues": (res.get("critique") or {}).get("issues"),
        "answer_chars": len(res.get("answer") or ""),
        "answer": res.get("answer"),
        "plan_id": res.get("plan_id"),
    }
    print(json.dumps({k: v for k, v in out.items() if k != "answer"},
                     ensure_ascii=False), flush=True)
    return out


if __name__ == "__main__":
    a = run("A_chat_only", None)
    b = run("B_reasoner_analyst", {"analyst": "deepseek-reasoner",
                                   "critic": "deepseek-reasoner"})
    print("\n===== 对照 =====", flush=True)
    print(f"费用：A ¥{a['cost_cny']} / B ¥{b['cost_cny']} "
          f"（{b['cost_cny'] / max(a['cost_cny'], 1e-9):.2f}x）", flush=True)
    print(f"时长：A {a['duration_s']}s / B {b['duration_s']}s", flush=True)
    print(f"critic：A ok={a['critique_ok']} issues={len(a['critique_issues'] or [])} / "
          f"B ok={b['critique_ok']} issues={len(b['critique_issues'] or [])}", flush=True)
    print(f"答案字数：A {a['answer_chars']} / B {b['answer_chars']}", flush=True)
    for tag, r in (("A", a), ("B", b)):
        print(f"\n--- {tag} 答案 ---\n{(r['answer'] or '')[:900]}", flush=True)
