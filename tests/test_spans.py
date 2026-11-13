"""span 本地持久化测试。

这一层的判据只有一条：**出了事能不能靠这张表复盘**。所以断言不测"写进去了"，
而测"写进去的东西够不够还原现场"：

- 父子关系：没有它，瀑布图就退化成耗时列表，"谁等的谁"永远说不清；
- 并发线程继承：不继承的话三个并行工具会画成三个独立的根，"并发"被读成"三次无关调用"；
- 失败也要留：只记成功等于把"这层挂了"变成"这层没跑过"；
- 属性预算：不裁剪会把库撑爆，裁错了会把查询维度裁掉——两边都得验。
"""
import datetime
import json
import sqlite3
import threading

import pytest

from app.core import db
from app.observability import tracing as tr
from app.services import trace_service


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.close_thread_conns()
    tr.close_span_conn()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    yield
    db.close_thread_conns()
    tr.close_span_conn()


def _rows(trace_id: str) -> list[dict]:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM spans WHERE trace_id=? ORDER BY seq", (trace_id,))]
    finally:
        conn.close()


def test_nested_spans_form_a_tree(fresh_db):
    with tr.agent_span("agent.console") as tid:
        with tr.span("agent.segment.1", tr.KIND_CHAIN):
            with tr.span("llm.chat", tr.KIND_LLM):
                pass
            with tr.span("tool.search_kb", tr.KIND_TOOL):
                pass
    rows = _rows(tid)
    names = [r["name"] for r in rows]
    assert names == ["agent.console", "agent.segment.1", "llm.chat", "tool.search_kb"]
    root, seg, llm, tool = rows
    assert root["parent_id"] is None
    assert seg["parent_id"] == root["span_id"]
    assert llm["parent_id"] == seg["span_id"], "孙 span 必须挂在段下，不能直接挂根"
    assert tool["parent_id"] == seg["span_id"]


def test_span_closes_on_exception_and_records_it(fresh_db):
    """异常路径必须落库：崩掉的那一层恰恰是唯一线索。"""
    with pytest.raises(ValueError):
        with tr.agent_span("agent.console") as tid:
            with tr.span("tool.bad", tr.KIND_TOOL):
                raise ValueError("下游 500")
    rows = _rows(tid)
    bad = next(r for r in rows if r["name"] == "tool.bad")
    assert bad["status"] == "error"
    assert "下游 500" in bad["error"]
    assert bad["end_ts"], "异常退出也要写结束时间，否则时长永远是 0"


def test_root_span_also_records_error(fresh_db):
    with pytest.raises(RuntimeError):
        with tr.agent_span("agent.console") as tid:
            raise RuntimeError("整轮炸了")
    root = _rows(tid)[0]
    assert root["status"] == "error" and "整轮炸了" in root["error"]


def test_thread_pool_inherits_parent(fresh_db):
    """并发线程不继承上下文时，这批 tool span 会变成平行根——图会撒谎。"""
    results = {}

    def work(i):
        tr.adopt(ctx)
        with tr.span(f"tool.t{i}", tr.KIND_TOOL):
            results[i] = tr.current_trace_id()

    with tr.agent_span("agent.console") as tid:
        with tr.span("agent.segment.1", tr.KIND_CHAIN) as seg_span:
            ctx = tr.capture()
            seg_id = seg_span.span_id
        threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    rows = _rows(tid)
    tools = [r for r in rows if r["name"].startswith("tool.")]
    assert len(tools) == 3
    assert {r["parent_id"] for r in tools} == {seg_id}, "三个并发工具必须同一个父"
    assert set(results.values()) == {tid}, "子线程必须复用同一条 trace"


def test_attrs_budget_keeps_dimensions_drops_content(fresh_db):
    """预算裁剪的取舍：属性**条数**太多时，维度键必留、大块内容让位，
    裁了哪些要写在 span 自己身上。

    注意这里刻意用 40 个中等字段（每个 700 字符，都不够长、不进全文附件），
    因为「单个字段太长」已经由 span_blobs 解决了（见文件后半）。
    """
    bulk = {f"bulk{i:02d}": "y" * 700 for i in range(40)}
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.ingest", tr.KIND_TOOL, {
            "tool.name": "ingest_experience",
            "tool.result_chars": 4800,      # 小字段：维度 + 计数，必须留下
            **bulk,
        }):
            pass
    row = next(r for r in _rows(tid) if r["name"] == "tool.ingest")
    assert len(row["attrs_json"]) < tr.SPAN_MAX_ATTR_JSON + 600, "整条属性必须压进预算"
    attrs = json.loads(row["attrs_json"])
    assert attrs["tool.name"] == "ingest_experience", "查询维度不能被裁"
    assert attrs["tool.result_chars"] == 4800, "小字段优先保留"
    assert "_dropped_keys" in attrs, "裁了哪些必须自述，否则会被读成「没打这个点」"
    assert "bulk39" not in attrs or "bulk00" not in attrs, "总得真的裁掉一些"


def test_event_count_is_capped(fresh_db):
    with tr.agent_span("agent.console") as tid:
        with tr.span("llm.chat", tr.KIND_LLM) as s:
            for i in range(60):
                tr.add_event(s, f"e{i}")
    row = next(r for r in _rows(tid) if r["name"] == "llm.chat")
    assert len(json.loads(row["events_json"])) == tr.SPAN_MAX_EVENTS


def test_persist_can_be_disabled(fresh_db, monkeypatch):
    """压测/评测时要能关，否则观测写库会污染耗时基准。"""
    monkeypatch.setenv("SPAN_PERSIST", "0")
    with tr.agent_span("agent.console") as tid:
        with tr.span("llm.chat", tr.KIND_LLM):
            pass
    assert _rows(tid) == []


def test_without_agent_span_still_has_identity(fresh_db):
    """没有根 span 的散调用也要有自己的 trace_id，不能写空串。"""
    with tr.span("retriever.hybrid", tr.KIND_RETRIEVER) as s:
        pass
    assert len(s.trace_id) == 32
    assert len(_rows(s.trace_id)) == 1


def test_trace_tree_restores_hierarchy_and_offsets(fresh_db):
    with tr.agent_span("agent.console") as tid:
        with tr.span("agent.segment.1", tr.KIND_CHAIN):
            with tr.span("llm.chat", tr.KIND_LLM):
                pass
    tree = trace_service.trace_tree(tid)
    assert tree["span_count"] == 3 and tree["orphans"] == 0
    assert tree["root"]["name"] == "agent.console"
    seg = tree["root"]["children"][0]
    assert seg["name"] == "agent.segment.1" and seg["depth"] == 1
    assert seg["children"][0]["depth"] == 2
    assert all(s["offset_ms"] >= 0 for s in tree["waterfall"])


def test_orphan_spans_attach_to_root_with_flag(fresh_db):
    """父 span 已收尾的孤儿不能静默丢弃：缺一块而没提示，比明显异常更危险。"""
    with tr.agent_span("agent.console") as tid:
        with tr.span("agent.segment.1", tr.KIND_CHAIN) as seg:
            seg_id = seg.span_id
            with tr.span("tool.late", tr.KIND_TOOL):
                pass
        # 段退出之后才把它的行删掉，模拟"父 span 那行没落库"（崩在写之前）
        conn = db.get_conn()
        conn.execute("DELETE FROM spans WHERE span_id=?", (seg_id,))
        conn.commit()
    tree = trace_service.trace_tree(tid)
    late = next(s for s in tree["waterfall"] if s["name"] == "tool.late")
    assert tree["orphans"] == 1
    assert any(c["name"] == "tool.late" and c.get("orphan") for c in tree["root"]["children"])


def test_trace_tree_missing_returns_none(fresh_db):
    assert trace_service.trace_tree("0" * 32) is None


def test_recent_traces_and_slow_spans(fresh_db):
    with tr.agent_span("agent.console", {"agent.skill": "diagnose_to_cards"}) as tid:
        with tr.span("llm.chat", tr.KIND_LLM):
            pass
    recent = trace_service.recent_traces(days=7)
    row = next(r for r in recent if r["trace_id"] == tid)
    assert row["name"] == "agent.console" and row["span_count"] == 2
    slow = trace_service.slow_spans(days=7)
    assert any(s["name"] == "agent.console" for s in slow)


def test_prune_removes_old_spans(fresh_db):
    with tr.agent_span("agent.console") as tid:
        pass
    conn = db.get_conn()
    old = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE spans SET start_ts=? WHERE trace_id=?", (old, tid))
    conn.commit()
    assert tr.prune_spans(days=14) >= 1
    assert _rows(tid) == []


def test_span_health_reports_store_state(fresh_db):
    with tr.agent_span("agent.console") as tid:
        with tr.span("llm.chat", tr.KIND_LLM):
            pass
    h = trace_service.span_health(days=7)
    assert h["spans"] == 2 and h["traces"] == 1
    assert h["by_kind"][0]["kind"] in ("LLM", "AGENT")
    assert h["traces_without_root"] == 0
    assert tr.span_stats()["writes"] >= 2


def test_second_root_is_detached_not_orphan(fresh_db):
    """一条 trace 里出现第二个无父 span：那是续跑接上的，不是"父丢了"。

    两者在图上长得像（都挂在根下），但含义完全相反：
    detached = 数据完整、链路被另一个进程续上了；orphan = 缺了一块。
    混成一个计数，排查时会往完全错的方向找。
    """
    with tr.agent_span("agent.orchestrate") as tid:
        with tr.span("agent.step.s1", tr.KIND_CHAIN):
            pass
    # 模拟另一个进程接上同一个 trace（resume）：新根、无 parent
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO spans (span_id,trace_id,parent_id,name,kind,session_id,turn,seq,"
        "start_ts,end_ts,duration_ms,status,attrs_json,events_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("resume0000000001", tid, None, "agent.resume", "AGENT", "s1", 1, 999,
         datetime.datetime.now().isoformat(timespec="milliseconds"),
         datetime.datetime.now().isoformat(timespec="milliseconds"), 1200, "ok",
         "{}", "[]", datetime.datetime.now().isoformat(timespec="milliseconds")))
    conn.commit()
    tree = trace_service.trace_tree(tid)
    assert tree["detached_roots"] == 1
    assert tree["orphans"] == 0
    assert any(c.get("detached_root") for c in tree["root"]["children"])


def test_agent_plans_has_trace_id_column(fresh_db):
    """编排续跑靠这一列把两棵树接起来，缺了就只能"看起来像两次独立任务"。"""
    conn = db.get_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_plans)")}
    assert "trace_id" in cols


# ---------- 全文（span_blobs）：入参必须能一字不差地看回来 ----------

def _blob_count() -> int:
    conn = db.get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM span_blobs").fetchone()[0]
    finally:
        conn.close()


def test_long_input_full_text_is_retrievable(fresh_db):
    """入参全文一字不差。属性里那 800 字只是索引，不是数据。"""
    body = "第一段：我做过推荐系统。" * 500          # 5000 字符，远超展示上限
    with tr.agent_span("agent.console") as tid:
        with tr.span("llm.chat", tr.KIND_LLM, {"input.value": body}):
            pass
    row = next(r for r in _rows(tid) if r["name"] == "llm.chat")
    attrs = json.loads(row["attrs_json"])
    assert len(attrs["input.value"]) < 900, "属性表里必须是预览，不能是全文"
    assert "⟦全文" in attrs["input.value"], "预览必须自带标记，否则前端不知道能点开"
    assert attrs["_blob_keys"] == "input.value"

    got = trace_service.read_blob(row["span_id"], "input.value")
    assert got["text"] == body, "全文必须逐字符一致（压缩往返不能有损）"
    assert got["len"] == len(body) and got["truncated"] is False


def test_short_value_does_not_create_blob(fresh_db):
    """常规大小不该进附件表：blob 表只该装真正的大块内容，否则每次都多一次写。"""
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.search_kb", tr.KIND_TOOL, {"input.value": '{"q":"向量检索"}'}):
            pass
    row = next(r for r in _rows(tid) if r["name"] == "tool.search_kb")
    assert json.loads(row["attrs_json"])["input.value"] == '{"q":"向量检索"}'
    assert _blob_count() == 0


def test_output_added_later_also_gets_full_text(fresh_db):
    """执行过程中补的 output 也要全文——只在构造时处理，等于漏掉最难查的那半边。"""
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.draft_cards", tr.KIND_TOOL) as s:
            s.set_attribute("output.value", "卡片草稿" * 800)
    row = next(r for r in _rows(tid) if r["name"] == "tool.draft_cards")
    attrs = json.loads(row["attrs_json"])
    assert "output.value" in attrs["_blob_keys"]
    assert trace_service.read_blob(row["span_id"], "output.value")["text"] == "卡片草稿" * 800


def test_blob_survives_attr_budget_drop(fresh_db):
    """属性被预算裁掉时，全文仍然要能取到——裁的该是预览，不是证据。"""
    many = {f"bulk{i}": "z" * 700 for i in range(24)}       # 撑爆 8000 预算
    with tr.agent_span("agent.console") as tid:
        with tr.span("llm.chat", tr.KIND_LLM,
                     {"input.value": "PROMPT" * 1000, **many}):
            pass
    row = next(r for r in _rows(tid) if r["name"] == "llm.chat")
    attrs = json.loads(row["attrs_json"])
    assert "_dropped_keys" in attrs, "这种情况本来就该触发裁剪"
    assert trace_service.read_blob(row["span_id"], "input.value")["text"] == "PROMPT" * 1000


def test_oversize_value_is_capped(fresh_db):
    """熔断：单条过大时截到上限并在 len 上体现，不能让一次写文件把库撑爆。"""
    huge = "x" * (tr.SPAN_BLOB_MAX_CHARS + 5000)
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.write_file", tr.KIND_TOOL, {"input.value": huge}):
            pass
    row = next(r for r in _rows(tid) if r["name"] == "tool.write_file")
    got = trace_service.read_blob(row["span_id"], "input.value")
    assert len(got["text"]) == tr.SPAN_BLOB_MAX_CHARS
    assert got["len"] == len(huge), "记录原文长度，才看得出「存的是被截过的」"


def test_blob_missing_returns_none(fresh_db):
    assert trace_service.read_blob("nope", "input.value") is None


def test_prune_keeps_whole_trace_with_error_or_slow(fresh_db):
    """保留单位是**一条 trace**：有个慢 span，整条都留。

    只留那一行慢 span 的话，瀑布图上它周围全是空的，"慢在哪一层、谁等的谁"
    恰恰要靠上下文回答——留个孤证等于没留。
    """
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.slow", tr.KIND_TOOL):
            pass
        with tr.span("tool.fast", tr.KIND_TOOL):
            pass
    conn = db.get_conn()
    old = (datetime.datetime.now() - datetime.timedelta(days=20)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE spans SET start_ts=?", (old,))
    conn.execute("UPDATE spans SET duration_ms=9000 WHERE name='tool.slow'")
    conn.commit()
    tr.prune_spans(days=14)
    names = [r["name"] for r in _rows(tid)]
    assert "tool.slow" in names, "慢 span 必须活着"
    assert "tool.fast" in names, "同一条 trace 的兄弟 span 也要留，否则看不出慢在哪一层"
    assert len(names) == 3


def test_prune_drops_clean_trace_entirely(fresh_db):
    """又老又没毛病的 trace 整体消失——不这样删，表就会无界增长。"""
    with tr.agent_span("agent.console") as slow_tid:
        with tr.span("tool.slow", tr.KIND_TOOL):
            pass
    with tr.agent_span("agent.console") as clean_tid:
        with tr.span("tool.fast", tr.KIND_TOOL):
            pass
    conn = db.get_conn()
    old = (datetime.datetime.now() - datetime.timedelta(days=20)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE spans SET start_ts=?", (old,))
    conn.execute("UPDATE spans SET duration_ms=9000 WHERE trace_id=?", (slow_tid,))
    conn.commit()
    tr.prune_spans(days=14)
    assert _rows(clean_tid) == [], "没有证据的旧 trace 应该整条清掉"
    assert len(_rows(slow_tid)) == 2


def test_prune_hard_cutoff_still_applies(fresh_db):
    """3 倍期是硬上限：证据也不是永久的，到点连证据一起放掉才叫有界。"""
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.slow", tr.KIND_TOOL):
            pass
    conn = db.get_conn()
    old = (datetime.datetime.now() - datetime.timedelta(days=50)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE spans SET start_ts=?, duration_ms=9000", (old,))
    conn.commit()
    tr.prune_spans(days=14)          # 14*3 = 42 天
    assert _rows(tid) == []


def test_blob_stats_reports_compression(fresh_db):
    with tr.agent_span("agent.console") as tid:
        with tr.span("llm.chat", tr.KIND_LLM, {"input.value": "重复的提示词。" * 2000}):
            pass
    st = trace_service.blob_stats(days=1)
    assert st["blobs"] >= 1
    assert st["gz_bytes"] > 0 and st["ratio"] > 1.5, "中文重复文本压缩率应该明显 > 1"
    assert st["by_key"][0]["key"] == "input.value"


# ---------- 去重与事件全文（2026-11-13） ----------

def _blob_texts_count() -> int:
    conn = db.get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM blob_texts").fetchone()[0]
    finally:
        conn.close()


def test_same_text_stored_once(fresh_db):
    """同一段文本被父子 span 各记一次时，正文只存一份。

    真实数据里这种重复很少（实测 43 条引用里 1 条）：每次 llm.chat 记的
    messages 都不一样。所以这条测的是**机制**而不是当前收益——一旦有人加了
    "父 span 也记一遍入参"的埋点，重复会成倍长，机制不在就得回头搬数据。
    """
    body = "同一个系统提示词。" * 300          # 2700 字，必然进 blob
    with tr.agent_span("agent.console") as tid:
        with tr.span("agent.chat", tr.KIND_CHAIN, {"input.value": body}):
            with tr.span("llm.chat", tr.KIND_LLM, {"input.value": body}):
                pass
    assert _blob_count() == 2, "两条引用都要在（每个 span 都能点开）"
    assert _blob_texts_count() == 1, "正文只该存一份"
    rows = [r for r in _rows(tid) if r["name"] != "agent.console"]
    for r in rows:
        got = trace_service.read_blob(r["span_id"], "input.value")
        assert got["text"] == body and got["shared"] is True
    assert tr.span_stats()["blob_dedup"] >= 1


def test_dedup_text_survives_until_last_reference(fresh_db):
    """共享正文不能跟着第一条引用一起删——删早了就是"点开是空的"，还不报错。"""
    body = "共享的长文本。" * 400
    with tr.agent_span("agent.console") as tid_a:
        with tr.span("llm.chat", tr.KIND_LLM, {"input.value": body}):
            pass
    with tr.agent_span("agent.console") as tid_b:
        with tr.span("llm.chat", tr.KIND_LLM, {"input.value": body}):
            pass
    survivor = next(r for r in _rows(tid_b) if r["name"] == "llm.chat")
    conn = db.get_conn()
    conn.execute("DELETE FROM spans WHERE trace_id=?", (tid_a,))
    conn.commit()
    tr.prune_spans(days=14)          # 触发孤儿回收
    assert _blob_texts_count() == 1, "还有引用就不能回收"
    assert trace_service.read_blob(survivor["span_id"], "input.value")["text"] == body

    conn.execute("DELETE FROM spans WHERE trace_id=?", (tid_b,))
    conn.commit()
    tr.prune_spans(days=14)
    assert _blob_texts_count() == 0, "最后一个引用没了才该回收"


def test_event_long_attribute_gets_full_text(fresh_db):
    """事件属性也要全文：排查时最想看的往往正是事件里被截掉的那段（工具入参、原始报错）。"""
    args = json.dumps({"note": "工具入参很长。" * 300}, ensure_ascii=False)
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.draft_cards", tr.KIND_TOOL) as s:
            s.add_event("tool.args", {"args": args, "retry": 2})
    row = next(r for r in _rows(tid) if r["name"] == "tool.draft_cards")
    ev = json.loads(row["events_json"])[0]
    assert "⟦全文" in ev["a"]["args"], "事件属性里应只剩预览 + 标记"
    assert ev["a"]["retry"] == 2, "数值不能被转成字符串，否则聚合/排序就废了"
    got = trace_service.read_blob(row["span_id"], "e0.args")
    assert got["text"] == args and got["len"] == len(args)


def test_event_short_attribute_stays_inline(fresh_db):
    with tr.agent_span("agent.console") as tid:
        with tr.span("tool.search_kb", tr.KIND_TOOL) as s:
            s.add_event("degraded", {"reason": "向量超时"})
    row = next(r for r in _rows(tid) if r["name"] == "tool.search_kb")
    ev = json.loads(row["events_json"])[0]
    assert ev["a"]["reason"] == "向量超时"
    assert _blob_count() == 0


def test_blob_stats_reports_dedup(fresh_db):
    body = "重复的入参。" * 500
    with tr.agent_span("agent.console") as tid:
        with tr.span("agent.chat", tr.KIND_CHAIN, {"input.value": body}):
            with tr.span("llm.chat", tr.KIND_LLM, {"input.value": body}):
                pass
    st = trace_service.blob_stats(days=1)
    assert st["blobs"] == 2 and st["texts"] == 1 and st["saved"] == 1
    assert st["gz_bytes"] > 0 and st["ratio"] > 1.5


def test_migration_adds_blob_hash_to_legacy_db(tmp_path, monkeypatch):
    """老库升级：span_blobs 没有 hash 列时 init_db 不能炸。

    这不是假想问题——把 hash 指数的 CREATE INDEX 写进 schema.sql 后，
    executescript 跑在老库上直接 "no such column: hash"，整个应用起不来。
    所以迁移顺序必须被测试钉住：先补列，再建索引。
    """
    import gzip

    db.close_thread_conns()
    tr.close_span_conn()
    p = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE span_blobs (span_id TEXT NOT NULL, key TEXT NOT NULL, "
        "text_len INTEGER NOT NULL DEFAULT 0, gz_len INTEGER NOT NULL DEFAULT 0, "
        "gz BLOB NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (span_id, key))")
    body = "老格式的正文"
    raw = gzip.compress(body.encode())
    conn.execute("INSERT INTO span_blobs VALUES (?,?,?,?,?,?)",
                 ("legacy-span", "input.value", len(body), len(raw), raw,
                  datetime.datetime.now().isoformat(timespec="milliseconds")))
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", str(p))
    db.init_db()          # 老库上跑一遍，不能抛
    try:
        conn = db.get_conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(span_blobs)")}
        assert "hash" in cols, "必须补上 hash 列"
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(span_blobs)")}
        assert "idx_span_blobs_hash" in idx, "补列之后才建索引"
        # 老行的正文还在自己的 gz 里，读路径必须仍然认得
        got = trace_service.read_blob("legacy-span", "input.value")
        assert got["text"] == body and got["shared"] is False
    finally:
        db.close_thread_conns()
        tr.close_span_conn()
