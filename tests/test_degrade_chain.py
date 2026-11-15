"""降级链的回归测试（零 LLM 调用）。

钉住两件事——都是 12 轮真实压力测试里暴露的，单看代码看不出来：

1. **P0 约束只认用户说的话**。助手回复里"必须""只能"是讲解措辞，把它们当约束轮，
   会让 `_drop_oldest_soft` 一轮都丢不掉，`drop_oldest_turn` 形同虚设。
2. **降级是单调的**：预算调小，留下的内容只能更少不能更多。旧判据是"摘要能否变短"，
   结果预算 3500 丢摘要、预算 2000 反而留 237 token。

跑法：
    python tests/test_degrade_chain.py
    pytest tests/test_degrade_chain.py -q
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.context import assembler, window  # noqa: E402
from app.context.token_budget import Budget, count_tokens  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="degrade-test-"))


def _fresh_db() -> None:
    # 单文件直跑时没有 conftest 的自动归还，句柄不关就在 Windows 上删不掉库
    db.close_thread_conns()
    p = _TMP / "kb.sqlite"
    if p.exists():
        p.unlink()
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def _history(n: int, reply_len: int = 1400, extra: str = "",
             user: str = "第{i}轮的普通问题，随便聊聊进度") -> list[dict]:
    hist, mid = [], 1
    for i in range(1, n + 1):
        hist.append({"id": mid, "role": "user", "content": user.format(i=i)})
        mid += 1
        hist.append({"id": mid, "role": "assistant",
                     "content": f"第{i}轮回复。" + "这是一段较长的技术说明。" * (reply_len // 12) + extra})
        mid += 1
    return hist


# ------------------------------------------------- 1. P0 只来自用户
def test_assistant_words_are_not_constraints():
    assert window.is_constraint({"role": "user", "content": "记住：不要 Redis"})
    # 讲解措辞不能算用户约束——这是 12 轮里 11 轮被误判的根因
    assert not window.is_constraint(
        {"role": "assistant", "content": "必须先把 checkpoint 打开，只能做到 at-least-once"})
    # 业务 id 仍然认助手的：那是引用，不是约束
    hits = window.key_hits({"role": "assistant", "content": "见 kb-flink-001 那张卡"})
    assert hits and all(pri == 1 for _, pri in hits)


# ------------------------------------------------- 2. drop_oldest_turn 可达
def test_drop_oldest_turn_reachable():
    _fresh_db()
    hist = _history(10, extra="（必须注意：只能这样做）")
    res = assembler.assemble(None, "系统提示", "再说说数据倾斜",
                             hist, budget=Budget(soft_cap=3000), persist=False)
    d = res.stats["degraded"]
    assert "drop_oldest_turn" in d, f"降级链没走到丢轮次：{d}"
    assert "keep_constraint" not in d, f"被误判成全是约束轮：{d}"
    assert res.stats["total"] <= res.stats["budget"], res.stats["total"]


# ------------------------------------------------- 3. 真约束轮仍然不丢
def test_real_constraints_still_protected():
    _fresh_db()
    # 每一轮用户都在提要求：这时候"守预算"必须让位给"别丢用户规则"
    hist = _history(8, user="第{i}轮：记住不要用 Redis，必须只用单机组件")
    res = assembler.assemble(None, "系统提示", "再说说方案",
                             hist, budget=Budget(soft_cap=3000), persist=False)
    assert "keep_constraint" in res.stats["degraded"], res.stats["degraded"]
    assert res.stats["fallback"] == "CTX_KEEP_CONSTRAINT"


# ------------------------------------------------- 4. 保护有上界
def test_protection_has_ceiling():
    _fresh_db()
    # 每轮都带"不要"：真实场景里"不要说教，直接讲"这种句式极常见。
    # 保护若无上界，一轮也丢不掉，降级链最后一级就失效了。
    hist = _history(10, user="第{i}轮：不要用 Redis，直接讲")
    r = assembler.assemble(None, "系统提示", "继续", hist,
                           budget=Budget(soft_cap=3000), persist=False)
    d = r.stats["degraded"]
    assert "drop_oldest_turn" in d, f"保护没有上界，一轮都丢不掉：{d}"
    kept_turns = (len(r.messages) - 2) / 2      # 减掉 system 与本轮 user
    assert kept_turns <= 5, f"只该保住最近 4 条约束轮，实际留了 {kept_turns} 轮"


# ------------------------------------------------- 5. 降级单调
def test_budget_ladder_is_monotone():
    _fresh_db()
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        "INSERT INTO conversation_summary (session_id,covered_from_id,covered_to_id,"
        "n_messages,src_chars,summary,created_at) VALUES (?,?,?,?,?,?,?)",
        ("s1", 1, 8, 8, 9000,
         "事实：机器 2C2G；进度：讲完 Flink 状态管理；约束：不要 Redis/Kafka 集群", "2026-11-15"))
    conn.commit()
    conn.close()

    hist = _history(10)
    totals = []
    for cap in (12000, 9000, 7000, 6000, 5000, 4200, 3500, 2800, 2400, 2000):
        r = assembler.assemble("s1", "系统提示", "再讲讲倾斜",
                               hist, budget=Budget(soft_cap=cap), persist=False)
        totals.append((cap, r.stats["total"], r.stats["degraded"]))
    for (c1, t1, _), (c2, t2, d2) in zip(totals, totals[1:]):
        assert t2 <= t1, f"预算从 {c1} 降到 {c2}，token 反而从 {t1} 涨到 {t2}：{d2}"
    # 最后几档必须真的把摘要处理掉，而不是留着说"已压缩"
    assert any("drop_summary" in d or "shorten_summary" in d for _, _, d in totals)


# ------------------------------------------------- 5. 超预算不装死
def test_over_budget_is_reported_not_hidden():
    _fresh_db()
    huge = "材料。" * 6000
    r = assembler.assemble(None, "系统提示", huge, [], budget=Budget(soft_cap=12000),
                           persist=False)
    assert r.stats["total"] > r.stats["budget"]
    assert r.stats["fallback"] == "CTX_USER_OVERSIZE"
    # 用户输入一个字都不能少
    assert len(r.messages[-1]["content"]) == len(huge)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}", flush=True)
    print(f"\n{len(fns)} passed")
