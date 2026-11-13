"""记忆向量召回测试（零 LLM、零真实模型）。

为什么打桩而不是真跑 bge：
1. 真模型要下载 90MB、加载 1~2 秒，测试时长和网络环境绑定；
2. 这里要验的是**融合规则**（max 取优、绝对阈值门、异常降级），
   不是 bge 本身准不准——后者只能靠线上效果评估。

跑法：
    pytest tests/test_recall_vector.py -q
    python tests/test_recall_vector.py
"""
import datetime
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import config, db  # noqa: E402
from app.memory import recall, store  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="mem-vec-test-"))
_NOW = datetime.datetime.now().isoformat(timespec="seconds")


def _fresh_db() -> sqlite3.Connection:
    p = _TMP / "kb.sqlite"
    for _ in range(5):
        try:
            if p.exists():
                p.unlink()
            break
        except PermissionError:
            import time
            time.sleep(0.05)
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _row(**kw) -> dict:
    r = {"id": "m-1", "scope": "global", "scope_id": None, "kind": "fact",
         "key": "Kafka acks 机制", "value": "acks=all 才保证不丢",
         "confidence": 0.9, "access_count": 0,
         "created_at": _NOW, "updated_at": _NOW}
    r.update(kw)
    return r


def _reset_vec_state():
    recall._vec_state["off"] = False
    recall._vec_state["reason"] = ""
    config.MEMORY_VECTOR = "auto"


# ---------- 融合规则 ----------

def test_synonym_hits_via_vector_only():
    """词面零重叠时，语义通道必须能救回来——这就是上向量的全部理由。"""
    q = "如何防止数据遗失"
    row = _row(key="broker 端可靠性", value="至少一次投递语义与副本同步")
    assert not (recall._grams(q) & recall._grams(row["key"] + row["value"])), "构造失败：本应零重叠"

    without = recall.score(q, row, None)
    with_vec = recall.score(q, row, 0.78)
    assert without == 0.0, "纯词面应该完全召回不到"
    assert with_vec > recall.MIN_SCORE, f"向量 0.78 应该过门，实际 {with_vec}"


def test_vector_below_floor_is_zero():
    """cosine 没有绝对零点：0.40 在 bge 里和无相关没有区别，必须判 0。

    如果这里放行，所有记忆都会以"语义相关"的名义涌进上下文，
    上一版"无关项凑分"的 bug 会以更隐蔽的形式复发。
    """
    row = _row()
    assert recall.score("如何防止数据遗失", row, 0.40) == 0.0
    assert recall.score("如何防止数据遗失", row, recall.VEC_MIN - 0.01) == 0.0
    # 门线上不是 0，但只值"刚好够格"：远低于明显相关的分数
    edge = recall.score("如何防止数据遗失", row, recall.VEC_MIN)
    strong = recall.score("如何防止数据遗失", row, recall.VEC_GOOD)
    assert 0 < edge < strong / 2, f"门线上应远低于明显相关：{edge} vs {strong}"


def test_vector_ceiling_caps():
    """超过上界不再加分：0.99 和 0.90 应该等价，避免"几乎同句"把分数推到虚高。"""
    row = _row()
    q = "如何防止数据遗失"
    assert recall.score(q, row, 0.90) == recall.score(q, row, 0.99)


def test_exact_key_still_wins():
    """向量只是勉强过门时，词面命中仍要显著压过去。

    注意别把断言写成"接近 0.7"：rel_lex = 0.45*overlap + 0.25*key_hit，
    value 里的字会稀释 overlap，且 key 含空格时 key_hit 只给 0.5——
    满分本身就是不存在的。要比的是"命中 vs 不沾"的相对差距。
    """
    q = "Kafka acks 机制怎么看"
    hit = recall.score(q, _row(confidence=1.0), 0.53)
    # 不沾的记录用 None（没有向量信号）——给它硬塞一个 0.53 是不真实的，
    # 那等于假设"无关文本也能拿到 0.53 的 cosine"，而实测无关上限是 0.33。
    miss = recall.score(q, _row(key="Redis 持久化", value="RDB 与 AOF 的取舍"), None)
    assert hit > 0.3, f"词面命中应有实质分数，实际 {hit}"
    assert miss == 0.0, f"不沾的记录应为 0，实际 {miss}"


def test_max_not_average():
    """融合必须是 max：一路强命中就够，平均会把强信号稀释掉。"""
    row = _row()
    q = "Kafka acks 机制"
    lex = recall.score(q, row, None)
    vec = recall.score("完全无关的另一句话", row, 0.88)
    mixed = recall.score(q, row, 0.88)
    assert mixed >= max(lex, vec) - 0.01, f"融合后 {mixed} 不应低于任一路 {lex}/{vec}"


def test_match_path_labels():
    row = _row()
    assert recall._match_path(row, "Kafka acks 机制", None) == "lex"
    assert recall._match_path(row, "如何防止数据遗失", 0.80) == "vec"
    assert recall._match_path(row, "Kafka acks 机制", 0.80) == "both"
    # 偏好类靠兜底分进来，既非词面也非向量
    assert recall._match_path(row, "无关问句", None) == "kind"


def test_preference_fallback_unaffected():
    """向量开关不能改变偏好类的兜底行为。"""
    pref = _row(kind="preference", key="讲题方式", value="先给结论再展开")
    q = "完全无关的另一个话题"
    assert recall.score(q, pref, None) > 0
    assert recall.score(q, pref, 0.30) > 0


# ---------- 失败与降级 ----------

def test_vector_exception_degrades_and_stops_retrying():
    """模型没下载/离线时：不崩、不重试。否则每轮对话白等一次模型加载。"""
    _reset_vec_state()
    conn = _fresh_db()
    try:
        store.set_memory("fact", "Kafka acks", "acks=all 才保证不丢", scope="global", confidence=0.9)
    finally:
        conn.close()

    from app.core import embedder
    calls = {"n": 0}
    orig = embedder.search_similar

    def boom(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("model not found")

    embedder.search_similar = boom
    try:
        hits = recall.recall("acks 机制", top_k=3)
        assert hits, "词面通道仍应返回结果，不能因为向量挂了就整体召回失败"
        assert recall._vec_state["off"] is True
        # 第二次不应再碰模型
        recall.recall("acks 机制", top_k=3)
        assert calls["n"] == 1, f"降级后不应重试，实际调用 {calls['n']} 次"
    finally:
        embedder.search_similar = orig
        _reset_vec_state()


def test_config_off_skips_vector():
    _reset_vec_state()
    config.MEMORY_VECTOR = "0"
    try:
        st = recall.vector_status()
        assert st["enabled"] is False and st["reason"] == "config_off"
    finally:
        _reset_vec_state()


def test_vector_status_reports_model_when_on():
    _reset_vec_state()
    st = recall.vector_status()
    # 装没装 fastembed 都可能，这里只断言结构：开关与"为什么"必须能回答
    assert "enabled" in st and "reason" in st
    if st["enabled"]:
        assert st["model"] and st["min_cos"] == recall.VEC_MIN


def test_recall_returns_match_and_vec():
    """返回结果要带命中路径：面板上"为什么想起这条"必须能答。"""
    _reset_vec_state()
    config.MEMORY_VECTOR = "0"     # 用词面路径，避开模型依赖
    conn = _fresh_db()
    try:
        store.set_memory("fact", "Kafka acks", "acks=all 才保证不丢", scope="global", confidence=0.9)
        hits = recall.recall("acks 机制是什么", top_k=3)
        assert hits
        assert hits[0]["match"] in ("lex", "vec", "both", "kind")
        assert "vec" in hits[0]
    finally:
        conn.close()
        _reset_vec_state()


def test_session_bonus_still_applies():
    """加了向量通道后，会话内记忆的优先仍然生效。"""
    _reset_vec_state()
    config.MEMORY_VECTOR = "0"
    conn = _fresh_db()
    try:
        store.set_memory("fact", "Kafka acks", "全局版本", scope="global", confidence=0.9)
        store.set_memory("fact", "Kafka acks", "本会话版本", scope="session",
                         scope_id="cs-1", confidence=0.9)
        hits = recall.recall("acks 机制", session_id="cs-1", top_k=5)
        assert len(hits) >= 2
        assert hits[0]["scope"] == "session", f"会话记忆应优先，实际第一名 {hits[0]['scope']}"
    finally:
        conn.close()
        _reset_vec_state()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
