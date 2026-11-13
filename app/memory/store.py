"""记忆写入与生命周期。

四个必须做对的地方（每一条都对应一个真实会出问题的场景）：

1. **同 key 只允许一条活跃记忆**。用户说"我目标岗位换成字节"，如果只是追加一条，
   下一轮检索会同时召回"腾讯"和"字节"，模型就开始在两份自相矛盾的上下文里编答案。
   所以新值替代旧值时，旧行写 `superseded_by` **软失效**——保留痕迹但不再参与检索。

2. **低置信不覆盖高置信**。自动抽取有噪音（模型可能把"我同学在字节"记成"我在字节"）。
   新值置信度比旧值低 0.15 以上时直接拒绝，记 `rejected` 事件而不是覆盖。
   这比"永远用最新值"保守，但记忆被污染比记忆过时更难修。

3. **相似值合并而不是堆版本**。"字节后端"→"字节 后端"这种改写不该产生 v2，
   相似度 ≥ 0.82 就视为同一条，提升置信度并累积 access_count。

4. **容量与过期都要有**。记忆会无限增长，检索成本和噪音都随之上升。
   每个 scope 上限 500 条，超了按"低置信 + 久未访问"淘汰；episode/task_state
   默认带 TTL（情景会过期，事实不会）。
"""
import datetime
import difflib
import json
import random
import re

from ..core.db import get_conn

KINDS = ("fact", "preference", "episode", "task_state")
MERGE_RATIO = 0.82        # 相似度高于此值视为同一条记忆的改写
CONF_MARGIN = 0.15        # 新值置信度低于旧值这么多时拒绝覆盖
MAX_PER_SCOPE = 500
DEFAULT_TTL_DAYS = {"episode": 90, "task_state": 14}   # 事实/偏好不自动过期
MAX_VALUE_CHARS = 400
MAX_KEY_CHARS = 40

_PUNCT = re.compile(r"[\s\u3000·・,，.。;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_=+*#~`-]+")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"mem-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(1000, 9999)}"


def norm_key(key: str) -> str:
    """主题键归一化：去标点空白、小写、限长。

    归一化是去重的前提——"目标岗位"和"目标 岗位"必须是同一个 key，
    否则同一条事实会分裂成两条活跃记忆。
    """
    k = _PUNCT.sub("", (key or "").strip().lower())
    return k[:MAX_KEY_CHARS] or "未命名"


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def _event(conn, memory_id: str, action: str, reason: str = "",
           old_value: str = "", new_value: str = "", session_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO memory_events (memory_id,action,reason,old_value,new_value,session_id,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (memory_id, action, (reason or "")[:200], old_value[:MAX_VALUE_CHARS],
         new_value[:MAX_VALUE_CHARS], session_id, _now()))


def _row_to_dict(r) -> dict:
    d = dict(r)
    try:
        d["ref"] = json.loads(d.pop("ref_json") or "{}")
    except (ValueError, TypeError):
        d["ref"] = {}
    return d


def set_memory(kind: str, key: str, value: str, *, scope: str = "session",
               scope_id: str | None = None, confidence: float = 0.7,
               source: str = "tool", session_id: str | None = None,
               ref: dict | None = None, ttl_days: int | None = None,
               merge_ratio: float = MERGE_RATIO) -> dict:
    """写入一条记忆。返回 {ok, action, id, ...}，不抛异常。

    action 取值：
      created     新建
      merged      与旧值高度相似，合并（提置信度、累访问数，不新增版本）
      superseded  替代旧值，旧行软失效，新行 version+1
      rejected    新值置信度太低，拒绝覆盖
    """
    kind = kind if kind in KINDS else "fact"
    value = (value or "").strip()[:MAX_VALUE_CHARS]
    if not value:
        return {"ok": False, "action": "rejected", "reason": "空值"}
    scope = scope if scope in ("global", "session") else "session"
    sid_key = "" if scope == "global" else (scope_id or session_id or "")
    k = norm_key(key)
    confidence = max(0.0, min(1.0, float(confidence or 0.7)))
    if ttl_days is None:
        ttl_days = DEFAULT_TTL_DAYS.get(kind)
    expires = (datetime.date.today() + datetime.timedelta(days=int(ttl_days))).isoformat() \
        if ttl_days else None

    conn = get_conn()
    try:
        old = conn.execute(
            "SELECT * FROM agent_memory WHERE scope=? AND scope_id=? AND kind=? AND key=? "
            "AND superseded_by IS NULL ORDER BY version DESC LIMIT 1",
            (scope, sid_key, kind, k)).fetchone()

        if old is None:
            mid = _new_id()
            conn.execute(
                "INSERT INTO agent_memory (id,scope,scope_id,kind,key,value,confidence,source,"
                "session_id,ref_json,version,superseded_by,ttl_days,expires_at,access_count,"
                "last_access_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,0,NULL,?,?)",
                (mid, scope, sid_key, kind, k, value, confidence, source, session_id,
                 json.dumps(ref or {}, ensure_ascii=False), ttl_days, expires, _now(), _now()))
            _event(conn, mid, "created", f"source={source}", "", value, session_id)
            conn.commit()
            return {"ok": True, "action": "created", "id": mid, "kind": kind, "key": k,
                    "value": value, "confidence": confidence, "version": 1}

        sim = _similar(old["value"], value)
        if sim >= merge_ratio:
            # 同一件事的措辞变化：置信度取高者，访问计数累加（热度信号），不升版本
            conn.execute(
                "UPDATE agent_memory SET value=?, confidence=MAX(confidence,?), "
                "access_count=access_count+1, last_access_at=?, updated_at=?, expires_at=COALESCE(?,expires_at) "
                "WHERE id=?",
                (value, confidence, _now(), _now(), expires, old["id"]))
            _event(conn, old["id"], "merged", f"similarity={sim:.2f}",
                   old["value"], value, session_id)
            conn.commit()
            return {"ok": True, "action": "merged", "id": old["id"], "kind": kind, "key": k,
                    "value": value, "similarity": round(sim, 3)}

        if confidence + CONF_MARGIN < float(old["confidence"]):
            _event(conn, old["id"], "rejected",
                   f"new_conf={confidence:.2f} < old_conf={old['confidence']:.2f}",
                   old["value"], value, session_id)
            conn.commit()
            return {"ok": False, "action": "rejected", "id": old["id"],
                    "reason": "新值置信度低于既有记忆，未覆盖", "existing": old["value"]}

        # 真替代：旧行软失效，新行版本 +1。旧值保留在事件表里，可追溯"什么时候改的口"
        mid = _new_id()
        conn.execute(
            "INSERT INTO agent_memory (id,scope,scope_id,kind,key,value,confidence,source,"
            "session_id,ref_json,version,superseded_by,ttl_days,expires_at,access_count,"
            "last_access_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,0,NULL,?,?)",
            (mid, scope, sid_key, kind, k, value, confidence, source, session_id,
             json.dumps(ref or {}, ensure_ascii=False), int(old["version"]) + 1,
             ttl_days, expires, _now(), _now()))
        conn.execute("UPDATE agent_memory SET superseded_by=?, updated_at=? WHERE id=?",
                     (mid, _now(), old["id"]))
        _event(conn, mid, "superseded", f"from={old['id']} v{old['version']}→v{int(old['version']) + 1}",
               old["value"], value, session_id)
        conn.commit()
        return {"ok": True, "action": "superseded", "id": mid, "kind": kind, "key": k,
                "value": value, "version": int(old["version"]) + 1, "replaced": old["id"]}
    finally:
        conn.close()


def list_memory(*, scope: str | None = None, scope_id: str | None = None,
                kinds: list[str] | None = None, include_inactive: bool = False,
                limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM agent_memory WHERE 1=1"
        args: list = []
        if not include_inactive:
            sql += " AND superseded_by IS NULL"
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if scope_id is not None:
            sql += " AND scope_id=?"
            args.append(scope_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [_row_to_dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def get_memory(mid: str) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM agent_memory WHERE id=?", (mid,)).fetchone()
        return _row_to_dict(r) if r else None
    finally:
        conn.close()


def touch(memory_ids: list[str]) -> None:
    """被检索命中就累加访问数。这是容量淘汰时的"用进废退"依据。"""
    if not memory_ids:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE agent_memory SET access_count=access_count+1, last_access_at=? WHERE id=?",
            [(_now(), mid) for mid in memory_ids])
        conn.commit()
    finally:
        conn.close()


def forget(memory_id: str | None = None, *, scope: str | None = None,
           reason: str = "manual") -> dict:
    """遗忘：软失效（superseded_by 指向自己，标记为手动遗忘）。

    不物理删除——记忆是审计对象，"为什么系统不记得这件事了"必须有答案。
    """
    conn = get_conn()
    try:
        if memory_id:
            row = conn.execute("SELECT * FROM agent_memory WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return {"ok": False, "reason": "记忆不存在"}
            conn.execute("UPDATE agent_memory SET superseded_by=?, updated_at=? WHERE id=?",
                         (memory_id, _now(), memory_id))
            _event(conn, memory_id, "forgotten", reason, row["value"], "", row["session_id"])
            conn.commit()
            return {"ok": True, "forgotten": memory_id}
        if scope:
            rows = conn.execute(
                "SELECT id FROM agent_memory WHERE scope=? AND superseded_by IS NULL",
                (scope,)).fetchall()
            conn.executemany("UPDATE agent_memory SET superseded_by=id, updated_at=? WHERE id=?",
                             [(_now(), r["id"]) for r in rows])
            conn.commit()
            return {"ok": True, "forgotten": len(rows), "scope": scope}
        return {"ok": False, "reason": "需要 memory_id 或 scope"}
    finally:
        conn.close()


def sweep(now: datetime.date | None = None) -> dict:
    """过期清理 + 容量淘汰。每次写入后调用一次，成本 O(活跃记忆数)，千级以内可接受。"""
    now = now or datetime.date.today()
    expired = capped = 0
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, expires_at FROM agent_memory WHERE superseded_by IS NULL "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (now.isoformat(),)).fetchall()
        for r in rows:
            conn.execute("UPDATE agent_memory SET superseded_by=?, updated_at=? WHERE id=?",
                         (r["id"], _now(), r["id"]))
            _event(conn, r["id"], "forgotten", f"ttl expired at {r['expires_at']}")
            expired += 1

        for (scope, sid) in conn.execute(
                "SELECT scope, scope_id FROM agent_memory WHERE superseded_by IS NULL "
                "GROUP BY scope, scope_id HAVING COUNT(*) > ?", (MAX_PER_SCOPE,)).fetchall():
            over = conn.execute(
                "SELECT id, value FROM agent_memory WHERE scope=? AND scope_id=? "
                "AND superseded_by IS NULL ORDER BY confidence ASC, "
                "COALESCE(last_access_at, updated_at) ASC LIMIT ?",
                (scope, sid, MAX_PER_SCOPE // 10)).fetchall()
            for r in over:
                conn.execute("UPDATE agent_memory SET superseded_by=?, updated_at=? WHERE id=?",
                             (r["id"], _now(), r["id"]))
                _event(conn, r["id"], "forgotten", "capacity eviction (low conf, least used)")
                capped += 1
        conn.commit()
    finally:
        conn.close()
    return {"expired": expired, "evicted": capped}


def stats() -> dict:
    conn = get_conn()
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_memory WHERE superseded_by IS NULL").fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM agent_memory").fetchone()["n"]
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM agent_memory WHERE superseded_by IS NULL GROUP BY kind")}
        by_scope = {r["scope"]: r["n"] for r in conn.execute(
            "SELECT scope, COUNT(*) AS n FROM agent_memory WHERE superseded_by IS NULL GROUP BY scope")}
        events = {r["action"]: r["n"] for r in conn.execute(
            "SELECT action, COUNT(*) AS n FROM memory_events GROUP BY action")}
        return {"active": active, "total": total, "by_kind": by_kind,
                "by_scope": by_scope, "events": events}
    finally:
        conn.close()
