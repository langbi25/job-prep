"""知识卡片服务：Markdown 是内容真相源，SQLite 是索引与状态库。"""
import datetime
import json
import re
import sqlite3

from ..core import config
from ..core.db import get_conn


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.date.today().isoformat()


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip() or "untitled"


_FM_SPLIT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def update_md_meta(rel_path: str, updates: dict) -> bool:
    """更新卡片 md 的 frontmatter 字段（不存在则追加）。

    归档/合并必须同步改 md——md 是事实源，只改库的话，
    换台机器（或 CI 空库）重新 sync 时，归档卡会以 active 身份复活。
    """
    p = config.KB_DIR / rel_path
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    m = _FM_SPLIT_RE.match(text)
    if not m:
        return False
    lines = m.group(1).splitlines()
    rest = text[m.end():]
    for key, val in updates.items():
        if val is None:
            continue
        for i, l in enumerate(lines):
            if l.startswith(f"{key}:"):
                lines[i] = f"{key}: {val}"
                break
        else:
            lines.append(f"{key}: {val}")
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\n" + rest, encoding="utf-8")
    return True


def _next_card_id(conn: sqlite3.Connection, domain: str, topic: str) -> str:
    abbr = re.sub(r"[^a-zA-Z]", "", topic)[:8].lower() or "gen"
    prefix = f"kb-{domain}-{abbr}-"
    row = conn.execute(
        "SELECT id FROM kb_cards WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",)
    ).fetchone()
    n = int(row["id"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{n:03d}"


def card_to_md(c: sqlite3.Row | dict) -> str:
    points_raw = c["points_json"] if isinstance(c["points_json"], str) else json.dumps(c.get("points", []), ensure_ascii=False)
    pitfalls_raw = c["pitfalls_json"] if isinstance(c["pitfalls_json"], str) else json.dumps(c.get("pitfalls", []), ensure_ascii=False)
    points = "\n".join(f"- {x}" for x in json.loads(points_raw)) or "- （待补充）"
    pitfalls = "\n".join(f"- {x}" for x in json.loads(pitfalls_raw)) or "- （无）"
    tags_val = json.loads(c["tags_json"]) if isinstance(c["tags_json"], str) else c.get("tags", [])
    front = (
        "---\n"
        f"id: {c['id']}\ndomain: {c['domain']}\ntopic: {c['topic']}\ntitle: {c['title']}\n"
        f"mastery: {c['mastery']}\nsrs_due: \"{c['srs_due'] or ''}\"\n"
        f"tags: {json.dumps(tags_val, ensure_ascii=False)}\nrelated: []\nsource: {c['source_type']}\n"
        f"created: {c['created_at'][:10]}\nupdated: {c['updated_at'][:10]}\n---\n\n"
    )
    body = f"# {c['title']}\n\n**Q：** {c['question'] or ''}\n\n## 要点\n\n{points}\n\n## 易错\n\n{pitfalls}\n"
    return front + body


def create_card(data: dict) -> dict:
    conn = get_conn()
    try:
        cid = _next_card_id(conn, data["domain"], data["topic"])
        rel_path = f"{data['domain']}/{_safe_name(data['topic'])}/{_safe_name(data['title'])}.md"
        card = {
            "id": cid, "domain": data["domain"], "topic": data["topic"], "title": data["title"],
            "question": data.get("question", ""), "points_json": json.dumps(data.get("points", []), ensure_ascii=False),
            "pitfalls_json": json.dumps(data.get("pitfalls", []), ensure_ascii=False),
            "related_json": "[]", "tags_json": json.dumps(data.get("tags", []), ensure_ascii=False),
            "source_type": data.get("source_type", "manual"), "source_ref": None,
            "file_path": rel_path, "mastery": 0, "review_count": 0, "last_review_at": None,
            "srs_due": None, "created_at": now(), "updated_at": now(),
        }
        conn.execute(
            "INSERT INTO kb_cards (id,domain,topic,title,question,points_json,pitfalls_json,related_json,"
            "tags_json,source_type,source_ref,file_path,mastery,review_count,last_review_at,srs_due,status,created_at,updated_at) "
            "VALUES (:id,:domain,:topic,:title,:question,:points_json,:pitfalls_json,:related_json,"
            ":tags_json,:source_type,:source_ref,:file_path,:mastery,:review_count,:last_review_at,:srs_due,'active',:created_at,:updated_at)",
            card,
        )
        conn.commit()
        path = config.KB_DIR / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(card_to_md(card), encoding="utf-8")
        card["points"] = json.loads(card["points_json"])
        card["pitfalls"] = json.loads(card["pitfalls_json"])
        card["tags"] = json.loads(card["tags_json"])
        _index_card(card)
        return card
    finally:
        conn.close()


def _index_card(card: dict) -> None:
    """新卡落地即建向量。

    为什么不能等启动时的懒索引：飞轮的顺序是"建卡 → 立刻拿这张卡去起草改造方案"。
    向量缺失时 _match_card 找不到它，方案会退化成 card_id=None 的裸计划，
    实战经验回灌也就没有互链目标。这里补上，成本只是一次 embed。
    """
    from ..core import embedder

    try:
        embedder.ensure_indexed("card", [(
            card["id"],
            " ".join([card["title"], card.get("question") or "", card["points_json"]]),
        )])
    except Exception:  # 向量是增强项，失败不能阻断建卡
        pass


def link_related(card_id: str, other_id: str) -> bool:
    """双向关联两张卡（原卡 ↔ 实战卡）。related 同时写库与 md frontmatter。

    related 是"深度"的载体：原子卡是通用答案，实战卡是"我真的做过"，
    两张卡互链后，复习原子卡时能看到自己的实战，检索也能顺带召回。
    """
    conn = get_conn()
    try:
        ok = False
        for a, b in ((card_id, other_id), (other_id, card_id)):
            row = conn.execute("SELECT related_json, file_path FROM kb_cards WHERE id=?", (a,)).fetchone()
            if not row:
                continue
            rel = json.loads(row["related_json"] or "[]")
            if b not in rel:
                rel.append(b)
                conn.execute("UPDATE kb_cards SET related_json=?, updated_at=? WHERE id=?",
                             (json.dumps(rel, ensure_ascii=False), now(), a))
                update_md_meta(row["file_path"], {"related": json.dumps(rel, ensure_ascii=False)})
            ok = True
        conn.commit()
        return ok
    finally:
        conn.close()


def list_cards(domain: str | None = None, topic: str | None = None, q: str | None = None, limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        if q:
            rows = conn.execute(
                "SELECT c.* FROM kb_cards_fts f JOIN kb_cards c ON c.rowid = f.rowid "
                "WHERE kb_cards_fts MATCH ? AND c.status='active' ORDER BY c.updated_at DESC LIMIT ?",
                (q, limit),
            ).fetchall()
        else:
            sql = "SELECT * FROM kb_cards WHERE status='active'"
            args: list = []
            if domain:
                sql += " AND domain=?"
                args.append(domain)
            if topic:
                sql += " AND topic=?"
                args.append(topic)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
        return [
            dict(r) | {"points": json.loads(r["points_json"]), "pitfalls": json.loads(r["pitfalls_json"]), "tags": json.loads(r["tags_json"])}
            for r in rows
        ]
    finally:
        conn.close()


def get_card(cid: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM kb_cards WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["points"] = json.loads(d["points_json"])
        d["pitfalls"] = json.loads(d["pitfalls_json"])
        d["tags"] = json.loads(d["tags_json"])
        p = config.KB_DIR / d["file_path"]
        d["markdown"] = p.read_text(encoding="utf-8") if p.exists() else ""
        return d
    finally:
        conn.close()
