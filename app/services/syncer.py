"""Markdown ↔ SQLite 同步：启动时全量扫描 knowledge/，把 Obsidian 手改的文件收编。"""
import json
import re
from pathlib import Path

import yaml

from ..core import config
from ..core.db import get_conn

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = FM_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict) or not fm.get("id") or not fm.get("domain"):
        return None
    return fm


def _strip_frontmatter(text: str) -> str:
    return FM_RE.sub("", text, count=1).strip()


def sync_all() -> int:
    """扫描 knowledge/**/*.md，缺行的按 md 建行（保留 DB 中已有的 mastery/srs_due）。返回收编数量。"""
    conn = get_conn()
    adopted = 0
    try:
        for path in config.KB_DIR.rglob("*.md"):
            fm = parse_frontmatter(path)
            if not fm:
                continue
            rel = str(path.relative_to(config.KB_DIR)).replace("\\", "/")
            row = conn.execute("SELECT id FROM kb_cards WHERE id=?", (fm["id"],)).fetchone()
            if row:
                conn.execute("UPDATE kb_cards SET file_path=? WHERE id=?", (rel, fm["id"]))
                continue
            rel_path = fm["id"].rsplit("-", 1)
            conn.execute(
                "INSERT INTO kb_cards (id,domain,topic,title,question,points_json,pitfalls_json,related_json,"
                "tags_json,source_type,source_ref,file_path,mastery,review_count,last_review_at,srs_due,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'0',0,NULL,NULL,'active',?,?)",
                (
                    str(fm["id"]), str(fm["domain"]), str(fm.get("topic", "通用")), str(fm.get("title", path.stem)),
                    "", json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False),
                    json.dumps(fm.get("related", []), ensure_ascii=False), json.dumps(fm.get("tags", []), ensure_ascii=False),
                    str(fm.get("source", "manual")), None, rel,
                    str(fm.get("created", "")), str(fm.get("updated", "")),
                ),
            )
            adopted += 1
        conn.commit()
    finally:
        conn.close()
    return adopted
