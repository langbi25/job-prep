"""知识卡片去重：找出「同一道题被重复建卡」，把多余的归档到保留卡上。

为什么要有这个脚本，而不是一句 SQL：
1) 重复不是一次性事故，是结构性风险。`draft_cards` 的去重闸门只挡「这道题已经有待审草稿」，
   挡不住「卡已经审批通过了，但覆盖统计仍判它无卡」——那会再起草一张，人再批一次。
   闸门修好之前攒下的重复，只能靠这个脚本清；修好之后再出现，也还得靠它清。
2) 归档不是删行。重复卡要留 md、留 merged_into 指针、把题目硬关联搬到保留卡上，
   还要删掉它的向量（embeddings 表不随 status 走），否则归档卡会继续从向量检索里冒出来。
3) 共用 md 的坑：domain/topic/title 全同算出同一个 rel_path，多张卡写同一个文件。
   这时改 md frontmatter 会把别的活卡一起标成归档（`_archive_card` 已修）。

判定口径（三者同时满足才算同一张卡）：
- 同一个 topic（归一化后相等，或一方包含另一方——`Spark Shuffle` vs `Spark Shuffle 实现`）；
- 标题归一化后相等，或短标题被长标题包含（`...时丢失...` vs `...丢失...`）；
- 都是 active。

用法：
    python scripts/dedupe_cards.py                              # 只体检，不改任何东西
    python scripts/dedupe_cards.py --apply                      # 备份后合并（自动挑要点最多的那张）
    python scripts/dedupe_cards.py --apply --keep kb-bigdata-sparkshu-001
"""
import argparse
import datetime
import difflib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config          # noqa: E402
from app.core.db import get_conn     # noqa: E402
from app.services import kb_service, multi_agent   # noqa: E402
from app.services.coverage_service import _norm_topic, _same_topic  # noqa: E402


def _norm(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "").lower()


def _points(card: dict) -> list[str]:
    try:
        v = json.loads(card["points_json"] or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _pitfalls(card: dict) -> list[str]:
    try:
        v = json.loads(card["pitfalls_json"] or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _same_card(a: dict, b: dict) -> bool:
    """同一张卡：同 topic + 标题等价。

    标题等价有三条路，缺一不可：
    ① 归一化后完全相同（`Memory 怎么做的？` 建 5 次就是 5 个一模一样）；
    ② 一方被另一方包含（`工具链能否 Skill 化？` / `…如何设计一套可复用的方案？`）；
    ③ 相似度 ≥ 0.88（序列比对）。为什么需要 ③：同一道题两次建卡，标题会长出
       「压缩上下文**时**丢失工具调用历史，**，**导致模型…」这种少一个字、少一个逗号的差别，
       既不完全相同也不互相包含，但人一眼就知道是同一道题。
    """
    if not _same_topic(a["topic"], b["topic"]):
        return False
    na, nb = _norm(a["title"]), _norm(b["title"])
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 8 and short in long_:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.88


def _overlap(a: dict, b: dict) -> float:
    """要点文字重合度（3-gram Jaccard），只用于报告里给人看，不参与判定。"""
    def grams(card):
        text = _norm(" ".join(_points(card)))
        return {text[i:i + 3] for i in range(len(text) - 2)}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def find_groups(conn) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kb_cards WHERE status='active' ORDER BY id")]
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if _same_card(rows[i], rows[j]):
                parent[find(j)] = find(i)

    buckets: dict[int, list[dict]] = {}
    for i, card in enumerate(rows):
        buckets.setdefault(find(i), []).append(card)
    groups = [g for g in buckets.values() if len(g) > 1]
    for g in groups:
        g.sort(key=lambda c: (-len(_points(c)), -len(_pitfalls(c)), c["created_at"], c["id"]))
    groups.sort(key=lambda g: g[0]["topic"])
    return groups


def _shared_md(group: list[dict]) -> bool:
    return len({c["file_path"] for c in group}) == 1


def _blank(cid: str, n: int) -> str:
    return " " * max(0, n - len(cid))


def _keeper_for(group: list[dict], keep_ids: set[str]) -> str:
    for c in group:
        if c["id"] in keep_ids:
            return c["id"]
    return _pick_keeper(group)["id"]


def print_report(groups: list[dict], keep_ids: set[str]) -> None:
    if not groups:
        print("没有重复卡。")
        return
    total_dup = sum(len(g) - 1 for g in groups)
    print(f"发现 {len(groups)} 组重复，可归档 {total_dup} 张：\n")
    for i, g in enumerate(groups, 1):
        keeper = _keeper_for(g, keep_ids)
        print(f"[{i}] topic={g[0]['topic']}  共 {len(g)} 张"
              f"{'（共用同一个 md）' if _shared_md(g) else ''}")
        for c in g:
            mark = "保留" if c["id"] == keeper else "归档"
            ov = "" if c["id"] == keeper else f" 要点重合 {_overlap(g[0], c) * 100:.0f}%"
            print(f"    {mark}  {c['id']}{_blank(c['id'], 26)}"
                  f"要点 {len(_points(c)):2} 坑点 {len(_pitfalls(c)):2}"
                  f"  {c['title'][:34]}{ov}")
        print()


def _pick_keeper(group: list[dict]) -> dict:
    return group[0]   # find_groups 已按「要点数 → 坑点数 → 创建时间」排好


def _backup() -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(config.DATA_DIR) / "_backup" / f"dedupe-{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    db = Path(config.DATA_DIR) / "kb.sqlite"
    if db.exists():
        shutil.copy2(db, dest / "kb.sqlite")
    shutil.copytree(config.KB_DIR, dest / "knowledge")
    return dest


def _rewrite_md(conn, card_id: str) -> None:
    """按库里的内容重写保留卡的 md。

    共用 md 的组里，文件内容是「最后一个写它的人」留下的，可能正好是归档卡；
    不重写的话，md 与库对不上，而这个项目里 md 是事实源。
    """
    row = conn.execute("SELECT * FROM kb_cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        return
    path = Path(config.KB_DIR) / row["file_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kb_service.card_to_md(row), encoding="utf-8")
    kb_service._index_card(dict(row))


def apply_groups(conn, groups: list[dict], keep_ids: set[str]) -> list[dict]:
    done = []
    for g in groups:
        keeper = _keeper_for(g, keep_ids)
        for c in g:
            if c["id"] == keeper:
                continue
            res = multi_agent._merge_card(conn, {"keeper_card_id": keeper, "duplicate_card_id": c["id"]})
            done.append(res)
        _rewrite_md(conn, keeper)
        conn.commit()
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="知识卡片去重")
    ap.add_argument("--apply", action="store_true", help="真执行（默认只体检）")
    ap.add_argument("--keep", action="append", default=[], metavar="CARD_ID",
                    help="指定保留卡，可多次传入；不指定则自动挑要点最多的那张")
    args = ap.parse_args()

    conn = get_conn()
    try:
        groups = find_groups(conn)
        keep_ids = set(args.keep)
        bad = keep_ids - {c["id"] for g in groups for c in g}
        if bad:
            raise SystemExit(f"这些 --keep 不在任何重复组里：{sorted(bad)}")
        print_report(groups, keep_ids)
        if not groups or not args.apply:
            if groups:
                print("（体检模式，未改动。加 --apply 执行）")
            return 0

        dest = _backup()
        print(f"已备份 → {dest}")
        done = apply_groups(conn, groups, keep_ids)
        for d in done:
            print(f"  归档 {d['merged']} → 保留 {d['into']}"
                  f"（题目关联搬了 {d['linked_questions']} 条）")

        act = conn.execute("SELECT COUNT(*) AS n FROM kb_cards WHERE status='active'").fetchone()["n"]
        arc = conn.execute("SELECT COUNT(*) AS n FROM kb_cards WHERE status='archived'").fetchone()["n"]
        print(f"\n完成：active={act} archived={arc}，归档 {len(done)} 张")
        left = find_groups(conn)
        print(f"复查：还剩 {len(left)} 组重复")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
