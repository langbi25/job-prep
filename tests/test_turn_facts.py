"""回合事实层测试。

这一层的全部价值在于"能不能拿它做决策"，所以断言分三类：
1. 记录完整——四类信号（输入/过程/结果/质量）不许缺项；
2. 归因可复核——强证据优先，弱信号只在没有别的问题时才当主因；
3. 统计口径——点踩率的分母是"表过态的回合"，不是"全部回合"。
   分母选错会把"没人评价"算成"没人不满"，这个错误最致命：
   它让一个全是沉默的系统看起来满分。
"""
import datetime
import json

import pytest

from app.core import db
from app.services import turn_facts

REPLY = "这是一段足够长的正式回复，用来保证不会被判成空回复。"
LONG = "这是完整结论，包含结论、依据和下一步建议。" * 12


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()


def _rec(**kw):
    """默认是"一次干净的通用对话"：不声明工具、不调工具、回复正常。
    各测试只改它关心的那一个维度，避免默认值把结论带偏。"""
    base = dict(session_id="s1", turn=1, mode="assist", skill=None,
                need_tools=0, msg_chars=20, ctx={}, llm_calls=2,
                trace=[], reply=REPLY, duration_ms=3000,
                usage={"prompt_tokens": 800, "completion_tokens": 200, "cost_cny": 0.012})
    base.update(kw)
    return turn_facts.record(**base)


def _row(turn_id: str) -> dict:
    return turn_facts.detail(turn_id)


def _backdate(turn_id: str, seconds: int) -> None:
    conn = db.get_conn()
    old = (datetime.datetime.now() - datetime.timedelta(seconds=seconds)).isoformat(timespec="seconds")
    conn.execute("UPDATE turn_facts SET ts=? WHERE id=?", (old, turn_id))
    conn.commit()


def test_record_captures_all_four_signal_groups(fresh_db):
    tid = _rec(ctx={"retrieved_n": 3, "memory_n": 2, "total": 1573, "budget": 12000,
                    "dropped": 1, "degraded": ["shorten_summary"]},
               trace=[{"tool": "get_daily_brief", "ok": True},
                      {"tool": "get_due_cards", "ok": False}],
               llm_calls=4, duration_ms=5200)
    r = _row(tid)
    assert (r["retrieval_n"], r["memory_n"], r["ctx_tokens"], r["ctx_budget"],
            r["dropped_msgs"]) == (3, 2, 1573, 12000, 1)
    assert (r["llm_calls"], r["tool_calls"], r["tool_errors"]) == (4, 2, 1)
    assert r["reply_chars"] == len(REPLY)
    assert (r["duration_ms"], r["status"]) == (5200, "ok")
    assert r["tokens"] == 1000 and abs(r["cost_cny"] - 0.012) < 1e-9
    assert json.loads(r["degraded_json"]) == ["shorten_summary"]


def test_failed_status_and_error_are_kept(fresh_db):
    tid = _rec(status="failed", error="LLMError: 400 Bad Request", reply="")
    r = _row(tid)
    assert r["status"] == "failed" and "400" in r["error"]
    assert r["failure_rule"] == "run_failed"


def test_user_reject_is_the_strongest_evidence(fresh_db):
    """点踩 + 一堆实现缺陷同时存在时，主因必须是"用户说没用"。
    否则归因会被技术噪声带偏：改了三处 prompt，用户的真实不满还在。"""
    tid = _rec(trace=[{"tool": "x", "ok": False}], reply="", status="failed")
    turn_facts.set_feedback(tid, -1, "答的是别的题")
    r = _row(tid)
    flags = json.loads(r["flags_json"])
    assert "user_reject" in flags and "run_failed" in flags
    assert r["failure_rule"] == "user_reject"


def test_blind_answer_requires_declared_tools(fresh_db):
    """技能声明了必须查数据，结果一次工具没调、记忆也没召回 → 盲答。
    通用对话（need_tools=0）不查数据是正常的，不能标成盲答。"""
    blind = _rec(session_id="a", need_tools=2, trace=[], ctx={"memory_n": 0})
    normal = _rec(session_id="b", need_tools=0, trace=[])
    assert "blind_answer" in json.loads(_row(blind)["flags_json"])
    assert "blind_answer" not in json.loads(_row(normal)["flags_json"])


def test_memory_hit_excuses_no_tool_call(fresh_db):
    """技能没调工具但召回了记忆——它手里有材料，不算盲答。"""
    tid = _rec(need_tools=2, trace=[], ctx={"memory_n": 3})
    assert "blind_answer" not in json.loads(_row(tid)["flags_json"])


def test_loop_exhausted_needs_short_reply(fresh_db):
    """打到轮次上限但答得很好，不该算失败——上限内做完就是做完了。"""
    bad = _rec(session_id="a", llm_calls=8, reply="没查到", max_llm_calls=8)
    good = _rec(session_id="b", llm_calls=8, reply=LONG, max_llm_calls=8)
    assert "loop_exhausted" in json.loads(_row(bad)["flags_json"])
    assert "loop_exhausted" not in json.loads(_row(good)["flags_json"])


def test_followup_backfills_and_rescores_previous_turn(fresh_db):
    """追问间隔是"后来才有的数据"，所以必须回填并触发上一轮重判。
    不回填的话，"快速追问"这个弱信号永远标不上，等于没有这个维度。"""
    t1 = _rec(turn=1)
    assert _row(t1)["followup_secs"] is None
    _backdate(t1, 40)
    _rec(turn=2)

    r1 = _row(t1)
    assert 30 <= r1["followup_secs"] <= 50
    assert "fast_followup" in json.loads(r1["flags_json"])
    assert r1["failure_rule"] == "fast_followup", "只有弱信号时它才当主因"


def test_slow_followup_does_not_flag(fresh_db):
    t1 = _rec(turn=1)
    _backdate(t1, 1800)
    _rec(turn=2)
    assert _row(t1)["failure_rule"] is None


def test_one_row_per_turn_even_if_rerun(fresh_db):
    """一轮被重新生成时必须覆盖，不能追加——否则统计里同一轮算两次，
    失败率会被自己放大。"""
    _rec(turn=1, reply="第一版答案，长度够但结论是错的，需要重来一次。")
    _rec(turn=1, reply="第二版答案，修正了上一版的错误结论并补上了依据。")
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM turn_facts WHERE session_id='s1' AND turn=1").fetchone()[0]
    assert n == 1
    rows = turn_facts.recent(1)
    first = [r for r in rows if r["turn"] == 1][0]
    assert first["reply_chars"] == len("第二版答案，修正了上一版的错误结论并补上了依据。"), \
        "留下的是最后一次的结果，不是第一版"


def test_feedback_recomputes_rule(fresh_db):
    tid = _rec(reply=LONG)
    assert _row(tid)["failure_rule"] is None
    turn_facts.set_feedback(tid, -1, "引用的是旧数据")
    r = _row(tid)
    assert r["feedback"] == -1 and r["feedback_note"] == "引用的是旧数据"
    assert r["failure_rule"] == "user_reject"


def test_unknown_turn_feedback_returns_none(fresh_db):
    assert turn_facts.set_feedback("tf-not-exist", -1) is None
    assert turn_facts.by_turn("s1", 99) is None


def test_feedback_without_note_keeps_previous_note(fresh_db):
    """面板上点按钮只传分数。不能因为"这次没写理由"就把上次写的理由抹掉——
    理由比分数难补。"""
    tid = _rec(reply=LONG)
    turn_facts.set_feedback(tid, -1, "引用的是旧数据")
    turn_facts.set_feedback(tid, 1)
    r = _row(tid)
    assert r["feedback"] == 1
    assert r["feedback_note"] == "引用的是旧数据"


def test_summary_rates_use_honest_denominators(fresh_db):
    """点踩率的分母必须是"表过态的回合"。用全部回合当分母，
    会让"没人评价"看起来像"没人不满"。"""
    a = _rec(session_id="a", reply=LONG)
    _rec(session_id="b", reply=LONG)
    _rec(session_id="c", reply=LONG, status="failed", error="boom")
    turn_facts.set_feedback(a, -1, "不对")

    s = turn_facts.summary(1)
    assert s["turns"] == 3
    assert s["rated"] == 1 and s["disliked"] == 1
    assert s["reject_rate"] == 1.0, "只有 1 人表态且是差评 → 100%，不是 33%"
    assert s["ok_turns"] == 2 and s["ok_rate"] == round(2 / 3, 4)
    assert s["flag_rate"] == round(2 / 3, 4)
    assert s["p50_ms"] == 3000 and s["p95_ms"] == 3000


def test_summary_empty_window_returns_none_rates(fresh_db):
    s = turn_facts.summary(7)
    assert s["turns"] == 0
    assert s["ok_rate"] is None and s["reject_rate"] is None, \
        "没有样本时给 0% 会让人以为系统全坏"


def test_clusters_group_by_rule_with_samples(fresh_db):
    for i in range(3):
        _rec(session_id=f"s{i}", reply="", status="failed", error="boom")
    c = turn_facts.clusters(1, per_rule=2)
    top = c["clusters"][0]
    assert top["rule"] == "run_failed" and top["n"] == 3
    assert len(top["samples"]) == 2, "每簇只取样本，不把整簇数据搬给模型"
    assert top["label"] == "整轮失败"


def test_record_never_breaks_the_conversation(fresh_db, monkeypatch):
    """事实层是统计，不是业务前置条件。库挂了对话也要能继续。"""
    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(turn_facts, "get_conn", boom)
    assert turn_facts.record(session_id="s", turn=1, reply="x") is None


def test_params_are_exposed_for_explainability(fresh_db):
    p = turn_facts.params()
    assert p["empty_reply_chars"] == 0 and p["fast_followup_secs"] == 90
    assert p["max_llm_calls"] == 6


def test_short_answer_is_not_a_defect(fresh_db):
    """「是，3 张」很短但是对的。把"短"判成"差"会让归因天天报警，
    而报警多了就没人看了——比没有报警更糟。"""
    tid = _rec(reply="是，3 张。")
    assert _row(tid)["failure_rule"] is None


def _msg(session_id: str, turn: int, text: str) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO console_messages (session_id,role,content,mode,created_at) "
        "VALUES (?,?,?,?,?)",
        (session_id, "user", text, "assist",
         datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def test_improvement_points_at_a_concrete_target(fresh_db):
    """归因的价值在于"改哪里"，所以每条规则必须带 target 与具体建议。
    只说"失败 3 次"的报告，看了也不知道动什么。"""
    _rec(session_id="a", skill="gap_to_cards", need_tools=2, trace=[], reply=LONG)
    _rec(session_id="b", skill="gap_to_cards", need_tools=2, trace=[], reply=LONG)
    _rec(session_id="c", skill="weakness_diagnosis", reply="", status="failed", error="boom")

    out = turn_facts.improvement(1)
    g = [s for s in out["skills"] if s["skill"] == "gap_to_cards"][0]
    assert g["flagged"] == 2
    rule = g["rules"][0]
    assert rule["rule"] == "blind_answer" and rule["target"] == "改技能正文"
    assert "必须先调用" in rule["advice"]
    assert len(g["samples"]) == 2


def test_improvement_skips_clean_skills(fresh_db):
    _rec(session_id="a", skill="gap_to_cards", reply=LONG)
    assert turn_facts.improvement(1)["skills"] == []


def test_regression_candidates_carry_the_original_question(fresh_db):
    """回归用例草稿必须带用户原话——没有原话的"失败样本"没法复现。"""
    _msg("a", 1, "帮我看看 redis 那块还差什么")
    _rec(session_id="a", turn=1, skill="gap_to_cards", need_tools=2, trace=[], reply=LONG)

    rows = turn_facts.regression_candidates(1)
    assert len(rows) == 1
    assert rows[0]["query"] == "帮我看看 redis 那块还差什么"
    assert rows[0]["draft_case"]["task"] == "帮我看看 redis 那块还差什么"
    assert rows[0]["draft_case"]["must_mention"] == [], "该提到什么必须人来定"


def test_regression_candidates_put_dislikes_first(fresh_db):
    _msg("a", 1, "问题甲")
    _msg("b", 1, "问题乙")
    _rec(session_id="a", turn=1, need_tools=2, trace=[], reply=LONG)     # blind_answer
    bad = _rec(session_id="b", turn=1, need_tools=2, trace=[], reply=LONG)
    turn_facts.set_feedback(bad, -1, "答非所问")

    rows = turn_facts.regression_candidates(1)
    assert rows[0]["failure_rule"] == "user_reject", "点踩的排最前，它是最可信的坏样本"


def test_improvement_labels_general_chat_separately(fresh_db):
    _rec(session_id="a", skill=None, trace=[{"tool": "x", "ok": False}], reply=LONG)
    out = turn_facts.improvement(1)
    assert out["skills"][0]["skill"] == "(通用)"


# ============ 可观测的细度：串表、失败留痕、覆盖率 ============
# 这一组的共同前提：**台账是唯一事实源**。轮级不自己攒数，
# 否则"轮级记 5 次、台账 6 行"这种事会一直存在（实测过一次）。

def _usage(session_id="s1", turn=1, scene="console:assist", status="ok",
           pt=1000, ct=200, cost=0.01, trace_id="t-1"):
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO llm_usage (ts,model,scene,session_id,turn,trace_id,prompt_tokens,"
            "completion_tokens,cached_tokens,cost_cny,price_estimated,latency_ms,"
            "status,error,finish_reason,retried) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"), "deepseek-chat",
             scene, session_id, turn, trace_id, pt, ct, 0, cost, 0, 900,
             status, None, "stop", 0))
        conn.commit()
    finally:
        conn.close()


def test_turn_counts_come_from_ledger_not_from_caller(fresh_db):
    """轮级调用数必须与台账一致——调用方攒的数会和旁路调用打架。"""
    for _ in range(3):
        _usage()
    _usage(scene="memory_extract")      # 同一轮但走旁路，不能算进主循环

    tid = _rec(llm_calls=99)            # 故意传一个错的值
    r = _row(tid)
    assert r["llm_calls"] == 3, "只数 console:* ——旁路调用不属于主循环配额"
    assert r["tokens"] == 3 * 1200
    assert abs(r["cost_cny"] - 0.03) < 1e-9


def test_failed_llm_call_is_recorded_and_flagged(fresh_db):
    """失败调用也占一行台账，且整轮即使成功也要标出来。"""
    _usage()
    _usage(status="error", pt=0, ct=0, cost=0.0)

    r = _row(_rec(status="ok", reply=LONG))
    assert r["llm_calls"] == 1, "成功数"
    assert r["llm_errors"] == 1, "失败数单独记"
    assert "llm_unstable" in json.loads(r["flags_json"])
    # 整轮成功、也没有别的问题时，主因就是它——处置是改稳定性，不是改 prompt
    assert r["failure_rule"] == "llm_unstable"


def test_llm_unstable_yields_to_content_problems(fresh_db):
    """链路抖过但用户已点踩：主因必须是点踩，不能拿稳定性当挡箭牌。"""
    _usage(status="error", pt=0, ct=0, cost=0.0)
    tid = _rec(reply=REPLY)
    turn_facts.set_feedback(tid, -1, "答错了")
    assert _row(tid)["failure_rule"] == "user_reject"


def test_trace_id_is_persisted_for_joining(fresh_db):
    """没有 trace_id，三张表只能靠 (session,turn) 对齐，跨线程的调用直接变孤儿。"""
    tid = _rec(trace_id="abc123")
    assert _row(tid)["trace_id"] == "abc123"


def test_bump_aux_adds_side_channel_cost(fresh_db):
    """异步旁路花的钱要能回填到那一轮，否则真实成本永远少算一块。"""
    tid = _rec()
    ok = turn_facts.bump_aux("s1", 1, calls=1, tokens=900, cost=0.002)
    assert ok is True
    r = _row(tid)
    assert r["llm_aux"] == 1
    assert r["tokens"] == 1000 + 900          # 调用方传的 800+200，再加旁路
    assert abs(r["cost_cny"] - 0.014) < 1e-9


def test_bump_aux_on_unknown_turn_is_harmless(fresh_db):
    assert turn_facts.bump_aux("nope", 9, calls=1, tokens=10, cost=0.001) is False


def test_hit_ids_are_persisted(fresh_db):
    """检索明细：只记"命中 3 条"回答不了"哪张卡被检索到却从没被引用"。"""
    hits = [{"k": "card", "i": "card-1", "s": 0.0312, "l": "card"},
            {"k": "summary", "i": "sum-7", "s": 0.0158, "l": "summary"}]
    r = _row(_rec(ctx={"retrieved_n": 2, "hit_ids": hits}))
    assert json.loads(r["hit_ids"]) == hits


def test_coverage_reports_the_missing_turns(fresh_db):
    """覆盖率是"结论的可信边界"：漏掉的轮次可能恰恰是失败最惨的。"""
    _msg("s1", 1, "第一问")
    _msg("s1", 2, "第二问")
    _msg("s1", 3, "第三问")
    _rec(session_id="s1", turn=1)           # 只落了 1 轮

    cov = turn_facts.coverage(30)
    assert cov["turns_seen"] == 3
    assert cov["turns_recorded"] == 1
    assert cov["coverage"] == pytest.approx(1 / 3, abs=1e-4)
    assert cov["missing"] and cov["missing"][0]["lost"] == 2


def test_coverage_is_full_when_every_turn_is_recorded(fresh_db):
    for t in (1, 2):
        _msg("s1", t, f"第{t}问")
        _rec(session_id="s1", turn=t)
    cov = turn_facts.coverage(30)
    assert cov["coverage"] == 1.0
    assert cov["missing"] == []


def test_coverage_counts_field_completeness(fresh_db):
    """字段完整率：费用/trace 缺失要说出来，而不是显示成 0 元、空 trace 混过去。"""
    _rec(session_id="s1", turn=1, trace_id="")                    # 无 trace
    _rec(session_id="s1", turn=2, trace_id="t-2")                 # 有 trace
    cov = turn_facts.coverage(30)
    assert cov["no_trace"] == 1
    assert cov["trace_complete"] == 0.5
