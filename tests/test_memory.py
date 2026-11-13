"""长期记忆层测试（零 LLM 调用）。

覆盖 store 的四条写入路径（created/merged/superseded/rejected）、
版本与软失效、recall 打分与阈值、TTL 过期、以及上下文注入格式。

跑法：
    pytest tests/test_memory.py -q
    python tests/test_memory.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.core import config  # noqa: E402
from app.memory import manager, recall, store  # noqa: E402

# 这组测试只验词面通道的确定性行为；向量通道由 test_recall_vector.py 用打桩覆盖。
# 不关掉的话每个测试都要走一次模型加载，测试时长从秒级变成分钟级，而且依赖模型文件。
config.MEMORY_VECTOR = "0"

_TMP = Path(tempfile.mkdtemp(prefix="mem-test-"))


def _fresh_db() -> sqlite3.Connection:
    p = _TMP / "kb.sqlite"
    if p.exists():
        p.unlink()
    db.DB_PATH = p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def test_created():
    conn = _fresh_db()
    try:
        r = store.set_memory("fact", "目标岗位", "字节后端", scope="global", confidence=0.9)
        assert r["ok"] and r["action"] == "created"
        assert r["version"] == 1
        rows = store.list_memory(scope="global")
        assert len(rows) == 1 and rows[0]["value"] == "字节后端"
        ev = {e["action"] for e in conn.execute("SELECT action FROM memory_events")}
        assert ev == {"created"}
    finally:
        conn.close()


def test_merged_when_similar():
    """措辞微调不该产生 v2——否则同一件事会堆出一串版本，检索时互相抢位。"""
    conn = _fresh_db()
    try:
        a = store.set_memory("fact", "目标岗位", "字节跳动后端开发", scope="global")
        b = store.set_memory("fact", "目标岗位", "字节跳动后端开发岗", scope="global")
        assert b["action"] == "merged", b
        assert b["id"] == a["id"]
        rows = store.list_memory(scope="global")
        assert len(rows) == 1 and rows[0]["version"] == 1
    finally:
        conn.close()


def test_superseded_and_soft_delete():
    """真替代：旧行软失效但保留，新行 version+1。这是"什么时候改的口"的唯一证据。"""
    conn = _fresh_db()
    try:
        a = store.set_memory("fact", "目标岗位", "腾讯后端", scope="global", confidence=0.9)
        b = store.set_memory("fact", "目标岗位", "字节后端", scope="global", confidence=0.9)
        assert b["action"] == "superseded", b
        assert b["version"] == 2 and b["replaced"] == a["id"]

        active = store.list_memory(scope="global")
        assert [r["value"] for r in active] == ["字节后端"]

        all_rows = store.list_memory(scope="global", include_inactive=True)
        assert len(all_rows) == 2
        old = [r for r in all_rows if r["id"] == a["id"]][0]
        assert old["superseded_by"] == b["id"]
        # 事件表留了新旧值，审计链路完整
        ev = conn.execute("SELECT old_value, new_value FROM memory_events WHERE action='superseded'").fetchone()
        assert ev["old_value"] == "腾讯后端" and ev["new_value"] == "字节后端"
    finally:
        conn.close()


def test_low_confidence_rejected():
    """自动抽取的猜测不能顶掉用户亲口说的事实。"""
    conn = _fresh_db()
    try:
        store.set_memory("fact", "目标岗位", "腾讯后端", scope="global", confidence=0.9)
        r = store.set_memory("fact", "目标岗位", "阿里中间件", scope="global", confidence=0.4)
        assert not r["ok"] and r["action"] == "rejected"
        assert store.list_memory(scope="global")[0]["value"] == "腾讯后端"
    finally:
        conn.close()


def test_recall_ranks_and_thresholds():
    conn = _fresh_db()
    try:
        store.set_memory("fact", "目标岗位", "字节跳动后端", scope="global", confidence=0.9)
        store.set_memory("preference", "偏好讲解粒度", "先讲结论再展开细节", scope="global", confidence=0.9)
        store.set_memory("fact", "家乡", "四川成都", scope="global", confidence=0.9)

        hits = recall.recall("我目标岗位投的是哪个公司", session_id=None)
        assert hits and hits[0]["key"] == "目标岗位"

        # 完全无关的 query 不该把「事实」硬凑进来（偏好类允许常驻，见 recall.PREF_BASE）
        hits = recall.recall("Flink 反压怎么排查", session_id=None)
        assert not [h for h in hits if h["kind"] == "fact"]
    finally:
        conn.close()


def test_session_scope_isolation():
    """会话记忆不跨会话泄漏：这次面试的判断不该出现在下次面试里。"""
    conn = _fresh_db()
    try:
        store.set_memory("task_state", "本轮进度", "面经已收，待建卡", scope="session",
                         session_id="cs-A")
        assert recall.recall("进度", session_id="cs-A")
        assert recall.recall("进度", session_id="cs-B") == []
    finally:
        conn.close()


def test_ttl_sweep():
    conn = _fresh_db()
    try:
        store.set_memory("episode", "临时结论", "这轮定位到反压是薄弱点", scope="global",
                         ttl_days=1)
        assert len(store.list_memory(scope="global")) == 1
        # 手动把过期时间推到昨天，模拟时间流逝
        import datetime
        conn.execute("UPDATE agent_memory SET expires_at=?",
                     ((datetime.date.today() - datetime.timedelta(days=1)).isoformat(),))
        conn.commit()
        res = store.sweep()
        assert res["expired"] >= 1
        assert store.list_memory(scope="global") == []
    finally:
        conn.close()


def test_forget_keeps_audit():
    conn = _fresh_db()
    try:
        r = store.set_memory("fact", "目标岗位", "字节后端", scope="global")
        assert store.forget(r["id"])["ok"]
        assert store.list_memory(scope="global") == []
        assert store.get_memory(r["id"])["superseded_by"] == r["id"]
        ev = conn.execute("SELECT action FROM memory_events WHERE memory_id=?", (r["id"],)).fetchall()
        assert "forgotten" in {e["action"] for e in ev}
    finally:
        conn.close()


def test_prompt_block():
    conn = _fresh_db()
    try:
        store.set_memory("preference", "偏好讲解粒度", "先结论后细节，别超过 5 条", scope="global")
        block, n = manager.prompt_block("讲题的时候注意什么", session_id=None)
        assert n == 1
        assert "偏好" in block and "既有记忆" in block
    finally:
        conn.close()


def test_stats():
    conn = _fresh_db()
    try:
        store.set_memory("fact", "目标岗位", "腾讯后端", scope="global", confidence=0.9)
        store.set_memory("fact", "目标岗位", "字节后端", scope="global", confidence=0.9)
        s = store.stats()
        assert s["active"] == 1 and s["total"] == 2
        assert s["events"].get("superseded") == 1
    finally:
        conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
