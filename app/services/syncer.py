"""Markdown ↔ SQLite 同步：启动时全量扫描 knowledge/，把 Obsidian 手改的文件收编。"""
import json
import re
from pathlib import Path

import yaml

from ..core import config
from ..core.db import get_conn

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(path: Path, text: str | None = None) -> dict | None:
    if text is None:
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


_EMPTY_POINT = {"（待补充）", "（无）"}


def _section_bullets(body: str, heading: str) -> list[str]:
    """取 `## {heading}` 下的 `- xxx` 条目，到下一个 `## ` 为止。"""
    out: list[str] = []
    inside = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = s[3:].strip() == heading
            continue
        if inside and s.startswith("- "):
            item = s[2:].strip()
            if item and item not in _EMPTY_POINT:
                out.append(item)
    return out


def parse_md_body(text: str) -> dict:
    """从 md 正文解析出 question / title / points / pitfalls。

    为什么必须解析：空库启动时 `sync_all()` 要把卡收编回来，如果只插 id 和路径，
    收编出来的是「空壳卡」——覆盖统计算它有卡，Agent 检索却读不到任何要点。
    md 被写成事实源，收编就得真把内容读回来，否则等于没有事实源。
    """
    body = _strip_frontmatter(text)
    title, question = "", ""
    for line in body.splitlines():
        s = line.strip()
        if not title and s.startswith("# "):
            title = s[2:].strip()
        elif not question and s.startswith("**Q"):
            question = re.sub(r"^\*\*Q[：:]\s*\*\*\s*", "", s).strip()
        if title and question:
            break
    return {"title": title, "question": question,
            "points": _section_bullets(body, "要点"),
            "pitfalls": _section_bullets(body, "易错")}


def sync_all() -> int:
    """扫描 knowledge/**/*.md，缺行的按 md 建行（保留 DB 中已有的 mastery/srs_due）。返回收编数量。

    已有行只同步 file_path，**不动 status**：md 是内容事实源，但"这张卡还收不收"
    是维护者的判断（归档/合并），DB 记着，md 只是跟着标记，不能被反向覆盖。
    只有第一次收编（DB 里没有这行）才读 frontmatter 的 status——
    否则去重时归档掉的卡，会在空库重新 sync 后以 active 身份复活。
    """
    conn = get_conn()
    adopted = 0
    try:
        for path in config.KB_DIR.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm = parse_frontmatter(path, text)
            if not fm:
                continue
            rel = str(path.relative_to(config.KB_DIR)).replace("\\", "/")
            row = conn.execute("SELECT id FROM kb_cards WHERE id=?", (fm["id"],)).fetchone()
            if row:
                conn.execute("UPDATE kb_cards SET file_path=? WHERE id=?", (rel, fm["id"]))
                continue
            status = str(fm.get("status") or "active")
            if status not in ("active", "archived"):
                status = "active"
            body = parse_md_body(text)
            conn.execute(
                "INSERT INTO kb_cards (id,domain,topic,title,question,points_json,pitfalls_json,related_json,"
                "tags_json,source_type,source_ref,file_path,mastery,review_count,last_review_at,srs_due,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'0',0,NULL,NULL,?,?,?)",
                (
                    str(fm["id"]), str(fm["domain"]), str(fm.get("topic", "通用")),
                    str(fm.get("title") or body["title"] or path.stem),
                    body["question"],
                    json.dumps(body["points"], ensure_ascii=False),
                    json.dumps(body["pitfalls"], ensure_ascii=False),
                    json.dumps(fm.get("related", []), ensure_ascii=False), json.dumps(fm.get("tags", []), ensure_ascii=False),
                    str(fm.get("source", "manual")), None, rel, status,
                    str(fm.get("created", "")), str(fm.get("updated", "")),
                ),
            )
            adopted += 1
        conn.commit()
    finally:
        conn.close()
    return adopted
