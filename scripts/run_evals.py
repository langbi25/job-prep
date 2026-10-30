"""Agent 评测套件（阶段 6 深化）。

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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import config, llm  # noqa: E402
from app.core.db import init_db, ensure_dirs  # noqa: E402
from app.services import context_builder, harness, rag_service  # noqa: E402

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"


def band_of(avg: float) -> str:
    """四维均分 → 档位。阈值来自实测分布而非拍脑袋：

    62 例标注集上，low 档实际均分封顶 4.8，mid 档最低 4.8，high 最低 7.0。
    所以 low/mid 切在 5.0（不是直觉的 4.0），high 切在 7.0。
    注意：阈值是在这批标注上标定的，扩样本后应重新确认，避免过拟合。
    """
    return "high" if avg >= 7 else ("mid" if avg >= 5 else "low")


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# 档位边界（与 band_of 保持一致），用于判断哪些用例需要抗抖动重评
BAND_EDGES = (5.0, 7.0)
EDGE_TOLERANCE = 0.5


def near_edge(avg: float) -> bool:
    return any(abs(avg - e) <= EDGE_TOLERANCE for e in BAND_EDGES)


def _score_once(c: dict) -> tuple[float, bool]:
    """评一次，返回 (四维均分, 是否解析成功)。"""
    ctx = context_builder.build_eval_context(c["topic"], c["question"])
    ev = llm.evaluate_answer({"text": c["question"], "qtype": c["qtype"]}, c["answer"], "", ctx)
    s = ev["scores"]
    avg = sum(s[d] for d in ("correctness", "depth", "structure", "highlight")) / 4
    return avg, True


# ---------- 1. 评分校准 ----------
def eval_scoring(stability: bool = False) -> dict:
    cases = _load_jsonl(EVALS_DIR / "golden_answers.jsonl")
    correct, valid, latencies, details = 0, 0, [], []
    rescored = 0
    for c in cases:
        t0 = time.time()
        try:
            avg, _ = _score_once(c)
            latencies.append(time.time() - t0)
            valid += 1
            # 边界用例重评两次取中位数：LLM judge 在档位边界附近的抖动是固有噪声
            # （同一回答跨轮 6.8 → 3.2 的极端情况另有上下文污染原因，已修），
            # 这里只处理 ±0.5 容差内的温和抖动，控制成本（最多 ×3 调用）。
            if stability and near_edge(avg):
                others = []
                for _ in range(2):
                    a2, _ = _score_once(c)
                    others.append(a2)
                avg = sorted([avg] + others)[1]
                rescored += 1
            got = band_of(avg)
            ok = got == c["expected_band"]
            correct += ok
            details.append({"q": c["question"][:26], "exp": c["expected_band"], "got": got,
                            "avg": round(avg, 1), "ok": ok})
        except Exception as e:  # noqa: BLE001
            details.append({"q": c["question"][:26], "error": str(e)[:100],
                            "exp": c["expected_band"], "ok": False})
    n = len(cases)
    # 多数类基线：如果标注全是 high，一个"永远猜 high"的判定器也能拿到高频占比。
    # 不报这个数，band_accuracy 看起来很高其实可能毫无判别力。
    from collections import Counter
    counts = Counter(c["expected_band"] for c in cases)
    baseline = max(counts.values()) / n if counts else 0.0
    acc = correct / n
    return {"cases": n, "band_accuracy": round(acc, 3),
            "majority_baseline": round(baseline, 3), "lift": round(acc - baseline, 3),
            "band_distribution": dict(counts),
            "stability_rescored": rescored if stability else None,
            "json_validity": round(valid / n, 3),
            "avg_latency_s": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "details": details}


# ---------- 2. 检索三档对比 ----------
def eval_retrieval(modes=("fts", "hybrid", "full")) -> dict:
    """按查询类型分桶统计：只有分桶后三档对比才有诊断价值。

    整套用例全命中时 hit@5 恒为 1.0，看不出 FTS/向量/rerank 各自的贡献；
    分桶后才能说明"FTS 在口语改写上塌了，向量补上，rerank 把 top1 掰正"。
    """
    cases = _load_jsonl(EVALS_DIR / "retrieval_cases.jsonl")
    kinds = sorted({c.get("kind", "unknown") for c in cases})
    out = {}
    for mode in modes:
        if mode == "full" and not config.LLM_API_KEY:
            out[mode] = {"skipped": "no api key"}
            continue
        hits, rr, details = 0, 0.0, []
        per_kind = {k: {"n": 0, "hit": 0, "rr": 0.0} for k in kinds}
        for c in cases:
            kind = c.get("kind", "unknown")
            per_kind[kind]["n"] += 1
            try:
                res = rag_service.search(c["query"], scope="cards", top_k=5, mode=mode)
                ids = [r["id"] for r in res]
                ok = c["expected_card_id"] in ids
                if ok:
                    hits += 1
                    gain = 1.0 / (ids.index(c["expected_card_id"]) + 1)
                    rr += gain
                    per_kind[kind]["hit"] += 1
                    per_kind[kind]["rr"] += gain
                details.append({"q": c["query"][:20], "kind": kind, "top3": ids[:3], "ok": ok})
            except Exception as e:  # noqa: BLE001
                details.append({"q": c["query"][:20], "kind": kind,
                                "error": str(e)[:80], "ok": False})
        n = len(cases)
        out[mode] = {
            "cases": n,
            "hit5": round(hits / n, 3),
            "mrr": round(rr / n, 3),
            "by_kind": {k: {"cases": v["n"],
                            "hit5": round(v["hit"] / v["n"], 3) if v["n"] else None,
                            "mrr": round(v["rr"] / v["n"], 3) if v["n"] else None}
                        for k, v in per_kind.items()},
            "details": details,
        }
    return out


# ---------- 2.5 检索工具（search_kb） ----------
def eval_search_tool() -> dict:
    """评测 Agent 手里的 search_kb 工具，而不是底层检索。

    为什么要单测这一层：工具是"检索 + 过滤 + 截断 + 置信度标注"的组合，
    底层 hit@5 好不代表工具可用——要点为空、卡被归档过滤、置信度误标，都只在工具层暴露。
    两组用例：
    - retrieval_cases.jsonl：有标准卡，量 hit@3 / top1
    - tool_gap_cases.jsonl：无卡的高频题，量"是否被误标成高置信度"（误标 = Agent 以为有卡）
    """
    covered = _load_jsonl(EVALS_DIR / "retrieval_cases.jsonl")
    gaps = _load_jsonl(EVALS_DIR / "tool_gap_cases.jsonl")
    tool = harness._IMPL["search_kb"]

    hits3, top1, empty_points, details = 0, 0, 0, []
    for c in covered:
        try:
            r = tool({"query": c["query"], "top_k": 3})
        except Exception as e:  # noqa: BLE001
            details.append({"q": c["query"][:20], "ok": False, "error": str(e)[:80]})
            continue
        ids = [h["card_id"] for h in r.get("hits", [])]
        ok3 = c["expected_card_id"] in ids
        ok1 = bool(ids) and ids[0] == c["expected_card_id"]
        hits3 += ok3
        top1 += ok1
        for h in r.get("hits", []):
            if not h.get("points"):
                empty_points += 1
        details.append({"q": c["query"][:20], "kind": c.get("kind"), "ok": ok3, "top1": ok1,
                        "confidence": r.get("confidence"), "top3": ids[:3],
                        "sim": r.get("max_similarity")})

    false_confident, gap_details = 0, []
    for c in gaps:
        try:
            r = tool({"query": c["query"], "top_k": 3})
        except Exception as e:  # noqa: BLE001
            gap_details.append({"q": c["query"][:20], "error": str(e)[:80]})
            continue
        bad = r.get("confidence") == "high"
        false_confident += bad
        gap_details.append({"q": c["query"][:20], "confidence": r.get("confidence"),
                            "sim": r.get("max_similarity"), "false_confident": bad})

    n = len(covered) or 1
    return {
        "cases": len(covered),
        "hit3": round(hits3 / n, 3),
        "top1": round(top1 / n, 3),
        "empty_point_hits": empty_points,
        "gap_cases": len(gaps),
        "gap_false_confident": false_confident,
        "gap_rate": round(false_confident / (len(gaps) or 1), 3),
        "details": details,
        "gap_details": gap_details,
    }



def _accept(v) -> list:
    return v if isinstance(v, list) else [v]


def eval_extract_judge() -> dict:
    """评测"提取阶段的判定"，而不是抽取出的题面。

    为什么这一层值得单独评：判定结果直接决定数据流向——
    判成 complete 的题会用原文答案生成卡片（低风险分支），判成 none/partial 的题走
    LLM 从零起草（高风险分支）。所以两列最关键：
    - complete_precision：判成 complete 的里面有多少真是 complete。
      误判 = 一段残缺甚至错误的回答被当成参考答案写进 question_pool，
      而 context_builder 会把它塞进模拟面试的评分上下文，静默污染整轮打分。
    - bare_recall：真实"光有题没答案"的题有没有被认出来。
      漏判 = 该建卡的题没建，用户还是得手动去题目池补。
    doc_type 允许一个用例给多个可接受答案：单题带"一面"语境本身就有歧义，
    强行只认一个值会让评测变成在考标注员的手感。
    """
    cases = _load_jsonl(EVALS_DIR / "extract_judge_cases.jsonl")
    doc_ok, pairs, details = 0, [], []
    for c in cases:
        try:
            d = llm.extract_interview(c["raw"], "")
        except Exception as e:  # noqa: BLE001
            details.append({"id": c["id"], "error": f"{type(e).__name__}: {e}"[:120]})
            continue
        ok_doc = d["doc_type"] in _accept(c["expect_doc_type"])
        doc_ok += ok_doc
        preds = [q["answer_completeness"] for q in d["questions"]]
        row = {"id": c["id"], "doc_ok": ok_doc, "doc": d["doc_type"],
               "pred": preds, "exp": c["expect_completeness"], "mismatch": []}
        for i, exp in enumerate(c["expect_completeness"]):
            acc = _accept(exp)
            pred = preds[i] if i < len(preds) else None
            pairs.append((acc, pred))
            if pred not in acc:
                row["mismatch"].append({"i": i, "exp": acc, "got": pred})
        details.append(row)

    n_pairs = len(pairs) or 1
    comp_ok = sum(1 for acc, pred in pairs if pred in acc)
    pred_complete = [(acc, p) for acc, p in pairs if p == "complete"]
    complete_precision = (sum(1 for acc, _ in pred_complete if "complete" in acc) / len(pred_complete)
                          if pred_complete else 1.0)
    bare = [(acc, p) for acc, p in pairs if acc == ["none"]]
    bare_recall = sum(1 for _, p in bare if p == "none") / (len(bare) or 1)
    return {
        "cases": len(cases),
        "doc_type_accuracy": round(doc_ok / (len(cases) or 1), 3),
        "completeness_accuracy": round(comp_ok / n_pairs, 3),
        "complete_precision": round(complete_precision, 3),
        "bare_recall": round(bare_recall, 3),
        "n_complete_pred": len(pred_complete),
        "details": details,
    }


# ---------- 3. Agent 任务级 ----------
def eval_agent_tasks() -> dict:
    """学习教练任务级评测：工具数、数据引用、时间预算三项是否都成立。"""
    tasks = _load_jsonl(EVALS_DIR / "agent_tasks.jsonl")
    details, ok_count = [], 0
    for t in tasks:
        try:
            res = harness.run_study_plan_agent(t["direction"])
        except Exception as e:  # noqa: BLE001  单条任务异常不能带崩整个套件
            details.append({"task": t["task"], "ok": False, "reason": f"{type(e).__name__}: {e}"})
            continue
        if res.get("status") != "done":
            details.append({"task": t["task"], "ok": False, "reason": res.get("status"),
                            "error": res.get("error")})
            continue
        trace = res["trace"]
        tools = {s["tool"] for s in trace if s.get("tool") and s["tool"] != "final_answer"}
        # plan 已在 harness 边界归一为 list[dict]，评测只认这一种结构
        plan = res["plan"]
        items = plan["plan"]
        plan_text = json.dumps(items, ensure_ascii=False)
        total_min = plan["total_minutes"]
        refs_ok = all(m in plan_text for m in t.get("must_mention", []))
        checks = {
            "tools": len(tools) >= t.get("min_tools", 2),
            "data_referenced": refs_ok and len(items) >= 2,
            "time_budget": total_min <= t.get("max_minutes", 60),
        }
        ok = all(checks.values())
        ok_count += ok
        details.append({"task": t["task"], "ok": ok, "checks": checks,
                        "tools": sorted(tools), "items": len(items), "minutes": total_min})
    return {"cases": len(tasks), "success_rate": round(ok_count / len(tasks), 3), "details": details}


# ---------- 4. 门禁 ----------
def gate(report: dict) -> tuple[bool, list[str]]:
    th = json.loads((EVALS_DIR / "thresholds.json").read_text(encoding="utf-8"))
    fails = []
    sc = report.get("scoring")
    if sc:
        if sc["band_accuracy"] < th["band_accuracy"]:
            fails.append(f"band_accuracy {sc['band_accuracy']} < {th['band_accuracy']}")
        # 只有准确率、不看基线时，标注一旦偏斜（比如 60% 都是 high），
        # 门禁就形同虚设——"永远猜 high"也能过。这里强制要求相对基线的提升。
        if th.get("band_lift_min") and sc.get("lift", 0) < th["band_lift_min"]:
            fails.append(f"band_lift {sc.get('lift')} < {th['band_lift_min']}（相对多数类基线无判别力）")
        if sc["json_validity"] < th["json_validity"]:
            fails.append(f"json_validity {sc['json_validity']} < {th['json_validity']}")
        if sc["avg_latency_s"] and sc["avg_latency_s"] > th["avg_latency_s_max"]:
            fails.append(f"latency {sc['avg_latency_s']}s > {th['avg_latency_s_max']}s")
    ret_all = report.get("retrieval") or {}
    ret = ret_all.get("hybrid", {})
    if ret and ret.get("hit5", 0) < th["retrieval_hit5"]:
        fails.append(f"retrieval hit@5 {ret.get('hit5')} < {th['retrieval_hit5']}")
    para = (ret.get("by_kind") or {}).get("paraphrase", {}).get("hit5")
    if th.get("retrieval_paraphrase_hit5") and para is not None and para < th["retrieval_paraphrase_hit5"]:
        fails.append(f"paraphrase hit@5 {para} < {th['retrieval_paraphrase_hit5']}（语义召回不足）")
    at = report.get("agent_tasks")
    if at and at["success_rate"] < th["agent_task_success"]:
        fails.append(f"agent_task_success {at['success_rate']} < {th['agent_task_success']}")
    st = report.get("search_tool")
    if st:
        if st["hit3"] < th["search_tool_hit3"]:
            fails.append(f"search_kb hit@3 {st['hit3']} < {th['search_tool_hit3']}")
        # 无卡题被标成 high = Agent 会以为"我有卡"，比检索不到更危险
        if st["gap_false_confident"] > th["search_tool_gap_false_confident"]:
            fails.append(f"search_kb 无卡题误标高置信 {st['gap_false_confident']} 例"
                         f" > {th['search_tool_gap_false_confident']}")
    ex = report.get("extract_judge")
    if ex:
        if ex["doc_type_accuracy"] < th["extract_doc_type_accuracy"]:
            fails.append(f"extract doc_type {ex['doc_type_accuracy']} < {th['extract_doc_type_accuracy']}")
        if ex["completeness_accuracy"] < th["extract_completeness_accuracy"]:
            fails.append(f"extract 完整度 {ex['completeness_accuracy']} < {th['extract_completeness_accuracy']}")
        # 这两条是"数据流向正确性"而不是"抽取质量"：误判 complete = 污染评分基准
        if ex["complete_precision"] < th["extract_complete_precision"]:
            fails.append(f"complete 精确率 {ex['complete_precision']} < {th['extract_complete_precision']}"
                         f"（错判会把残缺答案写成参考答案）")
        if ex["bare_recall"] < th["extract_bare_recall"]:
            fails.append(f"仅题目召回 {ex['bare_recall']} < {th['extract_bare_recall']}")
    return (not fails), fails


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--stability", action="store_true",
                        help="边界用例（均分在档位边界 ±0.5 内）重评 2 次取中位数，抑制判分抖动")
    args = parser.parse_args()

    ensure_dirs()
    init_db()
    report = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), "llm_calls": 0}
    before = llm.stats()["calls"]

    report["search_tool"] = eval_search_tool()
    if not args.skip_llm:
        report["scoring"] = eval_scoring(stability=args.stability)
        report["agent_tasks"] = eval_agent_tasks()
        report["extract_judge"] = eval_extract_judge()
        report["retrieval"] = eval_retrieval(("fts", "hybrid", "full"))
    else:
        report["retrieval"] = eval_retrieval(("fts", "hybrid"))
    report["llm_calls"] = llm.stats()["calls"] - before

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
