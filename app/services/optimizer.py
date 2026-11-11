"""优化执行器 v1（P4）：归因根因 → prompt 候选改写 → 评测门禁 → 自动晋级/回滚。

自进化闭环（评测 → 归因 → 分析 → 优化 → 再评测）的第四环。
P3 归因回答"为什么挂"，本模块回答"改什么、怎么改、改完怎么证明真的好了"。

设计决策：
1. **运行时唯一事实源是 prompts/*.yaml 文件**，prompt_versions 表只记生命周期与证据。
   文件之外的"第二事实源"迟早漂移（MCP/eval 的历史教训），所以上机 = 换文件，回滚 = 还原快照。
2. **候选必须先过确定性校验再上机**：占位符集合不变（渲染不炸）、输出 schema 字段仍在
   （JSON 契约不破）、长度有界（防跑飞）。LLM 改写是概率性的，能用规则挡住的绝不放行。
3. **晋级判据 = 门禁 AND 不劣于基线**：只看 gate 会在"本来就挂的指标"上误判晋级
   （候选没改好但 gate 挂点和之前一样），基线对比按 scoped 指标 ±0.02 容差逐项核。
4. **scoped eval**：改 study_plan 只跑 agent_tasks，不为一个候选付全套评测的 LLM 成本。
5. **改动可追溯**：每次候选记录 reason（哪些根因触发）、changes（LLM 自述改了什么）、
   eval_report（证据路径）、baseline_metrics/regressions（对比证据）——
   面试讲这个闭环时，每一环都有数据可指。

v1 只自动改 prompt（agent_tools 的 description 改写、记忆策略调整进 v2：
工具改写会改变模型行为面，需要先有工具级评测，否则晋级判据不成立）。
"""
import datetime
import json
import shutil
import string
from pathlib import Path

import yaml

from ..core import config, llm, prompts
from ..core.db import get_conn
from . import eval_suite

EVALS_DIR = config.BASE_DIR / "evals"
SNAPSHOT_DIRNAME = ".snapshots"

# 哪些 prompt 可以被自动优化：评测 scope（scoped eval 跑哪些 section）+ 触发它的失败来源
# + 允许触发的根因（unattributed = judge 校准类，也是 prompt 可修的）。
OPTIMIZABLE = {
    "study_plan": {
        "scope": ("agent_tasks",),
        "fail_sources": ("agent_tasks",),
        "fail_rules": ("no_data_reference", "wrong_tool", "format_violation",
                       "retrieval_miss", "unattributed"),
        "describe": "学习教练 Agent（工具调用 + 计划产出）",
    },
    "evaluate_answer": {
        "scope": ("scoring",),
        "fail_sources": ("scoring",),
        "fail_rules": ("unattributed", "context_degraded"),
        "describe": "回答评分 judge",
    },
    "extract_interview": {
        "scope": ("extract_judge",),
        "fail_sources": ("extract_judge",),
        "fail_rules": ("unattributed", "format_violation"),
        "describe": "面经抽取判定",
    },
}

# scoped 指标与方向：min = 越大越好，max = 越小越好。retrieval 的指标嵌套在 hybrid 下。
SECTION_METRICS = {
    "scoring": {"band_accuracy": "min", "json_validity": "min", "lift": "min"},
    "agent_tasks": {"success_rate": "min"},
    "extract_judge": {"doc_type_accuracy": "min", "completeness_accuracy": "min",
                      "complete_precision": "min", "bare_recall": "min"},
    "search_tool": {"hit3": "min", "top1": "min", "gap_false_confident": "max"},
    "retrieval": {"hit5": "min"},   # 取 retrieval.hybrid.hit5
}
TOLERANCE = 0.02   # min 指标的容差：噪声抖动内不算回归


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------- 1. 计划：归因报告 → 优化动作 ----------

def plan(attribution_report: dict) -> dict:
    """从归因报告生成优化计划。

    自动项：可优化 prompt 的候选改写（有对应失败才进）；
    人工项：ingest_targets（补卡走审批闸门，不直写知识库）与 v2 方向备注。
    """
    candidates, manual_notes = [], []
    for prompt_name, spec in OPTIMIZABLE.items():
        hits = [f for f in attribution_report.get("failures", [])
                if f.get("source") in spec["fail_sources"] and f.get("rule") in spec["fail_rules"]]
        if not hits:
            continue
        rules = sorted({h["rule"] for h in hits})
        from . import attribution as attr_mod
        hints = list(dict.fromkeys(attr_mod.RULES[r]["suggestion"] for r in rules))
        candidates.append({
            "prompt": prompt_name, "describe": spec["describe"], "scope": list(spec["scope"]),
            "failures": len(hits), "rules": rules, "hints": hints,
            "cases": [h["case"] for h in hits[:8]],
            "reason": f"归因触发：{len(hits)} 条失败（{'、'.join(rules)}）",
        })
    oi = attribution_report.get("optimizer_input") or {}
    if oi.get("ingest_targets"):
        manual_notes.append(f"知识缺口 {len(oi['ingest_targets'])} 条待补卡（走审批闸门，不自动入库）："
                            + "；".join(oi["ingest_targets"][:5]))
    manual_notes.append("工具 description 改写与记忆策略调整为 v2 方向：需先有工具级评测才能成立晋级判据。")
    return {"generated_at": _now(), "candidates": candidates, "manual_notes": manual_notes}


# ---------- 2. 候选校验（确定性规则，LLM 改写的放行闸） ----------

def _placeholders(text: str) -> set[str]:
    """string.Formatter 解析出 {变量} 集合；{{ }} 转义不计入。"""
    return {fname for _, fname, _, _ in string.Formatter().parse(text) if fname}


def _schema_keys_in_system(system: str, schema: dict) -> list[str]:
    """output_schema 声明的顶层键必须还在 system 的输出说明里（JSON 契约不破）。"""
    return [k for k in (schema or {}) if k not in system]


def validate_candidate(orig_system: str, orig_user: str, new_system: str,
                       orig_schema: dict) -> list[str]:
    """校验候选 system，返回问题清单（空 = 通过）。全部确定性规则，零 LLM。"""
    problems = []
    missing = _placeholders(orig_system) - _placeholders(new_system)
    if missing:
        problems.append(f"占位符丢失（渲染会炸）: {missing}")
    extra = _placeholders(new_system) - _placeholders(orig_system) - _placeholders(orig_user)
    if extra:
        problems.append(f"引入了未知占位符: {extra}")
    # 用哑变量真渲染一次：捕获非法 format 语法（孤括号/位置混用）
    dummy = {k: "x" for k in _placeholders(new_system) | _placeholders(orig_user)}
    try:
        new_system.format(**dummy)
    except (KeyError, ValueError, IndexError) as e:
        problems.append(f"format 渲染失败（花括号转义坏了）: {e}")
    broken = _schema_keys_in_system(new_system, orig_schema)
    if broken:
        problems.append(f"输出 schema 字段说明丢失（JSON 契约破坏）: {broken}")
    ratio = len(new_system) / max(1, len(orig_system))
    if ratio > 1.8:
        problems.append(f"长度膨胀 {ratio:.2f}x（>1.8x，改写跑飞）")
    if ratio < 0.5:
        problems.append(f"长度缩水 {ratio:.2f}x（<0.5x，疑似丢内容）")
    return problems


# ---------- 3. 起草候选 ----------

def _rewrite_llm(system: str, failures: str, cases: str, hints: str) -> dict:
    """LLM 改写入口（独立成函数方便测试注入 fake）。"""
    system_p, user_p, _ = prompts.render("prompt_rewrite", system=system,
                                         failures=failures, cases=cases, hints=hints)
    return llm.chat_parsed(system_p, user_p)


def _dump_prompt_yaml(data: dict) -> str:
    """手写 block 风格（system/user 用 | 块），人能直接读改；schema 片段交给 safe_dump。"""
    def block(key: str, text: str) -> list[str]:
        text = str(text)
        # clip(|) 会给值补一个尾部换行；原文不带尾换行时用 strip(|-) 才能往返一致
        chomp = "|" if text.endswith("\n") else "|-"
        lines = [f"{key}: {chomp}"]
        for l in text.splitlines() or [""]:
            lines.append(f"  {l}" if l.strip() else "")
        return lines
    schema = data.get("output_schema") or {}
    schema_text = yaml.safe_dump(schema, allow_unicode=True, sort_keys=False,
                                 default_flow_style=False, width=10**6).rstrip()
    lines = [f"name: {data['name']}", f"version: {data['version']}",
             f"description: {data.get('description', '')}"]
    lines += block("system", data["system"])
    lines += block("user", data["user"])
    lines.append("output_schema:")
    lines += [f"  {l}" for l in schema_text.splitlines()]
    return "\n".join(lines) + "\n"


def _save_candidate(prompt_name: str, version: int, parent_version: int,
                    system: str, user: str, schema: dict, description: str,
                    reason: str, changes: dict, source: str = "optimizer") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO prompt_versions (prompt_name,version,parent_version,system_text,user_text,"
            "output_schema,description,status,source,reason,changes_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (prompt_name, version, parent_version, system, user,
             json.dumps(schema, ensure_ascii=False), description,
             "candidate", source, reason, json.dumps(changes, ensure_ascii=False), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def draft_candidate(prompt_name: str, plan_entry: dict,
                    prompts_dir: Path | None = None) -> dict:
    """为某个 prompt 起草候选版本：LLM 改写 → 确定性校验 → 落表（不动运行文件）。

    校验失败也落表（status=rejected）——失败原因本身是调改写 prompt 的训练数据。
    """
    prompts_dir = prompts_dir or prompts.PROMPTS_DIR
    src_path = prompts_dir / f"{prompt_name}.yaml"
    src = yaml.safe_load(src_path.read_text(encoding="utf-8"))

    failures_text = plan_entry.get("reason", "")
    cases_text = "\n".join(f"- {c}" for c in plan_entry.get("cases", [])) or "（无明细）"
    hints_text = "\n".join(f"- {h}" for h in plan_entry.get("hints", []))

    try:
        data = _rewrite_llm(src["system"], failures_text, cases_text, hints_text)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"LLM 改写失败: {type(e).__name__}: {e}"}

    new_system = str(data.get("system") or "").strip()
    if not new_system:
        return {"ok": False, "error": "改写器返回空 system"}
    problems = validate_candidate(src["system"], src["user"], new_system, src.get("output_schema") or {})

    cur = prompts.load(prompt_name)
    row_id = _save_candidate(
        prompt_name, cur.version + 1, cur.version, new_system, src["user"],
        src.get("output_schema") or {}, src.get("description", ""),
        reason=plan_entry.get("reason", ""), changes={
            "changes": data.get("changes", []), "rationale": data.get("rationale", ""),
            "validation_problems": problems})
    if problems:
        conn = get_conn()
        try:
            conn.execute("UPDATE prompt_versions SET status='rejected' WHERE id=?", (row_id,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": False, "candidate_id": row_id, "problems": problems}
    return {"ok": True, "candidate_id": row_id, "version": cur.version + 1,
            "changes": data.get("changes", [])}


# ---------- 4. 上机评测：文件快照交换 → scoped eval → 晋级/回滚 ----------

def _metrics(report: dict, section: str) -> dict:
    """抽 scoped 指标；retrieval 的指标在 hybrid 子树下。缺 section 返回空 dict。"""
    if section == "retrieval":
        return {"hit5": ((report.get("retrieval") or {}).get("hybrid") or {}).get("hit5")}
    sec = report.get(section) or {}
    return {k: sec.get(k) for k in SECTION_METRICS.get(section, {}) if sec.get(k) is not None}


def compare_baseline(candidate_report: dict, baseline_report: dict,
                     sections: list[str]) -> list[str]:
    """逐项对比 scoped 指标，返回回归清单（空 = 不劣于基线）。

    只看 gate 会在"本来就挂的指标"上误判晋级，所以晋级判据是 gate AND 本函数为空。
    """
    regressions = []
    for sec in sections:
        cand, base = _metrics(candidate_report, sec), _metrics(baseline_report, sec)
        for metric, direction in SECTION_METRICS.get(sec, {}).items():
            cv, bv = cand.get(metric), base.get(metric)
            if cv is None or bv is None:
                continue  # 基线缺该指标（如 skip-llm 报告）只跳过对比，不冤枉候选
            if direction == "min" and cv < round(bv - TOLERANCE, 4):
                regressions.append(f"{sec}.{metric}: {cv} < 基线 {bv}（容差 {TOLERANCE}）")
            if direction == "max" and cv > bv:
                regressions.append(f"{sec}.{metric}: {cv} > 基线 {bv}（越小越好，无容差）")
    return regressions


def latest_results(evals_dir: Path = EVALS_DIR) -> dict | None:
    files = sorted(evals_dir.glob("results-*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _swap_in_candidate(prompt_name: str, cand: dict, prompts_dir: Path) -> Path:
    """上机：当前文件存快照 → 写候选。返回快照路径。文件版本不符则拒绝（防覆盖别人改动）。"""
    src_path = prompts_dir / f"{prompt_name}.yaml"
    src_data = yaml.safe_load(src_path.read_text(encoding="utf-8"))
    if int(src_data["version"]) != int(cand["parent_version"]):
        raise RuntimeError(
            f"文件版本 {src_data['version']} != 候选父版本 {cand['parent_version']}，"
            "文件在起草后被改过，拒绝覆盖（先处理版本冲突）")
    snap_dir = prompts_dir / SNAPSHOT_DIRNAME
    snap_dir.mkdir(exist_ok=True)
    snap = snap_dir / f"{prompt_name}-v{cand['parent_version']}-{datetime.datetime.now():%Y%m%d%H%M%S}.yaml"
    shutil.copyfile(src_path, snap)
    new_data = {"name": prompt_name, "version": cand["version"],
                "description": cand.get("description") or src_data.get("description", ""),
                "system": cand["system_text"], "user": cand["user_text"],
                "output_schema": json.loads(cand["output_schema"] or "{}")}
    src_path.write_text(_dump_prompt_yaml(new_data), encoding="utf-8")
    prompts.load.cache_clear()   # load 有 lru_cache，不清理会继续用旧 prompt
    return snap


def _restore_snapshot(snapshot: Path, prompt_name: str, prompts_dir: Path):
    shutil.copyfile(snapshot, prompts_dir / f"{prompt_name}.yaml")
    prompts.load.cache_clear()


def evaluate_candidate(candidate_id: int, baseline_report: dict | None = None,
                       run_eval=None, prompts_dir: Path | None = None,
                       persist_report: bool = True) -> dict:
    """候选上机评测并自动晋级/回滚。run_eval 可注入（测试用），缺省 eval_suite.run_suite。

    persist_report=False 时评测报告只留在内存（测试用），不写 evals/results-*.json——
    假报告混进真实评测历史会污染后续的基线选取。

    决策规则：scoped gate 通过 AND 基线对比无回归 → 晋级（status=active）；
    否则回滚（status=rolled_back，文件恢复快照）。评测中途抛异常也回滚——
    文件状态必须与表状态一致，这是本模块最硬的不变量。
    """
    prompts_dir = prompts_dir or prompts.PROMPTS_DIR
    run_eval = run_eval or eval_suite.run_suite
    conn = get_conn()
    try:
        cand = dict(conn.execute("SELECT * FROM prompt_versions WHERE id=?", (candidate_id,)).fetchone())
    finally:
        conn.close()
    if not cand:
        return {"ok": False, "error": f"候选不存在: {candidate_id}"}
    if cand["status"] != "candidate":
        return {"ok": False, "error": f"候选状态为 {cand['status']}，只有 candidate 可上机"}

    spec = OPTIMIZABLE[cand["prompt_name"]]
    prompt_name = cand["prompt_name"]

    try:
        snap = _swap_in_candidate(prompt_name, cand, prompts_dir)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    conn = get_conn()
    try:
        conn.execute("UPDATE prompt_versions SET snapshot_path=? WHERE id=?",
                     (str(snap), candidate_id))
        conn.commit()
    finally:
        conn.close()

    baseline = baseline_report or latest_results()
    outcome = {"ok": True, "candidate_id": candidate_id, "prompt": prompt_name}
    try:
        report = run_eval(list(spec["scope"]))
        if persist_report:
            out_path = EVALS_DIR / f"results-{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            out_path = None
        gate_pass, gate_fails = eval_suite.gate(report)
        regressions = compare_baseline(report, baseline or {}, list(spec["scope"]))
        promoted = gate_pass and not regressions
        outcome.update({"eval_report": str(out_path) if out_path else None, "gate_passed": gate_pass,
                        "gate_fails": gate_fails, "regressions": regressions,
                        "metrics": {sec: _metrics(report, sec) for sec in spec["scope"]},
                        "decision": "promote" if promoted else "rollback"})
        conn = get_conn()
        try:
            if promoted:
                conn.execute("UPDATE prompt_versions SET status='retired' "
                             "WHERE prompt_name=? AND status='active'", (prompt_name,))
            conn.execute(
                "UPDATE prompt_versions SET status=?, eval_report=?, gate_passed=?, "
                "baseline_metrics=?, regressions=?, evaluated_at=?, decided_at=? WHERE id=?",
                ("active" if promoted else "rolled_back",
                 str(out_path) if out_path else None, int(gate_pass),
                 json.dumps({sec: _metrics(baseline or {}, sec) for sec in spec["scope"]},
                            ensure_ascii=False) if baseline else None,
                 json.dumps(regressions, ensure_ascii=False) if regressions else None,
                 _now(), _now(), candidate_id))
            conn.commit()
        finally:
            conn.close()
        if not promoted:   # 先落库决策再还原文件，两头都失败时表里也留了痕迹
            _restore_snapshot(snap, prompt_name, prompts_dir)
    except Exception as e:  # noqa: BLE001  评测炸了必须还原文件，否则运行时已换血
        _restore_snapshot(snap, prompt_name, prompts_dir)
        conn = get_conn()
        try:
            conn.execute("UPDATE prompt_versions SET status='rolled_back', decided_at=?, "
                         "regressions=? WHERE id=?",
                         (_now(), json.dumps([f"评测异常: {type(e).__name__}: {e}"],
                                             ensure_ascii=False), candidate_id))
            conn.commit()
        finally:
            conn.close()
        outcome.update({"ok": False, "decision": "rollback", "error": f"{type(e).__name__}: {e}"})
    return outcome


# ---------- 5. 查询与手动回滚 ----------

def list_versions(prompt_name: str | None = None, limit: int = 30) -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM prompt_versions"
        args: tuple = ()
        if prompt_name:
            sql += " WHERE prompt_name=?"
            args = (prompt_name,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(sql, args + (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["output_schema"] = json.loads(d.pop("output_schema") or "{}")
            d["changes"] = json.loads(d.pop("changes_json") or "{}")
            d["regressions"] = json.loads(d.pop("regressions") or "[]")
            d.pop("system_text"), d.pop("user_text")   # 明细太长，CLI 里按需看
            out.append(d)
        return out
    finally:
        conn.close()


def manual_rollback(candidate_id: int, prompts_dir: Path | None = None) -> dict:
    """把某个已上机候选（active）手动回滚：文件恢复其快照。"""
    prompts_dir = prompts_dir or prompts.PROMPTS_DIR
    conn = get_conn()
    try:
        cand = dict(conn.execute("SELECT * FROM prompt_versions WHERE id=?", (candidate_id,)).fetchone())
    finally:
        conn.close()
    if not cand or cand["status"] != "active":
        return {"ok": False, "error": "只有 active 状态的候选可手动回滚"}
    snap = Path(cand["snapshot_path"]) if cand["snapshot_path"] else None
    if not snap or not snap.exists():
        return {"ok": False, "error": f"快照缺失: {snap}"}
    _restore_snapshot(snap, cand["prompt_name"], prompts_dir)
    conn = get_conn()
    try:
        conn.execute("UPDATE prompt_versions SET status='rolled_back', decided_at=? WHERE id=?",
                     (_now(), candidate_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "candidate_id": candidate_id, "restored_from": str(snap)}
