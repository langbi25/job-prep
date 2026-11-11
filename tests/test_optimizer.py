"""优化执行器（P4）测试：注入假 eval 与假 LLM，文件交换在临时目录，不碰真实 prompts/。

覆盖：
1. plan()：归因失败 → 候选计划映射（来源×规则 → 目标 prompt），人工项（补卡）不进自动候选；
2. validate_candidate()：占位符丢失 / 花括号转义坏 / schema 字段丢 / 长度跑飞，全部确定性拦截；
3. draft_candidate()：注入假 LLM——合格改写落为 candidate，违规改写落为 rejected；
4. evaluate_candidate()：文件快照交换 + scoped gate + 基线对比 →
   好候选晋级（文件版本已换）、坏候选回滚（文件还原快照）、版本冲突拒上机；
5. compare_baseline()：min 指标 ±0.02 容差、max 指标无容差、基线缺指标跳过。
"""
import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml

from app.core.db import get_conn, init_db
from app.core import prompts
from app.services import optimizer as opt


def _rules_of(fs):
    return sorted(f["rule"] for f in fs)


# ---------- 1. plan 映射 ----------

def test_plan_mapping():
    ar = {"failures": [
        {"source": "agent_tasks", "rule": "no_data_reference", "case": "bigdata 学习计划",
         "note": "产出未引用要求的真实数据", "severity": "high"},
        {"source": "agent_tasks", "rule": "wrong_tool", "case": "backend 学习计划",
         "note": "调用工具数不足", "severity": "medium"},
        {"source": "scoring", "rule": "unattributed", "case": "Spark shuffle…",
         "note": "judge 校准类", "severity": "low"},
        {"source": "retrieval", "rule": "retrieval_miss", "case": "Redis 断电…",
         "note": "混合检索未召回", "severity": "high"},
    ], "optimizer_input": {"ingest_targets": ["Redis 断电…"], "prompt_fixes": [],
                            "calibration_watch": []}}
    p = opt.plan(ar)
    targets = sorted(c["prompt"] for c in p["candidates"])
    # retrieval_miss 来源是 retrieval（不是 agent_tasks）→ 不触发 study_plan
    assert targets == ["evaluate_answer", "study_plan"], targets
    sp = next(c for c in p["candidates"] if c["prompt"] == "study_plan")
    assert sorted(sp["rules"]) == ["no_data_reference", "wrong_tool"]
    assert sp["scope"] == ["agent_tasks"]
    assert any("补卡" in n for n in p["manual_notes"])
    # 无失败 → 无候选
    assert opt.plan({"failures": [], "optimizer_input": {}})["candidates"] == []


# ---------- 2. 候选校验 ----------

def test_validate_candidate():
    orig = "你是教练，方向是 {direction}。输出 JSON：{{\"plan\": []}}。至少 60 字内容占位占位占位。"
    schema = {"plan": "list"}
    # 合格：占位符保留、schema 键在、长度接近
    assert opt.validate_candidate(orig, "目标 {direction}", orig, schema) == []
    # 占位符丢失
    bad = orig.replace("{direction}", "固定方向")
    ps = opt.validate_candidate(orig, "目标 {direction}", bad, schema)
    assert any("占位符丢失" in p for p in ps), ps
    # 花括号转义坏（{{ 变 { ，format 会炸或改变语义）
    bad2 = "你是教练，方向是 {direction}。输出 JSON：{\"plan\": []}。至少 60 字内容占位占位占位。"
    ps = opt.validate_candidate(orig, "目标 {direction}", bad2, schema)
    assert any("渲染失败" in p or "占位符" in p for p in ps), ps
    # schema 字段丢失
    bad3 = "你是教练，方向是 {direction}。输出 JSON：{{}}。至少 60 字内容占位占位占位。"
    ps = opt.validate_candidate(orig, "目标 {direction}", bad3, schema)
    assert any("schema" in p for p in ps), ps
    # 长度跑飞
    bad4 = "你是教练，方向是 {direction}。输出 JSON：{{\"plan\": []}}。" + "水" * 500
    ps = opt.validate_candidate(orig, "目标 {direction}", bad4, schema)
    assert any("膨胀" in p for p in ps), ps


# ---------- 3. 起草（注入假 LLM） ----------

def test_draft_candidate():
    init_db()
    orig_prompts_dir = prompts.PROMPTS_DIR
    with tempfile.TemporaryDirectory() as td:
        tdir = pathlib.Path(td)
        shutil.copyfile(orig_prompts_dir / "study_plan.yaml", tdir / "study_plan.yaml")
        prompts.PROMPTS_DIR = tdir          # prompts.load 按 PROMPTS_DIR 读，测试期指向临时目录
        prompts.load.cache_clear()
        try:
            orig = yaml.safe_load((tdir / "study_plan.yaml").read_text(encoding="utf-8"))
            plan_entry = {"reason": "归因触发：2 条失败（no_data_reference、wrong_tool）",
                          "cases": ["bigdata 学习计划"], "hints": ["引用约束强化"]}

            good_system = orig["system"] + "\n\n新增硬性要求：每条 plan 的 reason 必须含卡片 id 或错题原文，缺引用的条目直接丢弃。"
            opt._rewrite_llm = lambda *a, **k: {"system": good_system,
                                                 "changes": ["加引用硬约束 ← 没引用"],
                                                 "rationale": "x"}
            d = opt.draft_candidate("study_plan", plan_entry, prompts_dir=tdir)
            assert d["ok"], d
            row = opt.list_versions("study_plan")[0]
            assert row["status"] == "candidate" and row["version"] == orig["version"] + 1

            # 违规改写（整个 key_findings 字段说明被删 → JSON 契约破坏）→ rejected 落表
            bad_system = orig["system"].replace("key_findings", "") + "\n再改一点。"
            opt._rewrite_llm = lambda *a, **k: {"system": bad_system, "changes": [], "rationale": ""}
            d2 = opt.draft_candidate("study_plan", plan_entry, prompts_dir=tdir)
            assert not d2["ok"] and d2["problems"]
            row2 = opt.list_versions("study_plan")[0]
            assert row2["status"] == "rejected"
            assert row2["changes"]["validation_problems"]
        finally:
            prompts.PROMPTS_DIR = orig_prompts_dir
            prompts.load.cache_clear()


# ---------- 4. 上机评测：晋级 / 回滚 / 版本冲突 ----------

def test_evaluate_promote_and_rollback():
    init_db()
    orig_prompts_dir = prompts.PROMPTS_DIR
    with tempfile.TemporaryDirectory() as td:
        tdir = pathlib.Path(td)
        shutil.copyfile(orig_prompts_dir / "study_plan.yaml", tdir / "study_plan.yaml")
        prompts.PROMPTS_DIR = tdir
        prompts.load.cache_clear()
        try:
            orig = yaml.safe_load((tdir / "study_plan.yaml").read_text(encoding="utf-8"))
            v_orig = orig["version"]

            # --- 候选 A：评测好 → 晋级，文件版本已换成候选
            sys_a = orig["system"] + "\n硬性要求：reason 必须含具体 id。"
            cid_a = opt._save_candidate("study_plan", v_orig + 1, v_orig, sys_a, orig["user"],
                                        orig.get("output_schema") or {}, orig.get("description", ""),
                                        reason="test", changes={}, source="test")
            good_eval = lambda sections: {"agent_tasks": {"cases": 2, "success_rate": 1.0, "details": []}}
            out = opt.evaluate_candidate(cid_a, baseline_report={"agent_tasks": {"success_rate": 0.5}},
                                         run_eval=good_eval, prompts_dir=tdir, persist_report=False)
            assert out["decision"] == "promote", out
            now = yaml.safe_load((tdir / "study_plan.yaml").read_text(encoding="utf-8"))
            assert now["version"] == v_orig + 1 and "必须含具体 id" in now["system"]
            assert opt.list_versions("study_plan")[0]["status"] == "active"

            # 快照内容 = 晋级前的原文
            row_a = next(r for r in opt.list_versions("study_plan") if r["id"] == cid_a)
            snap = yaml.safe_load(pathlib.Path(row_a["snapshot_path"]).read_text(encoding="utf-8"))
            assert snap["version"] == v_orig

            # --- 候选 B：评测差（gate 挂 + 回归）→ 回滚，文件还原为 v_orig+1 的内容
            sys_b = now["system"] + "\n再加一条无用内容使版本递增。"
            cid_b = opt._save_candidate("study_plan", v_orig + 2, v_orig + 1, sys_b, orig["user"],
                                        orig.get("output_schema") or {}, orig.get("description", ""),
                                        reason="test", changes={}, source="test")
            bad_eval = lambda sections: {"agent_tasks": {"cases": 2, "success_rate": 0.0, "details": []}}
            out = opt.evaluate_candidate(cid_b, baseline_report={"agent_tasks": {"success_rate": 0.9}},
                                         run_eval=bad_eval, prompts_dir=tdir, persist_report=False)
            assert out["decision"] == "rollback", out
            assert out["gate_passed"] is False and out["regressions"]
            restored = yaml.safe_load((tdir / "study_plan.yaml").read_text(encoding="utf-8"))
            assert restored["version"] == v_orig + 1 and restored["system"] == sys_a
            assert opt.list_versions("study_plan")[0]["status"] == "rolled_back"

            # --- 版本冲突：父版本不匹配 → 拒绝上机且不动文件
            cid_c = opt._save_candidate("study_plan", v_orig, v_orig - 1, "x", "y", {}, "", "test", {}, "test")
            out = opt.evaluate_candidate(cid_c, run_eval=good_eval, prompts_dir=tdir, persist_report=False)
            assert out["ok"] is False and "版本" in out["error"]
            assert yaml.safe_load((tdir / "study_plan.yaml").read_text(encoding="utf-8"))["version"] == v_orig + 1

            # --- 手动回滚 active 候选 A → 文件还原 v_orig
            out = opt.manual_rollback(cid_a, prompts_dir=tdir)
            assert out["ok"], out
            assert yaml.safe_load((tdir / "study_plan.yaml").read_text(encoding="utf-8"))["version"] == v_orig
        finally:
            prompts.PROMPTS_DIR = orig_prompts_dir
            prompts.load.cache_clear()


# ---------- 5. 基线对比 ----------

def test_compare_baseline():
    baseline = {"agent_tasks": {"success_rate": 0.8},
                "retrieval": {"hybrid": {"hit5": 0.9}},
                "search_tool": {"hit3": 0.9, "gap_false_confident": 0}}
    cand = lambda **kw: {**{"agent_tasks": {"success_rate": 0.8},
                            "retrieval": {"hybrid": {"hit5": 0.9}},
                            "search_tool": {"hit3": 0.9, "gap_false_confident": 0}}, **kw}
    sections = ["agent_tasks", "retrieval", "search_tool"]
    assert opt.compare_baseline(cand(), baseline, sections) == []
    # 容差内噪声不算回归
    assert opt.compare_baseline(cand(agent_tasks={"success_rate": 0.781}), baseline, sections) == []
    # min 指标掉出容差
    r = opt.compare_baseline(cand(retrieval={"hybrid": {"hit5": 0.85}}), baseline, sections)
    assert r == ["retrieval.hit5: 0.85 < 基线 0.9（容差 0.02）"], r
    # max 指标无容差
    r = opt.compare_baseline(cand(search_tool={"hit3": 0.9, "gap_false_confident": 1}), baseline, sections)
    assert any("gap_false_confident" in x for x in r)
    # 基线缺指标 → 跳过不冤枉
    assert opt.compare_baseline(cand(), {"agent_tasks": {}}, sections) == []


if __name__ == "__main__":
    test_plan_mapping()
    print("ok plan 映射")
    test_validate_candidate()
    print("ok 候选校验")
    test_draft_candidate()
    print("ok 起草（假 LLM）")
    test_evaluate_promote_and_rollback()
    print("ok 上机评测：晋级/回滚/版本冲突/手动回滚")
    test_compare_baseline()
    print("ok 基线对比")
    print("\n5/5 全绿")
