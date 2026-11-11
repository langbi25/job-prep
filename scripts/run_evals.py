"""Agent 评测套件 CLI（阶段 6 深化；P4 起评测函数本体在 app/services/eval_suite.py）。

四组评测：
1. 评分校准（50 例 golden set）：band_accuracy / json_validity / 延迟
2. 检索质量：三档对比 fts / hybrid / full(改写+rerank)，hit@5 + MRR
3. Agent 任务级评测：学习教练是否真的完成任务（工具数/数据引用/时间预算）
4. 门禁：结果与 evals/thresholds.json 比对，不达标退出码 1（CI 用）

用法：
  python scripts/run_evals.py                # 全量
  python scripts/run_evals.py --skip-llm     # 只跑检索（fts/hybrid）
  python scripts/run_evals.py --gate         # 全量并做门禁判定
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import llm  # noqa: E402
from app.core.db import ensure_dirs, init_db  # noqa: E402
from app.services import eval_suite  # noqa: E402
from app.services.eval_suite import (  # noqa: E402,F401  re-export：ci_gate.py 依赖
    EVALS_DIR, band_of, eval_agent_tasks, eval_extract_judge, eval_retrieval,
    eval_scoring, eval_search_tool, gate)

from app.observability import phoenix_sync  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--stability", action="store_true",
                        help="边界用例（均分在档位边界 ±0.5 内）重评 2 次取中位数，抑制判分抖动")
    parser.add_argument("--phoenix", action="store_true",
                        help="评测结果双写 Phoenix（数据集+experiment，零额外 LLM 调用）")
    parser.add_argument("--attribute", action="store_true",
                        help="评测后跑归因引擎（P3）：失败 → 根因聚类，落 evals/attribution-*.json")
    args = parser.parse_args()

    ensure_dirs()
    init_db()
    report = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), "llm_calls": 0}
    before = llm.stats()["calls"]

    report["search_tool"] = eval_suite.eval_search_tool()
    if not args.skip_llm:
        report["scoring"] = eval_suite.eval_scoring(stability=args.stability)
        report["agent_tasks"] = eval_suite.eval_agent_tasks()
        report["extract_judge"] = eval_suite.eval_extract_judge()
        report["retrieval"] = eval_suite.eval_retrieval(("fts", "hybrid", "full"))
    else:
        report["retrieval"] = eval_suite.eval_retrieval(("fts", "hybrid"))
    report["llm_calls"] = llm.stats()["calls"] - before

    # 双写 Phoenix：本地报告照旧落盘（门禁与看板的事实源），平台只是分析层。
    # 失败不阻断评测——观测挂了不能拖垮业务，这是可观测层的第一原则。
    if args.phoenix:
        try:
            if phoenix_sync.available():
                info = phoenix_sync.sync_eval_report(report, EVALS_DIR)
                report["phoenix"] = {"datasets": info["datasets"],
                                     "experiments": {k: {"id": v["id"], "runs": v["runs"]}
                                                     for k, v in info["experiments"].items()}}
                print("\n[Phoenix 双写]")
                for name, d in info["datasets"].items():
                    print(f"   dataset {name}: {'复用' if d['skipped'] else '新建'}"
                          f"（{d['cases']} 例）")
                for name, e in info["experiments"].items():
                    print(f"   experiment {name}: {e['runs']} runs -> {phoenix_sync.experiment_url(e['id'])}")
            else:
                print("\n[Phoenix] 不可达，跳过双写（本地报告不受影响）")
        except Exception as e:  # noqa: BLE001
            print(f"\n[Phoenix] 双写失败（不影响本地结果）: {type(e).__name__}: {e}")

    out = EVALS_DIR / f"results-{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 评测结果（LLM 调用 {report['llm_calls']} 次）===")
    if "scoring" in report:
        s = report["scoring"]
        print(f"[评分校准] {s['cases']} 例  band_accuracy={s['band_accuracy']}"
              f"（多数类基线 {s['majority_baseline']}，提升 {s['lift']}）"
              f"  json={s['json_validity']}  latency={s['avg_latency_s']}s")
        print(f"   档位分布: {s['band_distribution']}")
        for d in s["details"]:
            if not d.get("ok"):
                print(f"   MISS {d.get('q')}… exp={d.get('exp')} got={d.get('got')} avg={d.get('avg')} {d.get('error','')}")
    print("\n[检索三档对比]")
    for mode, r in report["retrieval"].items():
        if r.get("skipped"):
            print(f"   {mode}: skipped")
        else:
            print(f"   {mode:8s} hit@5={r['hit5']}  MRR={r['mrr']}  ({r['cases']} 例)")
            for kind, v in r.get("by_kind", {}).items():
                print(f"            · {kind:11s} hit@5={v['hit5']}  MRR={v['mrr']}  ({v['cases']} 例)")
    if "agent_tasks" in report:
        a = report["agent_tasks"]
        print(f"\n[Agent 任务级] success_rate={a['success_rate']}")
        for d in a["details"]:
            print(f"   {'OK' if d['ok'] else 'FAIL'} {d['task']}  tools={d.get('tools')} items={d.get('items')} min={d.get('minutes')} {d.get('checks', '')}")

    st = report.get("search_tool")
    if st:
        print(f"\n[search_kb 工具] 有卡 {st['cases']} 例  hit@3={st['hit3']}  top1={st['top1']}"
              f"  空要点命中 {st['empty_point_hits']} 次")
        print(f"                 无卡 {st['gap_cases']} 例  误标高置信 {st['gap_false_confident']} 例")
        for d in st["details"]:
            if not d.get("ok"):
                print(f"   MISS {d['q']}… exp={d.get('kind')} got={d.get('top3')} conf={d.get('confidence')}")
        for d in st["gap_details"]:
            if d.get("false_confident"):
                print(f"   FALSE-CONFIDENT {d['q']}… sim={d.get('sim')}")

    ex = report.get("extract_judge")
    if ex:
        print(f"\n[提取判定] {ex['cases']} 例  doc_type={ex['doc_type_accuracy']}"
              f"  完整度={ex['completeness_accuracy']}"
              f"  complete 精确率={ex['complete_precision']}（判成 complete 的 {ex['n_complete_pred']} 条）"
              f"  仅题目召回={ex['bare_recall']}")
        for d in ex["details"]:
            if d.get("error"):
                print(f"   ERROR {d['id']}: {d['error']}")
            elif not d["doc_ok"] or d["mismatch"]:
                print(f"   MISS {d['id']}  doc={d['doc']} pred={d['pred']} exp={d['exp']}")

    # 归因（P3）：评测回答"挂没挂"，归因回答"为什么挂"。
    # 纯规则、零 LLM 调用，失败不阻断评测主流程——闭环里每一环都不能拖垮上一环。
    if args.attribute:
        try:
            from app.services import attribution
            ar = attribution.build_failure_report(eval_report=report)
            ar_path = attribution.save_failure_report(ar)
            report["attribution"] = {"failures": ar["summary"]["failures"],
                                     "top_issues": ar["top_issues"],
                                     "report": ar_path.name}
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[归因] 失败 {ar['summary']['failures']} 条 / 扫描 {ar['summary']['cases_scanned']}"
                  f" → {ar_path.name}")
            for i, t in enumerate(ar["top_issues"], 1):
                print(f"   {i}. [{t['severity']}] {t['name']} × {t['count']}（{t['sources']}）")
        except Exception as e:  # noqa: BLE001
            print(f"\n[归因] 失败（不影响评测结果）: {type(e).__name__}: {e}")

    if args.gate:
        passed, fails = gate(report)
        print(f"\n[门禁] {'PASS' if passed else 'FAIL'}")
        for f in fails:
            print("   -", f)
        print(f"\n完整报告: {out}")
        sys.exit(0 if passed else 1)
    print(f"\n完整报告: {out}")


if __name__ == "__main__":
    main()
