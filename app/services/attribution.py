"""归因引擎 v1（P3）：从「评测/运行失败」到「根因」的结构化解释。

定位：评测（run_evals）回答"挂没挂"，归因引擎回答"为什么挂"，
优化执行器（P4）拿着根因报告决定改什么、再评测验证是否真的好——
这是自进化闭环（评测 → 归因 → 分析 → 优化 → 再评测）的第二环。

设计原则：
1. 规则先行（v1 不用 LLM）：六类根因对应六条确定性检测规则，可单测、可复现。
   LLM 归因（跨案例模式发现）留给 v2——规则先把"全量日志"压成"结构化嫌疑列表"，
   LLM 的输入成本和幻觉面都小一个量级。
2. 多源接入：评测报告（evals/results-*.json）、Agent 运行轨迹（runs 表）、
   上下文降级记录（ctx_traces 表）都能喂进来；字段缺失时规则静默跳过而不是报错。
3. 失败要聚类：单条失败是噪音，重复模式才是工程信号。按规则 × 维度聚合，
   并给出修复优先级和给 P4 的结构化优化输入（optimizer_input）。
4. 闭合性：所有失败要么归到六类之一，要么进 unattributed——
   "归不进去"本身是信号（判定校准类问题），不能悄悄丢掉。

六类根因（+1 兜底）：
- retrieval_miss     检索未召回：期望卡完全不在返回结果里（索引/分词/卡片缺失）
- low_confidence     召回低置信：召回了但排序靠后 / 置信度误标（标定与排序问题）
- no_data_reference  没引用：产出没有引用要求的数据（编造或没查）
- wrong_tool         工具选错：少调/调错工具（工具描述或选择策略问题）
- context_degraded   上下文降级：评分/回答时上下文空或缺料（预算与组装问题）
- format_violation   格式违规：JSON 解析失败 / 缺字段 / 超预算被裁 / 迭代上限
- unattributed       未归因：以上都不匹配（如 LLM judge 校准类偏移）
"""
import datetime
import json
import re
from pathlib import Path

from ..core import config, spec

EVALS_DIR = config.BASE_DIR / "evals"

# ---------- 规则注册表：severity 决定修复优先级权重，suggestion 是给 P4 的方向 ----------

SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}

RULES = {
    "retrieval_miss": {
        "name": "检索未召回", "severity": "high",
        "suggestion": "补卡片或补索引：跨检索层都未命中 → 知识缺口（ingest）；仅单层未命中 → 分词/向量召回问题",
    },
    "low_confidence": {
        "name": "召回低置信", "severity": "medium",
        "suggestion": "排序与标定：调 rerank/相似度标定，或加 query 改写让正确卡进 top1",
    },
    "no_data_reference": {
        "name": "没引用", "severity": "high",
        "suggestion": "引用约束：prompt 强化「必须引用」要求 + harness 层校验失败重试（确定性兜底）",
    },
    "wrong_tool": {
        "name": "工具选错", "severity": "medium",
        "suggestion": "工具选择：改写工具 description 让「什么时候用谁」更可判，或减少同质工具",
    },
    "context_degraded": {
        "name": "上下文降级", "severity": "medium",
        "suggestion": "上下文质量：预算重分配或材料压缩；评分场景空上下文 → 补该主题卡片",
    },
    "format_violation": {
        "name": "格式违规", "severity": "high",
        "suggestion": "输出约束：JSON schema 约束强化 + harness 归一化兜底（已有 _normalize_plan 一类）",
    },
    "unattributed": {
        "name": "未归因", "severity": "low",
        "suggestion": "人工分析：多为 judge 校准类偏移，规则覆盖不到，进 v2 LLM 归因的观察名单",
    },
}

_EMPTY_CTX_MARK = "知识库中暂无该主题资料"   # context_builder 的空上下文兜底文案


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _get(d: dict, *keys, default=None):
    """评测报告的 detail 字段名跨版本漂移过（q/question、exp/expected），宽容读取。"""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _failure(source: str, case: str, rule: str, evidence: dict, note: str = "") -> dict:
    meta = RULES[rule]
    return {
        "source": source, "case": case, "rule": rule, "rule_name": meta["name"],
        "severity": meta["severity"], "note": note, "evidence": evidence,
        "suggestion": meta["suggestion"],
    }


def _prefix_join(detail_key: str, cases: list[dict], key_field: str) -> dict | None:
    """detail 里的 case 标识被截断过（[:20]/[:26]），按前缀双向匹配回 case 文件原行。"""
    for c in cases:
        full = str(c.get(key_field) or "")
        if full and (full.startswith(detail_key) or detail_key.startswith(full)):
            return c
    return None


def _load_case_files(evals_dir: Path | None = None) -> dict:
    """把 jsonl 用例装进内存，按用途分组；文件缺失不影响其它组。

    文件路径唯一定义点在 specs/business_lines.yaml（P5）；evals_dir 仅作测试注入兜底。
    """
    out = {"golden": [], "retrieval": [], "gap": [], "agent_tasks": []}
    files = {"golden": "golden_answers", "retrieval": "retrieval_cases",
             "gap": "tool_gap_cases", "agent_tasks": "agent_tasks"}
    for group, name in files.items():
        try:
            p = spec.eval_path(name)
        except KeyError:
            p = (evals_dir or EVALS_DIR) / f"{name}.jsonl"
        if not p.exists():
            continue
        try:
            out[group] = [json.loads(l) for l in
                          p.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:  # noqa: BLE001  用例文件坏了不能带崩归因
            out[group] = []
    return out


# ---------- 各 section 的规则检测（每个返回 list[failure]） ----------

def _attr_scoring(sec: dict | None, golden: list[dict]) -> list[dict]:
    """评分校准失败：先查"空上下文评分"（context_degraded），其余进 unattributed。

    评分 judge 的档位偏移本质是校准问题，六类 Agent 根因覆盖不到，
    强行塞进某一类会让聚类结果失真——所以 unattributed 必须存在。
    """
    if not sec:
        return []
    out = []
    for d in sec.get("details", []):
        if _get(d, "ok", default=True):
            continue
        q = str(_get(d, "q", "question", default=""))
        err = _get(d, "error")
        if err:
            out.append(_failure("scoring", q[:40], "format_violation",
                                {"error": str(err)[:160]},
                                note="评分阶段异常（多为 JSON 解析失败）"))
            continue
        g = _prefix_join(q, golden, "question")
        ctx_probe = ""
        if g:
            try:
                from . import context_builder
                ctx_probe = context_builder.build_eval_context(g.get("topic"), g["question"])
            except Exception:  # noqa: BLE001  探针失败只是丢一条证据
                ctx_probe = ""
        if _EMPTY_CTX_MARK in ctx_probe:
            out.append(_failure("scoring", q[:40], "context_degraded",
                                {"topic": (g or {}).get("topic"), "empty_context": True},
                                note="评分上下文为空：缺料导致的系统性扣分嫌疑"))
        else:
            out.append(_failure("scoring", q[:40], "unattributed",
                                {"expected": _get(d, "exp", "expected"),
                                 "got": _get(d, "got"), "avg": _get(d, "avg")},
                                note="非空上下文下的档位偏移：judge 校准类，规则未覆盖"))
    return out


def _attr_retrieval(sec: dict | None, cases: list[dict]) -> list[dict]:
    """检索对比失败：以门禁口径（hybrid）为准判失败，跨层结果作为证据写进 evidence。"""
    if not sec:
        return []
    out = []
    # 同一 query 在各 mode 下的结果对齐：details 顺序与用例文件一致（run_evals 的实现保证）
    by_mode = {}
    for mode, r in sec.items():
        if not isinstance(r, dict):
            continue
        for d in r.get("details", []):
            by_mode.setdefault(str(_get(d, "q", "query", default="")), {})[mode] = d
    for q, modes in by_mode.items():
        hyb = modes.get("hybrid")
        if hyb is None or _get(hyb, "ok", default=True):
            continue  # 门禁口径没挂，就不算失败（fts 单独挂但不拖累门禁 → 进证据）
        case = _prefix_join(q, cases, "query") or {}
        evidence = {"kind": _get(hyb, "kind") or case.get("kind"),
                    "hybrid_top3": _get(hyb, "top3", default=[]),
                    "expected_card_id": case.get("expected_card_id")}
        cross = []
        if "fts" in modes and _get(modes["fts"], "ok", default=True):
            cross.append("fts 可召回而混合失败：向量召回/融合排序问题")
        if "full" in modes and _get(modes["full"], "ok", default=True):
            cross.append("全链路(改写+rerank)可召回：缺 query 改写即可修复")
        if "fts" in modes and not _get(modes["fts"], "ok", default=True):
            cross.append("FTS 与混合均未召回：倾向知识缺口或索引缺失")
        evidence["cross_layer"] = cross
        out.append(_failure("retrieval", q[:40], "retrieval_miss", evidence,
                            note="；".join(cross) or "混合检索未召回"))
    return out


def _attr_search_tool(sec: dict | None, cases: dict) -> list[dict]:
    """search_kb 工具失败：有卡题 ok=False → 未召回；ok 但 top1 错 → 排序；
    无卡题误标高置信 → 置信度标定。"""
    if not sec:
        return []
    out = []
    covered, gap = cases["retrieval"], cases["gap"]
    for d in sec.get("details", []):
        q = str(_get(d, "q", "query", default=""))
        ok = _get(d, "ok", default=True)
        case = _prefix_join(q, covered, "query") or {}
        if not ok:
            out.append(_failure("search_tool", q[:40], "retrieval_miss",
                                {"kind": _get(d, "kind") or case.get("kind"),
                                 "top3": _get(d, "top3", default=[]),
                                 "expected_card_id": case.get("expected_card_id"),
                                 "confidence": _get(d, "confidence"),
                                 "sim": _get(d, "sim")},
                                note="工具层(top3)未见期望卡"))
        elif not _get(d, "top1", default=True):
            out.append(_failure("search_tool", q[:40], "low_confidence",
                                {"top3": _get(d, "top3", default=[]),
                                 "confidence": _get(d, "confidence"), "sim": _get(d, "sim")},
                                note="召回了但没进 top1：排序问题"))
        elif _get(d, "confidence") == "low":
            out.append(_failure("search_tool", q[:40], "low_confidence",
                                {"top3": _get(d, "top3", default=[]),
                                 "confidence": "low", "sim": _get(d, "sim")},
                                note="命中但低置信：Agent 会被迫按知识缺口处理"))
    for d in sec.get("gap_details", []):
        if not _get(d, "false_confident", default=False):
            continue
        q = str(_get(d, "q", "query", default=""))
        out.append(_failure("search_tool", q[:40], "low_confidence",
                            {"confidence": _get(d, "confidence"), "sim": _get(d, "sim")},
                            note="无卡题被误标高置信：比检索不到更危险（Agent 会以为有卡）"))
    return out


def _attr_agent_tasks(sec: dict | None, cases: list[dict]) -> list[dict]:
    """Agent 任务失败：工具数不够 → wrong_tool；引用缺失 → no_data_reference；
    时间预算超 → format_violation；异常/max_iters → format_violation。"""
    if not sec:
        return []
    out = []
    for d in sec.get("details", []):
        if _get(d, "ok", default=True):
            continue
        task = str(_get(d, "task", default=""))
        case = _prefix_join(task, cases, "task") or {}
        reason = str(_get(d, "reason", default=""))
        if reason and ("异常" in reason or "Error" in reason or "error" in reason):
            out.append(_failure("agent_tasks", task[:40], "format_violation",
                                {"reason": reason[:160]}, note="任务执行异常"))
            continue
        if reason == "max_iters":
            out.append(_failure("agent_tasks", task[:40], "format_violation",
                                {"reason": reason}, note="迭代上限内未产出合法计划"))
            continue
        checks = _get(d, "checks", default={}) or {}
        if not checks.get("data_referenced", True):
            out.append(_failure("agent_tasks", task[:40], "no_data_reference",
                                {"must_mention": case.get("must_mention", []),
                                 "missed_mentions": _get(d, "missed_mentions", default=[]),
                                 "items": _get(d, "items")},
                                note="产出未引用要求的真实数据"))
        if not checks.get("tools", True):
            out.append(_failure("agent_tasks", task[:40], "wrong_tool",
                                {"tools_used": _get(d, "tools", default=[]),
                                 "min_tools": case.get("min_tools")},
                                note="调用工具数不足或全未命中"))
        if not checks.get("time_budget", True):
            out.append(_failure("agent_tasks", task[:40], "format_violation",
                                {"minutes": _get(d, "minutes"),
                                 "max_minutes": case.get("max_minutes")},
                                note="时间预算超限"))
    return out


def _attr_extract(sec: dict | None) -> list[dict]:
    """提取判定失败：异常 → format_violation；判定偏移 → unattributed（judge 校准）。"""
    if not sec:
        return []
    out = []
    for d in sec.get("details", []):
        cid = str(_get(d, "id", default=""))
        err = _get(d, "error")
        if err:
            out.append(_failure("extract_judge", cid, "format_violation",
                                {"error": str(err)[:160]}, note="提取阶段异常"))
            continue
        if not _get(d, "doc_ok", default=True) or _get(d, "mismatch", default=[]):
            out.append(_failure("extract_judge", cid, "unattributed",
                                {"doc": _get(d, "doc"), "pred": _get(d, "pred"),
                                 "exp": _get(d, "exp")},
                                note="判定偏移：judge 校准类，规则未覆盖"))
    return out


# ---------- 轨迹/上下文来源（runs 表 + ctx_traces 表） ----------

_GAP_MARK_RE = re.compile(r"知识缺口|confidence[\"':= ]+none")

def attribute_run(run: dict) -> list[dict]:
    """单条 Agent 运行（runs 表的一行）的轨迹归因。

    steps 里的工具结果只存了 200 字 preview，证据天然受限：
    能确定的（异常/上限/预算裁剪/一个工具都没调）直接归因，
    不确定的（引用缺失）不硬猜——轨迹归因宁可少报不可错报。
    """
    status = run.get("status") or ""
    steps = run.get("steps") or []
    out = []
    rid = run.get("id") or ""
    if status == "error":
        out.append(_failure("runs", rid, "format_violation",
                            {"error": str(run.get("error"))[:160]},
                            note="运行异常退出"))
    elif status == "max_iters":
        out.append(_failure("runs", rid, "format_violation",
                            {"iters": len(steps)}, note="迭代上限内未产出合法输出"))
    elif status == "done":
        tools = [s.get("tool") for s in steps if s.get("tool")]
        data_tools = {"get_daily_brief", "list_due_cards", "list_open_mistakes",
                      "get_weak_topics", "get_top_questions", "search_kb"}
        if not (set(tools) & data_tools):
            out.append(_failure("runs", rid, "wrong_tool",
                                {"tools_used": tools},
                                note="产出计划前未调用任何数据工具"))
        result = run.get("result") or {}
        if isinstance(result, dict) and result.get("trimmed"):
            out.append(_failure("runs", rid, "format_violation",
                                {"total_minutes": result.get("total_minutes"),
                                 "dropped": result.get("dropped", [])},
                                note="预算违规：模型未自我修正，由代码裁剪兜底"))
        for s in steps:
            if s.get("tool") == "search_kb" and _GAP_MARK_RE.search(str(s.get("result_preview") or "")):
                out.append(_failure("runs", rid, "retrieval_miss",
                                    {"query": (s.get("args") or {}).get("query", ""),
                                     "evidence": "result_preview"},
                                    note="search_kb 返回知识缺口（轨迹内证据，200 字截断）"))
    return out


def attribute_recent_runs(limit: int = 30) -> list[dict]:
    """最近 N 条 runs 的归因（study_plan 场景）。"""
    from ..core.db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,kind,status,steps_json,result_json FROM runs "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        run = {"id": r["id"], "status": r["status"],
               "steps": json.loads(r["steps_json"] or "[]"),
               "result": json.loads(r["result_json"]) if r["result_json"] else None}
        out.extend(attribute_run(run))
    return out


def attribute_ctx_degradations(limit: int = 50) -> list[dict]:
    """最近的上下文降级记录（ctx_traces）：degraded 非空本身就是 context_degraded 证据。"""
    from ..core.db import get_conn
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT session_id,seq,degraded_json,fallback,retrieved_n FROM ctx_traces "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        degraded = json.loads(r["degraded_json"] or "[]")
        if not degraded and not r["fallback"]:
            continue
        out.append(_failure("ctx_traces", f"{r['session_id']}#{r['seq']}", "context_degraded",
                            {"degraded": degraded, "fallback": r["fallback"],
                             "retrieved_n": r["retrieved_n"]},
                            note="上下文装配降级"))
    return out


# ---------- 聚类与报告组装 ----------

def cluster(failures: list[dict]) -> dict:
    """按规则聚合 + 按来源×维度细分 + 共现模式。"""
    by_rule: dict[str, dict] = {}
    for f in failures:
        g = by_rule.setdefault(f["rule"], {"count": 0, "severity": f["severity"],
                                           "sources": {}, "cases": []})
        g["count"] += 1
        g["sources"][f["source"]] = g["sources"].get(f["source"], 0) + 1
        if len(g["cases"]) < 20:
            g["cases"].append(f["case"])
    total = len(failures) or 1
    by_rule = {k: {**v, "share": round(v["count"] / total, 3)}
               for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1]["count"])}

    # 共现：同一次运行/同一案例触发多类根因（如 没引用 + 工具选错 → 选择策略问题）
    co: dict[tuple, int] = {}
    groups: dict[str, list[str]] = {}
    for f in failures:
        key = f"{f['source']}:{f['case']}"
        groups.setdefault(key, [])
        if f["rule"] not in groups[key]:
            groups[key].append(f["rule"])
    for rules in groups.values():
        if len(rules) > 1:
            co[tuple(sorted(rules))] = co.get(tuple(sorted(rules)), 0) + 1
    patterns = [{"rules": list(k), "count": v,
                 "note": "同案例共现：单独修一类可能无效，需联合处理"}
                for k, v in sorted(co.items(), key=lambda kv: -kv[1])]

    return {"by_rule": by_rule, "patterns": patterns}


def _top_issues(failures: list[dict], top_n: int = 5) -> list[dict]:
    cl = cluster(failures)["by_rule"]
    ranked = sorted(cl.items(),
                    key=lambda kv: -kv[1]["count"] * SEVERITY_WEIGHT.get(kv[1]["severity"], 1))
    return [{"rule": k, "name": RULES[k]["name"], "count": v["count"],
             "severity": v["severity"], "sources": v["sources"],
             "suggestion": RULES[k]["suggestion"]} for k, v in ranked[:top_n]]


def _optimizer_input(failures: list[dict]) -> dict:
    """给 P4 优化执行器的结构化输入：按动作类型聚合，而不是让 P4 再解析一遍明细。"""
    ingest_targets, prompt_fixes, calibration_watch = [], [], []
    for f in failures:
        if f["rule"] == "retrieval_miss" and not f["evidence"].get("cross_layer"):
            ingest_targets.append(f["case"])
        elif f["rule"] in ("no_data_reference", "wrong_tool"):
            prompt_fixes.append({"case": f["case"], "rule": f["rule"],
                                 "suggestion": f["suggestion"]})
        elif f["rule"] == "unattributed":
            calibration_watch.append(f["case"])
    seen = set()
    ingest_targets = [x for x in ingest_targets if not (x in seen or seen.add(x))]
    return {"ingest_targets": ingest_targets[:20],
            "prompt_fixes": prompt_fixes[:20],
            "calibration_watch": calibration_watch[:20]}


def digest_md(report: dict, max_per_rule: int = 5) -> str:
    """人读摘要（也是 P6 复盘中心的素材源）。"""
    s = report["summary"]
    lines = [f"# 归因报告 · {report['generated_at']}",
             f"> 来源：{report.get('source', '-')}｜失败 {s['failures']} 条 / 扫描案例 {s['cases_scanned']}"
             f"（失败率 {s['failure_rate']}）", ""]
    if report["top_issues"]:
        lines += ["## 修复优先级", ""]
        for i, t in enumerate(report["top_issues"], 1):
            lines.append(f"{i}. **[{t['severity']}] {t['name']}（{t['rule']}）× {t['count']}**"
                         f"  来源 {t['sources']}  → {t['suggestion']}")
        lines.append("")
    pats = report["clusters"]["patterns"]
    if pats:
        lines += ["## 共现模式", ""]
        for p in pats[:3]:
            lines.append(f"- {' + '.join(p['rules'])} × {p['count']}")
        lines.append("")
    by_rule = report["clusters"]["by_rule"]
    if by_rule:
        lines += ["## 明细（每类最多 {} 条）".format(max_per_rule), ""]
        for rule, g in by_rule.items():
            lines.append(f"### {RULES[rule]['name']}（{rule}）× {g['count']}")
            for c in g["cases"][:max_per_rule]:
                lines.append(f"- {c}")
            lines.append("")
    return "\n".join(lines)


def build_failure_report(eval_report: dict | None = None,
                         eval_report_path: Path | None = None,
                         include_runs: bool = False,
                         include_ctx: bool = False,
                         evals_dir: Path = EVALS_DIR) -> dict:
    """组装完整归因报告。

    eval_report 缺省时从 eval_report_path（或目录里最新的 results-*.json）读取。
    include_runs / include_ctx 打开轨迹与上下文降级两个补充来源（默认关：评测报告归因是主路径）。
    """
    if eval_report is None:
        path = eval_report_path
        if path is None:
            candidates = sorted(evals_dir.glob("results-*.json"))
            if not candidates:
                raise ValueError("没有可归因的评测报告（evals/results-*.json）")
            path = candidates[-1]
        eval_report = json.loads(path.read_text(encoding="utf-8"))
        source = path.name
    else:
        source = "in-memory"

    cases = _load_case_files(evals_dir)
    failures: list[dict] = []
    failures += _attr_scoring(eval_report.get("scoring"), cases["golden"])
    failures += _attr_retrieval(eval_report.get("retrieval"), cases["retrieval"])
    failures += _attr_search_tool(eval_report.get("search_tool"), cases)
    failures += _attr_agent_tasks(eval_report.get("agent_tasks"), cases["agent_tasks"])
    failures += _attr_extract(eval_report.get("extract_judge"))
    if include_runs:
        failures += attribute_recent_runs()
    if include_ctx:
        failures += attribute_ctx_degradations()

    cases_scanned = sum(
        len((eval_report.get(k) or {}).get("details", []))
        for k in ("scoring", "search_tool", "agent_tasks", "extract_judge"))
    cases_scanned += sum(len((r or {}).get("details", []))
                         for r in (eval_report.get("retrieval") or {}).values())
    n = len(failures)
    report = {
        "generated_at": _now(),
        "source": source,
        "engine": "attribution-v1(rules)",
        "summary": {"cases_scanned": cases_scanned, "failures": n,
                    "failure_rate": round(n / cases_scanned, 3) if cases_scanned else 0.0,
                    "by_severity": {s: sum(1 for f in failures if f["severity"] == s)
                                    for s in ("high", "medium", "low")}},
        "failures": failures,
        "clusters": cluster(failures),
        "top_issues": _top_issues(failures),
        "optimizer_input": _optimizer_input(failures),
    }
    report["digest_md"] = digest_md(report)
    return report


def save_failure_report(report: dict, evals_dir: Path = EVALS_DIR) -> Path:
    """归因报告落盘（与评测报告同目录，attribution-*.json）。"""
    evals_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = evals_dir / f"attribution-{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
