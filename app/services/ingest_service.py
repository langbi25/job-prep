"""面经采集状态机：pending → extracted → confirmed / discarded。

三通道投喂（A 粘贴 / B URL 抓正文 / C bookmarklet），原文先保真存档，
LLM 抽取后必须人工确认才入库——确认页即"人工质检环节"。
合规边界：单次手动触发、不做定时爬取、不绕登录态、不并发抓取。
"""
import datetime
import json
import random
import re
import shutil
import sqlite3

import httpx

from ..core import config, llm
from ..core.db import get_conn
from . import rag_service
from .kb_service import today


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _new_ingest_id() -> str:
    return f"ing-{datetime.datetime.now():%Y%m%d-%H%M%S}-{random.randint(1000, 9999)}"


def _source_site(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    host = m.group(1) if m else ""
    for key in ("nowcoder", "zhihu", "weixin", "juejin", "csdn", "xiaohongshu"):
        if key in host:
            return key
    return host or "unknown"


def _fetch_url_text(url: str) -> str:
    """通道 B：抓取 URL 并抽取正文。失败或正文过短时抛异常，由调用方降级为粘贴。"""
    try:
        import trafilatura
    except ImportError:
        raise RuntimeError("未安装 trafilatura（pip install trafilatura），请使用粘贴投喂")
    resp = httpx.get(url, timeout=10, follow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    })
    resp.raise_for_status()
    text = trafilatura.extract(resp.text, include_comments=False) or ""
    if len(text.strip()) < 200:
        raise RuntimeError("正文抽取过短（该站可能需要登录或反爬），请改用粘贴投喂")
    return text


def create_ingest(source_type: str, text: str = "", url: str = "", selection: str = "") -> dict:
    """投喂入口：原文立即保真存档到 inbox/pending/，状态 pending。"""
    ingest_id = _new_ingest_id()
    if source_type == "url" and url and not text:
        text = _fetch_url_text(url)          # 失败时抛 HTTPException，不产生脏数据
    if not text and selection:
        text = selection
    if not text:
        raise ValueError("text/url 均为空，无内容可投喂")

    rel_path = f"pending/{ingest_id}.md"
    raw_path = config.INBOX_DIR / rel_path
    header = f"---\ningest_id: {ingest_id}\nsource_type: {source_type}\nsource_url: {url}\ncreated: {_now()}\n---\n\n"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(header + text, encoding="utf-8")

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO ingests (id,source_type,source_url,source_site,raw_path,status,created_at) VALUES (?,?,?,?,?,'pending',?)",
            (ingest_id, source_type, url or None, _source_site(url), rel_path, _now()),
        )
        conn.commit()
        return {"id": ingest_id, "status": "pending", "raw_path": rel_path}
    finally:
        conn.close()


def run_extract(ingest_id: str) -> dict:
    """触发 LLM 抽取，结果缓存进 extracted_json，状态 extracted（等待人工确认）。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM ingests WHERE id=?", (ingest_id,)).fetchone()
        if not row:
            raise ValueError(f"ingest {ingest_id} 不存在")
        raw = (config.INBOX_DIR / row["raw_path"]).read_text(encoding="utf-8")
        raw = re.sub(r"^---\s*\n.*?\n---\s*\n", "", raw, count=1, flags=re.DOTALL)
        try:
            extracted = llm.extract_interview(raw.strip(), row["source_url"] or "")
            conn.execute(
                "UPDATE ingests SET extracted_json=?, status='extracted', error=NULL WHERE id=?",
                (json.dumps(extracted, ensure_ascii=False), ingest_id),
            )
        except Exception as e:
            conn.execute("UPDATE ingests SET status='failed', error=? WHERE id=?", (str(e), ingest_id))
            conn.commit()
            raise
        conn.commit()
        return extracted
    finally:
        conn.close()


def _find_similar(conn: sqlite3.Connection, text: str) -> str | None:
    """题目查重：精确 → FTS → 向量语义（rag_service，余弦阈值 0.88）。"""
    if conn.execute("SELECT id FROM question_pool WHERE text=?", (text,)).fetchone():
        return conn.execute("SELECT id FROM question_pool WHERE text=?", (text,)).fetchone()["id"]
    return rag_service.find_similar_question(text)


def _next_question_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*) AS n FROM question_pool").fetchone()
    return f"q-{row['n'] + 1:04d}"


def _next_interview_id(conn: sqlite3.Connection) -> str:
    ym = f"fi-{datetime.datetime.now():%Y%m}"
    row = conn.execute("SELECT COUNT(*) AS n FROM feed_interviews WHERE id LIKE ?", (ym + "%",)).fetchone()
    return f"{ym}-{row['n'] + 1:04d}"


def confirm_ingest(ingest_id: str, payload: dict) -> dict:
    """人工确认入库：建面经 + 题目池去重入池 + 原文归档。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM ingests WHERE id=?", (ingest_id,)).fetchone()
        if not row:
            raise ValueError(f"ingest {ingest_id} 不存在")
        if row["status"] == "confirmed":
            raise ValueError("该采集已确认入库")

        fi_id = _next_interview_id(conn)
        raw_rel = str(row["raw_path"]).replace("pending", "archived")
        iv_rel = f"{datetime.datetime.now():%Y}/{fi_id}.md"
        kept = [q for q in payload["questions"] if q.get("keep", True)]

        conn.execute(
            "INSERT INTO feed_interviews (id,ingest_id,company,position,direction,round,interview_date,"
            "result,source_type,source_url,raw_path,question_count,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fi_id, ingest_id, payload.get("company"), payload.get("position"), payload["direction"],
             payload.get("round"), payload.get("interview_date"), payload.get("result", "unknown"),
             row["source_type"], row["source_url"], raw_rel, len(kept), _now()),
        )

        today_s = today()
        for i, q in enumerate(kept, 1):
            text = (q.get("text") or "").strip()
            if not text:
                continue
            qid = q.get("merge_into") or _find_similar(conn, text)
            if qid:
                dup = conn.execute("SELECT id, freq_count FROM question_pool WHERE id=?", (qid,)).fetchone()
                if dup:
                    conn.execute(
                        "UPDATE question_pool SET freq_count=freq_count+1, last_seen_at=? WHERE id=?",
                        (today_s, qid),
                    )
                    aliases = json.loads(conn.execute("SELECT aliases_json FROM question_pool WHERE id=?", (qid,)).fetchone()["aliases_json"])
                    if text not in aliases:
                        aliases.append(text)
                        conn.execute("UPDATE question_pool SET aliases_json=? WHERE id=?", (json.dumps(aliases, ensure_ascii=False), qid))
            else:
                qid = _next_question_id(conn)
                conn.execute(
                    "INSERT INTO question_pool (id,text,aliases_json,qtype,topic,freq_count,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,1,?,?)",
                    (qid, text, json.dumps([], ensure_ascii=False), q.get("qtype"), q.get("topic"), today_s, today_s),
                )
                # question_fts 由 questions_ai 触发器维护，不再手工插入索引
            conn.execute(
                "INSERT OR IGNORE INTO question_occurrences (question_id,interview_id,raw_text,ask_order) VALUES (?,?,?,?)",
                (qid, fi_id, text, i),
            )

        # 面经原文归档（保真 + 补写抽取结果）
        raw_abs = config.INBOX_DIR / row["raw_path"]
        iv_abs = config.IV_DIR / iv_rel
        iv_abs.parent.mkdir(parents=True, exist_ok=True)
        raw_text = raw_abs.read_text(encoding="utf-8")
        qlines = "\n".join(f"{i}. [{q.get('qtype', '')}|{q.get('topic', '')}] {q.get('text', '')}" for i, q in enumerate(kept, 1))
        iv_abs.write_text(raw_text + f"\n\n## 抽取的题目（共 {len(kept)} 道）\n\n{qlines}\n", encoding="utf-8")

        archived_abs = config.INBOX_DIR / raw_rel
        archived_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(raw_abs), str(archived_abs))

        conn.execute(
            "UPDATE ingests SET status='confirmed', confirmed_at=? WHERE id=?", (_now(), ingest_id)
        )
        conn.commit()
        return {"interview_id": fi_id, "questions_kept": len(kept)}
    finally:
        conn.close()


def discard_ingest(ingest_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT raw_path FROM ingests WHERE id=?", (ingest_id,)).fetchone()
        if not row:
            raise ValueError(f"ingest {ingest_id} 不存在")
        raw_abs = config.INBOX_DIR / row["raw_path"]
        if raw_abs.exists():
            arch = config.INBOX_DIR / "archived" / raw_abs.name
            arch.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(raw_abs), str(arch))
        conn.execute("UPDATE ingests SET status='discarded' WHERE id=?", (ingest_id,))
        conn.commit()
        return {"id": ingest_id, "status": "discarded"}
    finally:
        conn.close()


def list_ingests(status: str | None = None, limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        if status:
            rows = conn.execute("SELECT * FROM ingests WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ingests ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["extracted_json"]:
                d["extracted"] = json.loads(d["extracted_json"])
            out.append(d)
        return out
    finally:
        conn.close()
