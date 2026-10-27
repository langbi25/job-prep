"""CI 门禁：读取最近一次评测结果，与阈值比对（不产生 LLM 调用，可放进 pre-commit / CI）。

用法：python scripts/ci_gate.py [results_file]
退出码 0 = 通过，1 = 不达标（CI 直接红）。

判定逻辑复用 run_evals.gate()，避免两处阈值实现各自演化——
门禁脚本和评测脚本用两套规则时，最容易出现"CI 绿、本地红"的假通过。
"""
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
EVALS_DIR = SCRIPTS_DIR.parent / "evals"
sys.path.insert(0, str(SCRIPTS_DIR))

from run_evals import gate  # noqa: E402


def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        files = sorted(EVALS_DIR.glob("results-*.json"))
        if not files:
            print("没有评测结果，先运行 scripts/run_evals.py")
            sys.exit(1)
        path = files[-1]

    report = json.loads(path.read_text(encoding="utf-8"))
    th = json.loads((EVALS_DIR / "thresholds.json").read_text(encoding="utf-8"))
    passed, fails = gate(report)

    print(f"检查文件: {path.name}")
    print(f"阈值: {th}")
    if not passed:
        print("门禁 FAIL：")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("门禁 PASS")


if __name__ == "__main__":
    main()
