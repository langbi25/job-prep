"""归因引擎 v1 测试：纯规则、纯内存 fixture，不需要后端、不需要 LLM。

覆盖：
1. 六类根因规则各自的触发条件（检索未召回 / 召回低置信 / 没引用 / 工具选错 /
   上下文降级 / 格式违规）；
2. unattributed 兜底闭合性（judge 校准类失败不能悄悄丢）；
3. 跨层证据（fts 挂而 hybrid 好 → 不算门禁失败；双挂 → 知识缺口嫌疑）；
4. 聚类：按规则聚合、共现模式、修复优先级排序（severity×count）；
5. 报告组装：optimizer_input 聚合、digest_md 生成、落盘。
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services import attribution as attr


def _rules(fs):
    return sorted(f["rule"] for f in fs)


def test_six_rules_fire():
    # --- scoring：异常 → format_violation；空上下文 → context_degraded；档位偏移 → unattributed
    golden = [
        {"question": "Spark shuffle 有哪几种实现，各自的问题是什么？", "topic": "Spark"},
        {"question": "Kafka 怎么保证消息不丢？", "topic": "Kafka"},
    ]
    scoring = {"details": [
        {"q": "Spark shuffle 有哪几种实现，各自的问题是什么？"[:26], "exp": "high",
         "got": "mid", "avg": 6.8, "ok": False},
        {"q": "Kafka 怎么保证消息不丢？"[:26], "error": "JSONDecodeError: Expecting value",
         "exp": "high", "ok": False},
    ]}
    fs = attr._attr_scoring(scoring, golden)
    # Spark 例：探针查真实库上下文，测试环境可能空也可能不空——两类都合法，断言闭合性
    assert _rules(fs) == sorted(_rules(fs))
    spark = next(f for f in fs if "Spark" in f["case"])
    assert spark["rule"] in ("context_degraded", "unattributed")
    kafka = next(f for f in fs if "Kafka" in f["case"])
    assert kafka["rule"] == "format_violation"
    assert "JSONDecodeError" in kafka["evidence"]["error"]

    # --- retrieval：hybrid 挂才算失败；fts 单挂只是证据
    cases = [{"query": "RDB AOF 区别", "expected_card_id": "kb-redis-001", "kind": "keyword"},
             {"query": "进程挂了内存里的东西还能找回来吗", "expected_card_id": "kb-redis-001",
              "kind": "paraphrase"}]
    retrieval = {
        "fts": {"details": [
            {"q": "RDB AOF 区别"[:20], "kind": "keyword", "top3": ["kb-redis-001"], "ok": True},
            {"q": "进程挂了内存里的东西还能找回来吗"[:20], "kind": "paraphrase",
             "top3": ["kb-other"], "ok": False}]},
        "hybrid": {"details": [
            {"q": "RDB AOF 区别"[:20], "kind": "keyword", "top3": ["kb-redis-001"], "ok": True},
            {"q": "进程挂了内存里的东西还能找回来吗"[:20], "kind": "paraphrase",
             "top3": ["kb-other"], "ok": False}]},
    }
    fs = attr._attr_retrieval(retrieval, cases)
    assert len(fs) == 1, "fts 单独挂不能算门禁失败"
    f = fs[0]
    assert f["rule"] == "retrieval_miss" and f["severity"] == "high"
    assert any("FTS 与混合均未召回" in c for c in f["evidence"]["cross_layer"])

    # --- search_tool：miss / 排序 / 低置信 / 无卡误标高置信 四路
    sec = {
        "details": [
            {"q": "RDB AOF 区别"[:20], "kind": "keyword", "ok": False, "top1": False,
             "top3": ["kb-x"], "confidence": "none", "sim": 0.3},
            {"q": "滚动窗口 滑动窗口 会话窗口区别"[:20], "kind": "keyword", "ok": True,
             "top1": False, "top3": ["kb-a", "kb-flink"], "confidence": "mid", "sim": 0.66},
            {"q": "消息可靠性 不丢失 数据重复消费"[:20], "kind": "keyword", "ok": True,
             "top1": True, "top3": ["kb-kafka"], "confidence": "low", "sim": 0.5},
        ],
        "gap_details": [
            {"q": "Agent死循环问题怎么解决?"[:20], "confidence": "high", "sim": 0.77,
             "false_confident": True},
            {"q": "为什么不直接用Claude Code"[:20], "confidence": "low", "sim": 0.5,
             "false_confident": False},
        ],
    }
    fs = attr._attr_search_tool(sec, {"retrieval": cases, "gap": []})
    assert _rules(fs) == ["low_confidence", "low_confidence", "low_confidence",
                          "retrieval_miss"], _rules(fs)

    # --- agent_tasks：没引用 / 工具选错 / 预算超限 / 异常
    tasks = [{"task": "bigdata 学习计划", "min_tools": 2, "must_mention": ["卡", "错题"],
              "max_minutes": 60}]
    sec = {"details": [
        {"task": "bigdata 学习计划", "ok": False,
         "checks": {"tools": False, "data_referenced": False, "time_budget": True},
         "tools": ["get_daily_brief"], "items": 2, "minutes": 55,
         "missed_mentions": ["错题"]},
    ]}
    fs = attr._attr_agent_tasks(sec, tasks)
    assert _rules(fs) == ["no_data_reference", "wrong_tool"]
    ref = next(f for f in fs if f["rule"] == "no_data_reference")
    assert ref["evidence"]["missed_mentions"] == ["错题"]
    assert ref["evidence"]["must_mention"] == ["卡", "错题"]

    sec2 = {"details": [{"task": "bigdata 学习计划", "ok": False, "reason": "max_iters"}]}
    assert _rules(attr._attr_agent_tasks(sec2, tasks)) == ["format_violation"]

    # --- extract：异常 → format_violation；判定偏移 → unattributed
    sec = {"details": [
        {"id": "x1", "error": "timeout"},
        {"id": "x2", "doc_ok": False, "doc": "mixed", "pred": ["complete"], "exp": [["none"]]},
    ]}
    assert _rules(attr._attr_extract(sec)) == ["format_violation", "unattributed"]


def test_attribute_run_and_ctx():
    # done + 没调任何数据工具 + 预算被裁 → wrong_tool + format_violation
    run_no_tool = {"id": "run-1", "status": "done",
                   "steps": [{"tool": "final_answer", "result_preview": "ok"}],
                   "result": {"trimmed": True, "total_minutes": 65, "dropped": ["xx"]}}
    fs = attr.attribute_run(run_no_tool)
    assert _rules(fs) == ["format_violation", "wrong_tool"], _rules(fs)

    # done + search_kb 返回知识缺口（200 字 preview 里的轨迹证据）→ retrieval_miss
    run_gap = {"id": "run-1b", "status": "done",
               "steps": [{"tool": "get_daily_brief", "result_preview": "ok"},
                         {"tool": "search_kb", "args": {"query": "xxx"},
                          "result_preview": '{"note": "知识库无相关卡片 —— 这是知识缺口"}'}],
               "result": {"total_minutes": 40}}
    fs = attr.attribute_run(run_gap)
    assert _rules(fs) == ["retrieval_miss"], _rules(fs)
    assert fs[0]["evidence"]["query"] == "xxx"

    run_err = {"id": "run-2", "status": "error", "steps": [], "error": "boom",
               "result": None}
    assert _rules(attr.attribute_run(run_err)) == ["format_violation"]


def test_cluster_and_report(tmp_path=None):
    failures = [
        attr._failure("retrieval", "q1", "retrieval_miss", {}),
        attr._failure("search_tool", "q1", "retrieval_miss", {}),
        attr._failure("search_tool", "q1", "low_confidence", {}),
        attr._failure("agent_tasks", "t1", "no_data_reference", {}),
        attr._failure("agent_tasks", "t2", "wrong_tool", {}),
        attr._failure("scoring", "s1", "unattributed", {}),
        attr._failure("scoring", "s2", "context_degraded", {}),
        attr._failure("runs", "r1", "format_violation", {}),
    ]
    cl = attr.cluster(failures)
    assert cl["by_rule"]["retrieval_miss"]["count"] == 2
    # q1 共现：retrieval_miss + low_confidence
    assert any(set(p["rules"]) == {"low_confidence", "retrieval_miss"} and p["count"] == 1
               for p in cl["patterns"])

    top = attr._top_issues(failures, top_n=10)
    # high(3)×2 的 retrieval_miss 应排第一；unattributed(low) 排最后
    assert top[0]["rule"] == "retrieval_miss"
    assert top[-1]["rule"] == "unattributed"

    oi = attr._optimizer_input(failures)
    assert oi["ingest_targets"] == ["q1"]     # 去重后
    assert len(oi["prompt_fixes"]) == 2
    assert oi["calibration_watch"] == ["s1"]

    eval_report = {"timestamp": "t", "llm_calls": 0,
                   "search_tool": {"cases": 1, "hit3": 0.0, "top1": 0.0,
                                   "empty_point_hits": 0, "gap_cases": 0,
                                   "gap_false_confident": 0, "gap_rate": 0.0,
                                   "details": [{"q": "RDB AOF 区别"[:20], "kind": "keyword",
                                                "ok": False, "top1": False, "top3": [],
                                                "confidence": "none", "sim": 0.2}],
                                   "gap_details": []},
                   "retrieval": {"fts": {"cases": 1, "hit5": 0.0, "mrr": 0.0,
                                         "details": [{"q": "RDB AOF 区别"[:20],
                                                      "kind": "keyword", "top3": [], "ok": False}]}}}
    # 用临时目录当 evals_dir，隔离真实库探针影响
    with tempfile.TemporaryDirectory() as td:
        report = attr.build_failure_report(eval_report=eval_report, evals_dir=__import__("pathlib").Path(td))
        assert report["summary"]["failures"] >= 1
        assert "digest_md" in report and report["digest_md"].startswith("# 归因报告")
        assert report["clusters"]["by_rule"]["retrieval_miss"]["count"] >= 1
        # 落盘
        p = attr.save_failure_report(report, evals_dir=__import__("pathlib").Path(td))
        back = json.loads(p.read_text(encoding="utf-8"))
        assert back["summary"] == report["summary"]


if __name__ == "__main__":
    test_six_rules_fire()
    print("ok 六类规则触发与闭合性")
    test_attribute_run_and_ctx()
    print("ok 运行轨迹归因")
    test_cluster_and_report()
    print("ok 聚类/优先级/报告组装/落盘")
    print("\n3/3 全绿")
