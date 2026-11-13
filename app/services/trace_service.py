"""调用树查询：把 spans 表还原成前端能画的瀑布图。

为什么不把这段逻辑塞进 API 层：树的重建规则（孤儿怎么挂、偏移怎么算、
同一毫秒怎么排）是有取舍的判断，不是 IO 拼装。放 service 才能被测试直接打。

三个取舍写在这里，免得以后有人"顺手优化"掉：

1. **偏移一律相对根 span**，不用绝对时间。看瀑布图时人是比较"谁先谁后、
   谁占了大头"，绝对时钟除了难看没有额外信息，还会因时区/精度出错。
2. **孤儿挂到根下并标 orphan**。并发线程里 parent 已经 end 的情况是真实存在的
   （线程池继承上下文时父 span 可能刚好收尾）。静默丢掉会让图缺一块，
   而没有提示的缺失比明显的异常更危险。
3. **同一毫秒按 seq 排**。ISO 毫秒精度下并发 span 大量撞车，只按时间排
   会出现"子 span 在父 span 前面"这种看着像 bug 的图。
"""
import datetime
import gzip
import json

from ..core.db import get_conn

MAX_SPANS_PER_TRACE = 500       # 单个 trace 的读取上限，防止异常递归把页面拖死
MAX_BLOB_CHARS = 400_000        # 单次返回上限：再大也不该在浏览器里渲染
DEFAULT_DAYS = 7


def _since(days: int) -> str:
    return (datetime.datetime.now() - datetime.timedelta(days=max(int(days), 1))
            ).isoformat(timespec="milliseconds")


def _ms(ts: str | None) -> int | None:
    """ISO 时间 → epoch 毫秒。解析失败返回 None 而不是抛。"""
    if not ts:
        return None
    try:
        return int(datetime.datetime.fromisoformat(ts).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _loads(s: str | None, default):
    try:
        return json.loads(s) if s else default
    except Exception:  # noqa: BLE001
        return default


def _row_to_span(r) -> dict:
    return {
        "span_id": r["span_id"],
        "parent_id": r["parent_id"],
        "name": r["name"],
        "kind": r["kind"],
        "seq": r["seq"],
        "start_ts": r["start_ts"],
        "duration_ms": int(r["duration_ms"] or 0),
        "status": r["status"],
        "error": r["error"],
        "attrs": _loads(r["attrs_json"], {}),
        "events": _loads(r["events_json"], []),
        "children": [],
    }


def trace_tree(trace_id: str) -> dict | None:
    """按 trace_id 还原一棵调用树。查不到返回 None（不是空树——两者含义不同）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT span_id,parent_id,name,kind,seq,start_ts,duration_ms,status,error,"
            "attrs_json,events_json,session_id,turn FROM spans "
            "WHERE trace_id=? ORDER BY seq LIMIT ?",
            (trace_id, MAX_SPANS_PER_TRACE)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    spans = [_row_to_span(r) for r in rows]
    by_id = {s["span_id"]: s for s in spans}

    # 根 = 没有 parent 或 parent 不在本 trace 里的；正常只有一个
    roots = [s for s in spans if not s["parent_id"] or s["parent_id"] not in by_id]
    root = roots[0] if roots else spans[0]
    t0 = _ms(root["start_ts"]) or 0

    for s in spans:
        s["offset_ms"] = max(0, (_ms(s["start_ts"]) or t0) - t0)

    orphans = 0
    detached = 0
    for s in spans:
        if s is root:
            continue
        if not s["parent_id"]:
            # 独立根：一条 trace 里出现第二个无父 span，正常情况只有一种——
            # 编排续跑（另一个进程接上同一个 trace_id，parent 接不上）。
            # 它和"父 span 丢了"是两回事：前者数据完整，后者缺了一块。
            detached += 1
            s["detached_root"] = True
            root["children"].append(s)
            continue
        parent = by_id.get(s["parent_id"])
        if parent is None:
            orphans += 1
            s["orphan"] = True
            root["children"].append(s)
        else:
            parent["children"].append(s)

    def sort_tree(node: dict) -> None:
        node["children"].sort(key=lambda x: (x["offset_ms"], x["seq"]))
        for c in node["children"]:
            sort_tree(c)

    sort_tree(root)

    deepest = root

    def depth_of(node: dict, d: int = 0) -> None:
        nonlocal deepest
        node["depth"] = d
        if d > deepest.get("depth", 0):
            deepest = node
        for c in node["children"]:
            depth_of(c, d + 1)

    depth_of(root)

    # 子树规模：折叠时要显示"这里藏了 12 个 span / 2 个错误"，
    # 否则折起来就只剩一行，看不出底下是不是有一片报错。
    def count_sub(node: dict) -> tuple[int, int]:
        total = 1
        errs = 1 if node["status"] == "error" else 0
        for c in node["children"]:
            t, e = count_sub(c)
            total += t
            errs += e
        node["span_total"] = total
        node["error_total"] = errs
        return total, errs

    count_sub(root)

    # 总耗时取根 span 的时长。链路上若有比根更长的（根先结束、子还在跑），
    # 那是记账顺序问题，用最大值兜住，免得瀑布图右端被切掉。
    total = max([root["duration_ms"]] + [s["offset_ms"] + s["duration_ms"] for s in spans])

    return {
        "trace_id": trace_id,
        "session_id": rows[0]["session_id"],
        "turn": int(rows[0]["turn"] or 0),
        "root": root,
        "span_count": len(spans),
        "orphans": orphans,
        # 独立根（续跑）：不是缺陷，是"这条链路被另一个进程接上了"的事实
        "detached_roots": detached,
        "total_ms": int(total),
        "error_count": sum(1 for s in spans if s["status"] == "error"),
        "waterfall": [{k: s[k] for k in
                       ("span_id", "parent_id", "name", "kind", "depth", "offset_ms",
                        "duration_ms", "status")} for s in sorted(
                           spans, key=lambda x: (x["offset_ms"], x["seq"]))],
    }


def read_blob(span_id: str, key: str) -> dict | None:
    """取一个属性的全文。查不到返回 None——"没有这条全文"和"全文是空串"要分得开。

    单独一个函数的理由：全文在树接口里**不返回**。一轮 30 个 span、其中 4 个
    LLM 的 prompt 各有几万字，跟着树一起传等于每次点开 trace 都拖几百 KB，
    而人通常只想看其中一两个。按需取是按需取，不是省事。

    正文可能在本行（老数据）、也可能在 blob_texts（去重后的共享副本）。
    两种都要能读——迁移只在"新写入"时改行为，读路径必须同时兼容，
    否则历史 trace 会集体变成"点开是空的"。
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT b.text_len, b.gz, b.hash, b.created_at, "
            "       t.gz AS t_gz, t.text_len AS t_len "
            "FROM span_blobs b LEFT JOIN blob_texts t ON t.hash = b.hash "
            "WHERE b.span_id=? AND b.key=?",
            (span_id, key)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = row["t_gz"] if row["hash"] else row["gz"]
    if payload is None:
        # 引用在、正文没了：说明 blob_texts 被清早了（或被手工删过）。
        # 报"损坏"而不是空字符串——否则会被人当成"当时就是空入参"。
        return {"span_id": span_id, "key": key, "text": "", "len": 0,
                "error": "正文缺失（引用还在，blob_texts 里没有）", "truncated": False}
    try:
        text = gzip.decompress(payload).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  压缩块损坏也要能说清楚，而不是 500
        return {"span_id": span_id, "key": key, "text": "", "len": 0,
                "error": "解压失败（blob 已损坏）", "truncated": False}
    # truncated 有两种来源，都要如实反映：单条熔断（存的时候就截了）、
    # 读接口上限（存全了但一次不返回那么多）。
    born_len = int(row["text_len"] or len(text))
    cut = len(text) > MAX_BLOB_CHARS or born_len > len(text)
    return {
        "span_id": span_id,
        "key": key,
        "text": text[:MAX_BLOB_CHARS],
        # len 用记录的原文字数而不是解压后的长度：两者不一致时，
        # 差异本身就是信息（说明存的时候就被熔断截过了）。
        "len": born_len,
        "truncated": cut,
        "created_at": row["created_at"],
        "shared": bool(row["hash"]),     # 正文来自共享表（同内容多引用）还是本行自带
    }


def blob_stats(days: int = DEFAULT_DAYS) -> dict:
    """全文附件的容量账：条数、压缩后字节、平均压缩比。

    单列出来是因为它最容易无声膨胀——属性表有预算看着不变，全文却没有。

    去重之后"有多少条附件"和"实际占多少磁盘"不再是同一个数：同一个 prompt 被
    父子 span 各记一次，引用是 2、正文是 1。两个都报——只报引用会高估占用
    （然后误以为该改保留策略），只报正文又会看不出"这一轮到底产生了多少份证据"。
    """
    since = _since(days)
    conn = get_conn()
    try:
        ref = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(text_len),0) AS raw "
            "FROM span_blobs WHERE created_at >= ?", (since,)).fetchone()
        # 实际占用 = 去重表的正文 + 迁移前自带正文的老行（hash IS NULL）
        uniq = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(text_len),0) AS raw, "
            "COALESCE(SUM(gz_len),0) AS gz FROM blob_texts WHERE created_at >= ?",
            (since,)).fetchone()
        legacy = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(text_len),0) AS raw, "
            "COALESCE(SUM(gz_len),0) AS gz FROM span_blobs "
            "WHERE created_at >= ? AND hash IS NULL", (since,)).fetchone()
        big = conn.execute(
            "SELECT key, COUNT(*) AS n, MAX(text_len) AS mx FROM span_blobs "
            "WHERE created_at >= ? GROUP BY key ORDER BY COUNT(*) DESC LIMIT 8",
            (since,)).fetchall()
    finally:
        conn.close()
    refs = int(ref["n"] or 0)
    texts = int(uniq["n"] or 0) + int(legacy["n"] or 0)
    gz = int(uniq["gz"] or 0) + int(legacy["gz"] or 0)
    raw = int(uniq["raw"] or 0) + int(legacy["raw"] or 0)
    return {
        "blobs": refs,                      # 引用数（前端"附件 N 条"沿用这个）
        "texts": texts,                     # 去重后的正文份数
        "saved": max(0, refs - texts),      # 被去重掉的份数
        "gz_bytes": gz,                     # 实际磁盘（压缩后）
        "raw_chars": raw,
        "ratio": round(raw / gz, 1) if gz else 0,
        "by_key": [{"key": r["key"], "count": int(r["n"]), "max_len": int(r["mx"] or 0)}
                   for r in big],
    }


def flat_tree(trace_id: str) -> list[dict]:
    """保持层级顺序的扁平列表（DFS），前端用缩进渲染比递归组件简单。"""
    tree = trace_tree(trace_id)
    if not tree:
        return []
    out: list[dict] = []

    def walk(node: dict) -> None:
        out.append({k: v for k, v in node.items() if k != "children"})
        for c in node["children"]:
            walk(c)

    walk(tree["root"])
    return out


def recent_traces(days: int = DEFAULT_DAYS, limit: int = 50,
                  session_id: str | None = None) -> list[dict]:
    """按 trace 汇总。根 span 的 name/时长 + 这一条的总 span 数 + 有没有错。"""
    since = _since(days)
    where = ["start_ts >= ?"]
    args: list = [since]
    if session_id:
        where.append("session_id = ?")
        args.append(session_id)
    sql = (
        "SELECT trace_id, MIN(span_id) AS any_id, "
        "COUNT(*) AS span_count, "
        "MAX(duration_ms) AS max_ms, "
        "SUM(duration_ms) AS sum_ms, "
        "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
        "MIN(start_ts) AS started, MIN(session_id) AS session_id, MIN(turn) AS turn "
        "FROM spans WHERE " + " AND ".join(where) +
        " GROUP BY trace_id ORDER BY started DESC LIMIT ?")
    conn = get_conn()
    try:
        rows = conn.execute(sql, (*args, int(limit))).fetchall()
        if not rows:
            return []
        ids = [r["trace_id"] for r in rows]
        marks = ",".join("?" for _ in ids)
        # 一条 trace 理论上只有一个根。但历史数据里可能有多个（早期版本异步旁路
        # 没继承父 span，自己也成了根）。取 AGENT 优先、否则最早的那个，
        # 否则列表里会出现"一条 trace 叫 memory.extract、塞了 24 个 span"这种怪事。
        roots: dict = {}
        for r in conn.execute(
                "SELECT trace_id, name, kind, status, duration_ms, error, attrs_json "
                f"FROM spans WHERE trace_id IN ({marks}) "
                "AND (parent_id IS NULL OR parent_id='') "
                "ORDER BY trace_id, CASE WHEN kind='AGENT' THEN 0 ELSE 1 END, seq", ids):
            roots.setdefault(r["trace_id"], r)
    finally:
        conn.close()

    out = []
    for r in rows:
        root = roots.get(r["trace_id"])
        out.append({
            "trace_id": r["trace_id"],
            "name": (root["name"] if root else "?"),
            "kind": (root["kind"] if root else "?"),
            "status": (root["status"] if root else ("error" if r["errors"] else "ok")),
            "error": (root["error"] if root else None),
            # 用根 span 的时长而不是 MAX(子 span)：子 span 可能比根活得久
            # （异步旁路、收尾没对齐），取最大值会让列表和详情页显示两个数。
            "duration_ms": int(root["duration_ms"] or 0) if root else int(r["max_ms"] or 0),
            "span_count": int(r["span_count"] or 0),
            "error_spans": int(r["errors"] or 0),
            "started": r["started"],
            "session_id": r["session_id"],
            "turn": int(r["turn"] or 0),
            "skill": _loads(root["attrs_json"], {}).get("agent.skill") if root else None,
        })
    return out


def slow_spans(days: int = DEFAULT_DAYS, limit: int = 20) -> list[dict]:
    """最慢的 span。按 name 聚合后才看得出"同一类调用反复慢"，单条慢是噪声。"""
    since = _since(days)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name, kind, COUNT(*) AS n, AVG(duration_ms) AS avg_ms, "
            "MAX(duration_ms) AS max_ms, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs "
            "FROM spans WHERE start_ts >= ? AND duration_ms > 0 "
            "GROUP BY name, kind ORDER BY avg_ms DESC LIMIT ?",
            (since, int(limit))).fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "kind": r["kind"], "count": int(r["n"]),
             "avg_ms": int(r["avg_ms"] or 0), "max_ms": int(r["max_ms"] or 0),
             "errors": int(r["errs"] or 0)} for r in rows]


def span_health(days: int = DEFAULT_DAYS) -> dict:
    """spans 表自己的健康度：写了多少、失败多少、覆盖多少 trace。"""
    since = _since(days)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS spans, COUNT(DISTINCT trace_id) AS traces, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
            "SUM(CASE WHEN parent_id IS NULL OR parent_id='' THEN 1 ELSE 0 END) AS roots, "
            "AVG(duration_ms) AS avg_ms "
            "FROM spans WHERE start_ts >= ?", (since,)).fetchone()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM spans WHERE start_ts >= ? "
            "GROUP BY kind ORDER BY n DESC", (since,)).fetchall()
    finally:
        conn.close()
    spans = int(row["spans"] or 0)
    traces = int(row["traces"] or 0)
    return {
        "window_days": int(days),
        "spans": spans,
        "traces": traces,
        "spans_per_trace": round(spans / traces, 1) if traces else 0,
        "error_spans": int(row["errors"] or 0),
        "roots": int(row["roots"] or 0),
        # 根数少于 trace 数说明有 trace 的根 span 没落库（崩在写之前），
        # 这种 trace 在瀑布图上会从某个子节点开始，是个需要知道的缺口。
        "traces_without_root": max(0, traces - int(row["roots"] or 0)),
        "avg_ms": int(row["avg_ms"] or 0),
        "by_kind": [{"kind": r["kind"], "count": int(r["n"])} for r in kinds],
    }
