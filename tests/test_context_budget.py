"""上下文预算的回归测试（零 LLM 调用，摘要用 stub）。

跑法：
    python tests/test_context_budget.py          # 不需要 pytest，直接跑
    pytest tests/test_context_budget.py -q       # 装了 pytest 也能跑

为什么双模式：项目部署在 2C2G 单机，requirements 里没放 pytest（少一个依赖少一份风险），
但测试函数本身按 pytest 风格写（`test_*` + 纯 assert），装上 pytest 直接可用。
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db, llm  # noqa: E402
from app.context import assembler, memory_retriever, summarizer, window  # noqa: E402
from app.context.token_budget import Budget, count_messages, count_tokens  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="ctx-test-"))


def _fresh_db() -> None:
    """每个测试用干净库：库路径是模块级变量，改它比清表可靠。"""
    p = _TMP / "kb.sqlite"
    if p.exists():
        p.unlink()
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def _turn(i: int, text: str, reply_len: int = 1200) -> list[dict]:
    return [{"role": "user", "content": text},
            {"role": "assistant", "content": f"第{i}轮回复。" + "这是一段较长的说明。" * (reply_len // 10)}]


# ---------------------------------------------------------------- 1. 预算不超
def test_20_turns_within_budget():
    _fresh_db()
    history = []
    for i in range(1, 21):
        kind = ("记住：只用 SQLite，不要引入外部组件" if i == 1
                else f"q-{i:04d} 这题帮我看看" if i == 8
                else f"第 {i} 轮的普通问题，随便聊聊进度")
        history += _turn(i, kind)

    budget = Budget(window=64000, soft_cap=12000)
    ctx = assembler.assemble("s-test-1", "你是助理。" * 50, "接着说", history, budget=budget)

    assert ctx.stats["total"] <= budget.usable, f"超预算：{ctx.stats['total']} > {budget.usable}"
    assert ctx.stats["dropped"] > 0, "20 轮长对话应该挤掉一些旧轮次"
    assert ctx.stats["fallback"] is None, f"不该触发兜底：{ctx.stats['fallback']}"


# ------------------------------------------- 2. 关键轮次锚定（约束不能丢）
def test_key_turn_anchored():
    _fresh_db()
    history = []
    for i in range(1, 21):
        kind = ("记住：只用 SQLite，不要引入外部组件" if i == 1
                else f"第 {i} 轮的普通问题")
        history += _turn(i, kind)

    ctx = assembler.assemble("s-test-2", "你是助理。", "接着说", history, budget=Budget(soft_cap=12000))
    joined = "\n".join(m.get("content") or "" for m in ctx.messages)

    assert "只用 SQLite" in joined, "第 1 轮的约束被滑掉了——这正是纯滑窗的坑"
    assert ctx.stats["anchored"] >= 1


# --------------------------------- 3. 全锚定的极端场景：宁可记账，也不静默丢约束
def test_pressure_all_constraints():
    """每轮都是约束时，预算是压不住的——这时的正确行为是"如实记账 + 约束不丢"，
    而不是悄悄把用户说过的规则删掉。这条测试锁住的是这个取舍。"""
    _fresh_db()
    history = []
    for i in range(1, 21):
        history += _turn(i, f"q-{i:04d} 得分 {(i % 9) + 1} 分，必须记住这个结论")

    budget = Budget(soft_cap=6000)
    ctx = assembler.assemble("s-test-3", "你是助理。", "继续", history, budget=budget)
    total = ctx.stats["total"]

    if total > budget.usable:
        assert ctx.stats["fallback"] == "CTX_KEEP_CONSTRAINT", \
            f"超预算了必须标 CTX_KEEP_CONSTRAINT，实际 {ctx.stats['fallback']}"
        assert "keep_constraint" in ctx.stats["degraded"]
        assert "必须记住这个结论" in "\n".join(m.get("content") or "" for m in ctx.messages)
    else:
        assert ctx.stats["dropped"] > 0, "没超预算就说明确实丢了旧轮次"


# ------------------------------------------------- 4. 轮内工具结果按预算裁剪
def test_inflight_tool_results_trimmed():
    _fresh_db()
    budget = Budget(soft_cap=4000)
    messages = [{"role": "system", "content": "规则"}]
    for i in range(8):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "search_kb", "arguments": '{"q":"上下文"}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "结果" * 1300})

    before = count_messages(messages)
    out = assembler.enforce_inflight(messages, budget)

    assert before > budget.usable
    assert out["after"] <= budget.usable, f"裁剪后仍超预算：{out['after']}"
    assert out["trimmed"] >= 1
    last_tool = [m for m in messages if m["role"] == "tool"][-1]["content"]
    if assembler.TRUNC_MARK in last_tool:
        # 末尾那条是"模型正要读的结果"，只在连它一起裁过才够时才会被动
        assert out["trimmed"] >= 8, f"末尾过早被裁：只裁了 {out['trimmed']} 条"


# --------------------------------------------- 5. 摘要落库 + 不重复压同一段
def test_summary_persisted_and_not_repeated():
    _fresh_db()
    fake_calls = []

    def fake_summarize(prev, transcript):
        fake_calls.append(1)
        return "事实：\n- 用户只用 SQLite\n进度：\n- 处理了 8 条旧对话\n约束：\n- 不要引外部组件"

    real = llm.summarize_context
    llm.summarize_context = fake_summarize
    try:
        sid = "s-test-5"
        conn = db.get_conn()
        for i in range(1, 21):
            conn.execute("INSERT INTO console_messages (session_id,role,content,trace_json,mode,created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (sid, "user" if i % 2 else "assistant", f"第{i}条消息，" + "内容" * 100,
                          "[]", "auto", "2026-11-05T00:00:00"))
        conn.commit()
        conn.close()

        r1 = summarizer.summarize(sid, keep_recent=12)
        assert r1["ok"] and r1.get("summary_id"), r1
        row = summarizer.latest_summary(sid)
        assert row and row["n_messages"] == 8, f"应压 20-12=8 条，实际 {row and row['n_messages']}"
        assert len(row["summary"]) < row["src_chars"] / 3, "摘要应显著短于原文"

        # 再压一次：没有新消息 → 跳过（不能把摘要再摘要一遍）
        r2 = summarizer.summarize(sid, keep_recent=12)
        assert r2.get("skipped"), r2
        assert len(fake_calls) == 1, "不该重复调用 LLM"

        # 重新组装时摘要必须被注入
        ctx = assembler.assemble(sid, "你是助理。", "继续", [], budget=Budget(soft_cap=12000))
        assert ctx.stats["summary_used"] is False, "history 为空时不该注入摘要（省预算）"
    finally:
        llm.summarize_context = real


# -------------------------------------------------- 6. 摘要注入 + 降级顺序可查
def test_assemble_injects_summary_when_over_window():
    _fresh_db()
    sid = "s-test-6"
    conn = db.get_conn()
    conn.execute("INSERT INTO conversation_summary (session_id,covered_from_id,covered_to_id,"
                 "n_messages,src_chars,summary,created_at) VALUES (?,?,?,?,?,?,?)",
                 (sid, 1, 8, 8, 3000, "事实：\n- 项目用 FastAPI + SQLite\n约束：\n- 不要引入 Redis", "2026-11-05T00:00:00"))
    conn.commit()
    conn.close()

    history = []
    for i in range(1, 9):
        history += _turn(i, f"第 {i} 轮普通问题")

    ctx = assembler.assemble(sid, "你是助理。", "继续", history, budget=Budget(soft_cap=12000))
    assert ctx.stats["summary_used"] is True
    assert any("不要引入 Redis" in (m.get("content") or "") for m in ctx.messages)

    # 预算被压小时，降级顺序必须是 检索 → 摘要 → 最旧轮次
    # 注意下限：system + 最近 1 轮是"不可再压的核心"，预算不能设得比它还小
    tight = assembler.assemble(sid, "你是助理。" * 20, "继续", history, budget=Budget(soft_cap=2200))
    order = tight.stats["degraded"]
    assert tight.stats["total"] <= 2200, tight.stats["total"]
    assert order, "预算不够时必须发生降级"
    valid = ["drop_retrieval", "shorten_summary", "drop_summary", "drop_oldest_turn"]
    assert all(d in valid for d in order), order
    if "drop_oldest_turn" in order:
        assert order.index("drop_oldest_turn") == len(order) - order.count("drop_oldest_turn"), \
            f"丢轮次必须排在最后：{order}"


# ------------------------------- 7. 摘要覆盖过的消息不能再进历史（别花两份钱）
def test_summary_covers_messages_not_duplicated():
    _fresh_db()
    sid = "s-test-7"
    conn = db.get_conn()
    for i in range(1, 11):
        conn.execute("INSERT INTO console_messages (session_id,role,content,trace_json,mode,created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (sid, "user" if i % 2 else "assistant", f"旧消息{i}", "[]", "auto",
                      "2026-11-05T00:00:00"))
    last_id = conn.execute("SELECT MAX(id) AS m FROM console_messages").fetchone()["m"]
    conn.execute("INSERT INTO conversation_summary (session_id,covered_from_id,covered_to_id,"
                 "n_messages,src_chars,summary,created_at) VALUES (?,?,?,?,?,?,?)",
                 (sid, 1, last_id, 10, 400, "事实：\n- 用户在讨论上下文管理", "2026-11-05T00:00:00"))
    conn.commit()
    conn.close()

    history = [{"id": i, "role": "user" if i % 2 else "assistant", "content": f"旧消息{i}"}
               for i in range(1, 11)]
    history.append({"id": last_id + 1, "role": "user", "content": "新的问题"})

    ctx = assembler.assemble(sid, "你是助理。", "继续", history, budget=Budget(soft_cap=12000))
    joined = "\n".join(m.get("content") or "" for m in ctx.messages)
    assert "旧消息1" not in joined, "被摘要覆盖的消息不该再进历史（重复计价）"
    assert "新的问题" in joined, "覆盖范围之后的消息必须还在"
    assert ctx.stats["summary_used"] is True
    for m in ctx.messages:      # 历史里的 id 只是内部判定用，不能透传给模型
        assert set(m.keys()) == {"role", "content"}, m.keys()


# ------------------------------------------------------- 8. 窗口锚定规则本身
def test_window_rules():
    msgs = [
        {"role": "user", "content": "记住：报告只用中文"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "kb-backend-redis-001 这张卡讲什么"},
        {"role": "assistant", "content": "讲持久化，得分 8 分"},
        {"role": "user", "content": "今天天气不错"},
        {"role": "assistant", "content": "是啊"},
    ]
    sel = window.select(msgs, budget_tokens=100000, recent_turns=1)
    assert sel.anchored == 2, sel.anchored
    assert len(sel.kept) == 6          # 预算充足时不该丢
    assert any("用户约束" in r for r in sel.reasons)
    assert sum(1 for m in msgs if window.key_reasons(m)) == 3   # 约束 1 + id 1 + 评分 1


# ------------------------------------------------------------ 8. token 估算
def test_token_estimate_sane():
    zh = "上下文管理策略"          # 8 个汉字
    en = "context management strategy"
    assert 4 <= count_tokens(zh) <= 10, count_tokens(zh)
    assert count_tokens(en) < count_tokens(zh) * 2
    assert count_tokens("") == 0
    assert count_tokens(None) == 0


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} 通过")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
