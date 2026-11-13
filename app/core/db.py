import datetime
import json
import sqlite3
import threading
from pathlib import Path

from .config import DB_PATH, DATA_DIR, ensure_dirs

_SCHEMA = Path(__file__).resolve().parent.parent.parent / "schema.sql"

# FTS 表 → 其触发器。表定义过期（列名对不上 / 不是外部内容表）时连触发器一起重建，
# 否则旧触发器会继续往"不存在的列"或已被删除的表里写。
_FTS_TRIGGERS = {
    "kb_cards_fts": ("cards_ai", "cards_au", "cards_ad"),
    "question_fts": ("questions_ai", "questions_au", "questions_ad"),
}

# 线程局部连接池：{DB_PATH: conn}。键必须含 DB_PATH——测试会把 DB_PATH 换到临时目录，
# 若只按线程缓存，换库后拿到的还是旧库的连接，测试互相写脏数据且极难排查。
_local = threading.local()


class _PooledConn(sqlite3.Connection):
    """close() 改为「归还」而不是真正关闭。

    全库约 140 处 ``conn.close()`` 都是"用完了"的语义（try/finally 防泄漏），
    逐处删除要动 30+ 个文件且每处都要 review。让 close 变成归还，
    配合 get_conn 借出时的 in_transaction 回滚，语义等价、调用点零改动。
    真正关闭只发生在 close_thread_conns()（测试隔离 / 进程退出）。
    """

    def close(self) -> None:  # noqa: D102 覆写为归还，见类注释
        pass

    def _really_close(self) -> None:
        super().close()


def get_conn() -> sqlite3.Connection:
    """借出当前线程的连接（没有才新建），用完不必 close。

    实测本机（Windows + WAL）connect+pragma+close 一轮 8.4ms，面板一次请求
    开 7 个连接 = 50ms 纯开销，比解析技能还贵。SQLite 同线程复用连接没有
    一致性代价，唯一要防的是两件事：
    1. 上一个使用者忘 commit —— 借出前发现 in_transaction 就回滚，
       否则下一个使用者会读到它的中间状态（复用连接最阴险的坑）；
    2. 库文件被删重建（测试 / 换环境）—— 旧连接对已删除文件的句柄还活着，
       借出前 SELECT 1 探活，失效就丢弃重建。
    """
    key = str(DB_PATH)
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    conn = pool.get(key)
    if conn is not None:
        try:
            if conn.in_transaction:
                conn.rollback()
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            try:
                conn._really_close()
            except Exception:  # noqa: BLE001  连接已经坏了，关不掉也只能放弃
                pass
            del pool[key]
    conn = sqlite3.connect(DB_PATH, factory=_PooledConn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    pool[key] = conn
    return conn


def close_thread_conns() -> None:
    """关掉当前线程缓存的所有连接。

    只有两个调用场景：测试隔离（DB_PATH 换临时目录前先关，否则 Windows 上
    临时目录因句柄占用删不掉）和进程退出前。常规请求路径永远不调它。
    """
    pool = getattr(_local, "pool", None)
    if not pool:
        return
    for conn in pool.values():
        try:
            conn._really_close()
        except Exception:  # noqa: BLE001
            pass
    _local.pool = {}
    # span 用独立连接写库（避开业务事务，见 tracing._span_conn），不在上面的池里。
    # 不一起关的话，Windows 上临时目录会因为一个残留句柄删不掉，
    # 表现成下一个用例建库时 "attempt to write a readonly database"——
    # 偶发、且报错点离根因很远（在建表，实际是上个用例的句柄没放）。
    # 用 sys.modules 判断而不是 import：tracing 拖着 otel 重依赖，
    # 没启用观测的进程不该在这里被拖进来。
    import sys as _sys

    _tr = _sys.modules.get("app.observability.tracing")
    if _tr is not None:
        try:
            _tr.close_span_conn()
        except Exception:  # noqa: BLE001
            pass


def _stale_fts(conn: sqlite3.Connection) -> list[str]:
    """找出定义过期的 FTS 表。

    两种历史坑：
    1) kb_cards_fts 声明了内容表里不存在的 body 列 → 读列值必报错；
    2) question_fts 曾是独立表（无 content=），靠代码手工同步 → 索引漂移。
    """
    stale = []
    for table in _FTS_TRIGGERS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row["sql"]:
            continue
        if "content=" not in row["sql"] or "body" in row["sql"]:
            stale.append(table)
    return stale


def _drop_stale_fts(conn: sqlite3.Connection) -> None:
    for table in _stale_fts(conn):
        for trg in _FTS_TRIGGERS[table]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trg}")
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """轻量列迁移：给已存在的库补新增列（schema.sql 只对新建库生效）。

    SQLite 的 ALTER TABLE ADD COLUMN 很便宜，逐个判断比引入迁移框架更务实。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(kb_cards)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE kb_cards ADD COLUMN merged_into TEXT REFERENCES kb_cards(id)")

    # 题目池的"答案三态"：由面经抽取阶段判定，决定建卡走原文规范化还是 LLM 起草。
    # 存下来而不是只留在 extracted_json 里，是因为卡建完、面经归档后还要能回溯
    # "这张卡的参考答案是原文来的还是模型编的"。
    qcols = {r["name"] for r in conn.execute("PRAGMA table_info(question_pool)")}
    if "answer_completeness" not in qcols:
        conn.execute("ALTER TABLE question_pool ADD COLUMN answer_completeness TEXT")
    if "answer_gaps_json" not in qcols:
        conn.execute("ALTER TABLE question_pool ADD COLUMN answer_gaps_json TEXT DEFAULT '[]'")

    # 对话消息带上"这一轮的上下文账本"，刷新页面后仍能看见当时花了多少 token
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(console_messages)")}
    if "ctx_json" not in ccols:
        conn.execute("ALTER TABLE console_messages ADD COLUMN ctx_json TEXT DEFAULT '{}'")

    # 上下文账本记下"这轮注入了几条长期记忆"：没有这一列，就分不清
    # 「模型不知道用户偏好」是记忆没召回、还是召回了但被预算挤掉。
    xcols = {r["name"] for r in conn.execute("PRAGMA table_info(ctx_traces)")}
    if "memory_n" not in xcols:
        conn.execute("ALTER TABLE ctx_traces ADD COLUMN memory_n INTEGER NOT NULL DEFAULT 0")

    # 编排记录带上费用列。老库补列默认 0 是诚实的：那几次编排的逐调用账
    # 没记过（llm_usage 是后建的），拿 0 比"拿 token 数乘单价编一个"干净。
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_plans)")}
    if "cost_cny" not in pcols:
        conn.execute("ALTER TABLE agent_plans ADD COLUMN cost_cny REAL NOT NULL DEFAULT 0")
    # 续跑需要的两列：mode 决定写权限（默认 assist 才是安全的，老计划一律按只读续跑），
    # resume_count 用来识别"这条计划反复续不上"——那是 plan 本身有问题，不是运气差。
    if "mode" not in pcols:
        conn.execute("ALTER TABLE agent_plans ADD COLUMN mode TEXT NOT NULL DEFAULT 'assist'")
    if "resume_count" not in pcols:
        conn.execute("ALTER TABLE agent_plans ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0")

    # 技能运行要能回答"这个版本的改动是变好还是变坏了"：只记技能名等于把所有版本
    # 混成一个平均数，改坏了也看不出来。skill_version 允许 NULL——技能可能还没进
    # 版本管理，编一个 0 会让人以为"存在第 0 版"。
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(skill_runs)")}
    if "skill_version" not in rcols:
        conn.execute("ALTER TABLE skill_runs ADD COLUMN skill_version INTEGER")
    if "cost_cny" not in rcols:
        conn.execute("ALTER TABLE skill_runs ADD COLUMN cost_cny REAL NOT NULL DEFAULT 0")

    # 事实层的"可 join"改造（2026-11-12）。三张表原来只能靠 (session_id, turn) 对齐，
    # 一旦跨线程/子 agent，调用就成了孤儿。trace_id 写进轮级，就能把
    # 「这一轮 → 每一次 LLM 调用 → 每一次工具调用」串成一条链。
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(turn_facts)")}
    if "trace_id" not in tcols:
        conn.execute("ALTER TABLE turn_facts ADD COLUMN trace_id TEXT")
    # 主循环与旁路分开记：原来一个 llm_calls 混着算，实测出现过
    # 「轮级记 5 次、台账同轮 6 行」——差的那次是异步记忆抽取，
    # 它花的是同一轮的钱但不占主循环配额，混在一起就说不清"这轮几次是因为绕圈"。
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_plans)")}
    if "trace_id" not in acols:
        conn.execute("ALTER TABLE agent_plans ADD COLUMN trace_id TEXT")

    if "llm_aux" not in tcols:
        conn.execute("ALTER TABLE turn_facts ADD COLUMN llm_aux INTEGER NOT NULL DEFAULT 0")
    # 失败/降级的模型调用数：>0 而整轮仍 ok，就是"链路不稳"而不是"内容不行"，
    # 这两种处置相反（一个改重试/超时，一个改 prompt），混着看必然改错地方。
    if "llm_errors" not in tcols:
        conn.execute("ALTER TABLE turn_facts ADD COLUMN llm_errors INTEGER NOT NULL DEFAULT 0")
    # 检索命中了什么。只有数量（retrieval_n=3）回答不了「哪张卡被检索到却从没被引用」，
    # 而那正是知识库该删该改的依据。
    if "hit_ids" not in tcols:
        conn.execute("ALTER TABLE turn_facts ADD COLUMN hit_ids TEXT NOT NULL DEFAULT '[]'")

    # LLM 台账补失败维度：原来只在成功时写，"重试过"和"挂过"在台账里看不见。
    lcols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_usage)")}
    for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'ok'"),
                     ("error", "TEXT"),
                     ("finish_reason", "TEXT"),
                     ("retried", "INTEGER NOT NULL DEFAULT 0")):
        if col not in lcols:
            conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {ddl}")

    # 全文去重（2026-11-13）：老库的 span_blobs 没有 hash 列，正文还躺在自己的 gz 里。
    # 补这一列而不是重建表——重建要把几十 MB 的 blob 搬一遍，中途挂掉就是数据没了，
    # 而补列只是让新写入走"引用"路径，老行靠 hash IS NULL 区分，读路径天然兼容。
    bcols = {r["name"] for r in conn.execute("PRAGMA table_info(span_blobs)")}
    if "hash" not in bcols:
        conn.execute("ALTER TABLE span_blobs ADD COLUMN hash TEXT")
    # 索引在补列之后建，且不放进 schema.sql：schema 是"建表"的脚本，跑在老库上时
    # 列还不存在，在那里建索引会让整个 init_db 抛错（应用直接起不来）。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_span_blobs_hash ON span_blobs(hash)")

    # 工具台账也要有 trace_id：只靠 (session,turn) 对齐，同一轮里"重试前那次失败"
    # 和"重试后那次成功"会混成两条并列记录，谁对应哪次模型决策就分不出来了。
    ccols2 = {r["name"] for r in conn.execute("PRAGMA table_info(tool_calls)")}
    if "trace_id" not in ccols2:
        conn.execute("ALTER TABLE tool_calls ADD COLUMN trace_id TEXT")

    # 记忆 scope 回填：偏好类一律跨会话。    # 实测发现早期抽取把 8 条记忆全写成 session → 新开一个会话就"什么都不记得"，
    # 而偏好（怎么讲、多长、要不要举例）按定义与具体会话无关。幂等，每次启动跑一遍。
    # 只动 preference：fact/episode 里确实可能有"只对这次面试有效"的内容，不替用户判断。
    try:
        n = conn.execute(
            "UPDATE agent_memory SET scope='global', scope_id=NULL "
            "WHERE kind='preference' AND scope='session' AND superseded_by IS NULL").rowcount
        if n:
            conn.commit()
    except sqlite3.OperationalError:
        pass

    # 关系层迁移①：embeddings 记模型版本。不记的话换 embedding 模型后新旧向量混存，
    # 余弦在两个空间之间算，检索结果无声劣化且无法甄别——比报错难查得多。
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(embeddings)")}
    if "model" not in ecols:
        conn.execute("ALTER TABLE embeddings ADD COLUMN model TEXT DEFAULT ''")
        from . import embedder
        conn.execute("UPDATE embeddings SET model=? WHERE model='' OR model IS NULL",
                     (embedder.MODEL_ID,))

    # 关系层迁移②：json 列里的题↔卡关联回灌进 card_question_links（match_method='legacy'）。
    # 用 Python 循环而不是 json_each：老数据可能有脏 json，一条坏行不该让整个迁移失败。
    # 存量关联是历次建卡写的硬关联，标 legacy 而不是编造一个匹配方式。
    n = conn.execute("SELECT COUNT(*) FROM card_question_links").fetchone()[0]
    if n == 0:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        for r in conn.execute(
                "SELECT id, related_card_ids_json FROM question_pool WHERE related_card_ids_json != '[]'"):
            try:
                ids = json.loads(r["related_card_ids_json"] or "[]")
            except (ValueError, TypeError):
                continue
            for cid in ids if isinstance(ids, list) else []:
                conn.execute(
                    "INSERT OR IGNORE INTO card_question_links "
                    "(question_id, card_id, match_method, created_at) VALUES (?,?,'legacy',?)",
                    (r["id"], cid, ts))
        # 迁移函数自己提交：调用方（init_db）会 commit，但任何绕过 init_db 的路径
        # （脚本、手工迁移、异常后重试）不 commit 就 close，整个迁移静默回滚——
        # 表结构在（DDL 隐式提交）而数据没灌进去，比报错难查得多。
        conn.commit()


def init_db():
    """执行 schema.sql 建库建表（幂等），并保证两套全文索引与内容表一致。"""
    ensure_dirs()
    conn = get_conn()
    _drop_stale_fts(conn)
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    _ensure_columns(conn)
    # 外部内容 FTS 与内容表漂移时 MATCH 只会静默漏召回，比报错更难查。
    # 千级行 rebuild 是毫秒级，启动时全量重建换确定性很划算。
    for table in _FTS_TRIGGERS:
        try:
            conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    # 不 close：连接留在线程池里被后续请求复用（close 掉也只是被探活机制重建，白绕一圈）
