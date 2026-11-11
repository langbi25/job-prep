"""归因引擎 CLI（P3）：对评测报告 / 运行轨迹做根因归因。

评测回答"挂没挂"，本工具回答"为什么挂"，产出：
- evals/attribution-*.json：结构化归因报告（failures + 聚类 + 修复优先级）
- optimizer_input：给 P4 优化执行器的结构化输入（补卡清单 / prompt 修复清单 / 校准观察名单）

用法：
  python scripts/run_attribution.py                    # 归因最新评测报告
  python scripts/run_attribution.py --report evals/results-xxx.json
  python scripts/run_attribution.py --runs             # 附带最近 30 条 Agent 运行轨迹归因
  python scripts/run_attribution.py --ctx              # 附带最近上下文降级记录归因
  python scripts/run_attribution.py --digest out.md    # 把人读摘要写到 md
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import ensure_dirs, init_db  # noqa: E402
from app.services import attribution  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="归因引擎 v1（规则版）")
    parser.add_argument("--report", help="指定评测报告路径，缺省取 evals/ 里最新的 results-*.json")
    parser.add_argument("--runs", action="store_true", help="附带最近 30 条 Agent 运行轨迹归因")
    parser.add_argument("--ctx", action="store_true", help="附带最近上下文降级记录归因")
    parser.add_argument("--digest", help="把人读摘要（digest_md）写到指定 md 文件")
    args = parser.parse_args()

    ensure_dirs()
    init_db()

    report = attribution.build_failure_report(
        eval_report_path=Path(args.report) if args.report else None,
        include_runs=args.runs, include_ctx=args.ctx)
    out = attribution.save_failure_report(report)

    print(report["digest_md"])
    print(f"归因报告: {out}")

    oi = report["optimizer_input"]
    print("\n[P4 优化器输入]")
    if oi["ingest_targets"]:
        print(f"   补卡/补索引: {len(oi['ingest_targets'])} 条")
        for q in oi["ingest_targets"][:5]:
            print(f"     - {q}")
    if oi["prompt_fixes"]:
        print(f"   prompt/工具修复: {len(oi['prompt_fixes'])} 条")
    if oi["calibration_watch"]:
        print(f"   校准观察名单: {len(oi['calibration_watch'])} 条")
    if not any(oi.values()):
        print("   （无失败，无需优化）")

    if args.digest:
        Path(args.digest).write_text(report["digest_md"], encoding="utf-8")
        print(f"\n摘要已写入: {args.digest}")


if __name__ == "__main__":
    main()
