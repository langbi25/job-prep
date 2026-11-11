"""优化执行器 CLI（P4）：归因根因 → prompt 候选改写 → scoped eval 门禁 → 自动晋级/回滚。

用法：
  python scripts/run_optimizer.py --plan                     # 从最新归因报告出优化计划（零 LLM）
  python scripts/run_optimizer.py --evaluate --yes           # 起草 → 上机评测 → 自动晋级/回滚（有 LLM 成本）
  python scripts/run_optimizer.py --status                   # prompt 版本表
  python scripts/run_optimizer.py --rollback 12              # 手动回滚某个 active 候选
可选：--report PATH（指定归因报告）、--baseline PATH（指定基线评测报告，缺省最新 results）
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import ensure_dirs, init_db  # noqa: E402
from app.services import optimizer  # noqa: E402


def _load_attribution(path: str | None) -> dict:
    from app.services import attribution
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    files = sorted((attribution.EVALS_DIR).glob("attribution-*.json"))
    if not files:
        sys.exit("没有归因报告（evals/attribution-*.json），先跑 "
                 "run_evals.py --attribute 或 run_attribution.py")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="优化执行器 v1")
    parser.add_argument("--plan", action="store_true", help="从归因报告生成优化计划（零 LLM）")
    parser.add_argument("--evaluate", action="store_true",
                        help="起草候选 → scoped eval → 自动晋级/回滚（有 LLM 成本）")
    parser.add_argument("--status", action="store_true", help="查看 prompt 版本表")
    parser.add_argument("--rollback", type=int, help="手动回滚指定候选 id（须为 active）")
    parser.add_argument("--report", help="归因报告路径（缺省最新 attribution-*.json）")
    parser.add_argument("--baseline", help="基线评测报告路径（缺省最新 results-*.json）")
    parser.add_argument("--yes", action="store_true", help="--evaluate 的确认开关（会有 LLM 调用成本）")
    args = parser.parse_args()

    ensure_dirs()
    init_db()

    if args.rollback:
        print(json.dumps(optimizer.manual_rollback(args.rollback), ensure_ascii=False, indent=2))
        return

    if args.status:
        rows = optimizer.list_versions()
        if not rows:
            print("版本表为空")
            return
        print(f"{'id':>4}  {'prompt':<20} {'ver':>4}  {'status':<12} {'gate':<5} 决策依据")
        for r in rows:
            basis = ""
            if r["regressions"]:
                basis = "回归: " + r["regressions"][0]
            elif r["gate_passed"] is not None:
                basis = "gate 通过" if r["gate_passed"] else "gate 未过"
            elif r["changes"].get("validation_problems"):
                basis = "校验拒绝: " + r["changes"]["validation_problems"][0]
            print(f"{r['id']:>4}  {r['prompt_name']:<20} v{r['version']:<3} {r['status']:<12} "
                  f"{'' if r['gate_passed'] is None else ('PASS' if r['gate_passed'] else 'FAIL'):<5} {basis}")
            if r["reason"]:
                print(f"     └ {r['reason'][:90]}")
        return

    ar = _load_attribution(args.report)
    p = optimizer.plan(ar)
    print(f"# 优化计划 · {p['generated_at']}")
    if not p["candidates"]:
        print("（无可自动执行的候选——归因失败都不落在可优化 prompt 的范围内）")
    for c in p["candidates"]:
        print(f"\n[候选] {c['prompt']}（{c['describe']}）  {c['failures']} 条失败  scope={c['scope']}")
        print(f"   根因: {', '.join(c['rules'])}")
        print(f"   案例: {'；'.join(c['cases'][:4])}")
    for n in p["manual_notes"]:
        print(f"\n[人工] {n}")

    if not args.evaluate:
        print("\n（预览模式：加 --evaluate --yes 才会真正起草并上机评测）")
        return
    if not args.yes:
        sys.exit("--evaluate 需要 --yes 确认（会产生 LLM 调用成本）")

    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    for c in p["candidates"]:
        print(f"\n=== 起草候选：{c['prompt']} ===")
        d = optimizer.draft_candidate(c["prompt"], c)
        if not d.get("ok"):
            print(f"   起草失败: {d.get('error') or d.get('problems')}")
            if d.get("candidate_id"):
                print(f"   已记为 rejected（id={d['candidate_id']}），失败原因进了版本表")
            continue
        print(f"   候选 id={d['candidate_id']} v{d['version']}，修改点 {len(d['changes'])} 处")
        for ch in d["changes"][:5]:
            print(f"     - {ch}")
        print("   上机评测中（scoped eval）…")
        out = optimizer.evaluate_candidate(d["candidate_id"], baseline_report=baseline)
        if not out.get("ok"):
            print(f"   评测异常: {out.get('error')}")
            continue
        print(f"   决策: {out['decision'].upper()}  gate={'PASS' if out['gate_passed'] else 'FAIL'}")
        for f in out.get("gate_fails", []):
            print(f"     - gate: {f}")
        for r in out.get("regressions", []):
            print(f"     - 回归: {r}")
        print(f"   指标: {json.dumps(out['metrics'], ensure_ascii=False)}")
        print(f"   评测报告: {out['eval_report']}")


if __name__ == "__main__":
    main()
