"""Harness 管理：把散在代码里的工具、散在 markdown 里的技能，收敛成一个可查可改可验的面。

平台化的关键不是"加个页面"，是**消掉第二事实源**：

- 工具清单原来在 `console_service` 里手工 `harness.TOOLS + agent_tools.SCHEMAS`，
  编排、平台各写一遍——加一个工具要改三处，漏一处就是"面板上没有但模型能调"。
  现在只有 `all_tools()` 一处，面板看到的就是模型能用的。
- 技能原来只能改文件，改完不知道有没有写错：`tools` 里工具名拼错一个字母，
  路由选中它之后白名单被收成空集，模型什么都调不了，而日志里只有一句"模型没调工具"。
  现在保存前强制 lint，错误码直接指出是哪一行的问题。

管理台**不自己存技能**：文件仍是唯一事实源，平台只是受控编辑器（解析 → 校验 → 备份 → 原子写）。
一旦平台另存一份数据库副本，"文件改了但面板没变"就会成为新的日常。
"""
import difflib
import hashlib
import re
import shutil
import time
from pathlib import Path

from ..core.config import BASE_DIR
from ..core.db import get_conn
from ..core import tool_flags
from ..skills import registry
from . import agent_tools, harness

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
BACKUP_DIR = registry.SKILLS_DIR / ".backup"
DEFAULT_DAYS = 14

# 任何技能都能用的工具：记忆读写 + 大结果翻页。
# 理由：技能限制的是"这件事该怎么做"，不是"不许记得用户是谁"；
# 而翻页是上下文基础设施，不该因为换了技能就看不到自己刚拿到的大结果。
# 放在这里而不是各调用处再写一遍——第三处硬编码就是这么长出来的。
EXEMPT_TOOLS = frozenset({"remember", "recall_memory", "fetch_tool_result"})

ISSUE_WEIGHT = {"error": 12, "warn": 4, "info": 0}


# ---------------------------------------------------------------- 工具目录

def _all_tools_raw() -> tuple[list[dict], dict]:
    """全量工具（含已停用）。面板与 lint 要用它，否则工具一停用就从列表里消失，
    再也开不回来；lint 也会把"引用了已停用工具"错报成"引用了不存在的工具"。"""
    tools = list(harness.TOOLS) + list(agent_tools.SCHEMAS)
    impl = {**harness._IMPL, **agent_tools.IMPL}
    return tools, impl


def all_tools() -> tuple[list[dict], dict]:
    """**模型可见**的工具。唯一合并点——别处不要再拼 TOOLS + SCHEMAS。

    停用的工具连 impl 一起剔除：只删 schema 不删 impl，虽然模型看不见了，
    但技能白名单点到名字仍能在 impl 里找到函数，停用就只停了一半。
    """
    tools, impl = _all_tools_raw()
    flags = tool_flags.all_flags()
    if not flags:
        return tools, impl
    tools = [t for t in tools
             if ((t.get("function") or {}).get("name") or "") not in flags]
    impl = {k: v for k, v in impl.items() if k not in flags}
    return tools, impl


def _tool_names() -> set[str]:
    # 用全量：停用的工具仍然"存在"，lint 不该把它报成拼写错误
    tools, _ = _all_tools_raw()
    return {(t.get("function") or {}).get("name") or "" for t in tools} - {""}



def _skill_tool_refs(items: dict | None = None) -> dict[str, list[dict]]:
    """工具 → 引用它的技能（含组合技能段里的 require_tools）。

    反查比正查有用：改一个工具的行为前，先知道谁会受影响。
    items 可传入已加载的技能目录，避免每次调用都重新 stat 一遍 skills/。
    """
    refs: dict[str, list[dict]] = {}
    for sk in (registry.load_all() if items is None else items).values():
        for t in sk.tools:
            refs.setdefault(t, []).append({"skill": sk.name, "title": sk.title, "via": "tools"})
        for t in sk.require_tools:
            refs.setdefault(t, []).append({"skill": sk.name, "title": sk.title, "via": "require"})
        for raw in sk.steps:
            for t in (raw or {}).get("require_tools") or []:
                refs.setdefault(str(t), []).append(
                    {"skill": sk.name, "title": sk.title, "via": "step"})
    return refs


def tool_stats(days: int = DEFAULT_DAYS, conn=None) -> dict[str, dict]:
    since = _since(days)
    own = conn is None
    conn = get_conn() if own else conn
    try:
        rows = conn.execute(
            "SELECT tool, COUNT(*) AS calls, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_calls, "
            "SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END) AS cached_calls, "
            "SUM(retried) AS retried_calls, "
            "AVG(CASE WHEN cached=0 AND latency_ms>0 THEN latency_ms END) AS avg_ms, "
            "MAX(latency_ms) AS max_ms, MAX(ts) AS last_ts "
            "FROM tool_calls WHERE ts >= ? GROUP BY tool", (since,)).fetchall()
        errs = conn.execute(
            "SELECT tool, COALESCE(error_kind,'failed') AS kind, COUNT(*) AS n "
            "FROM tool_calls WHERE ts >= ? AND status='error' GROUP BY tool, kind "
            "ORDER BY n DESC", (since,)).fetchall()
    finally:
        if own:
            conn.close()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        calls = d["calls"] or 0
        d["ok_rate"] = round((d["ok_calls"] or 0) / calls, 4) if calls else None
        d["avg_ms"] = int(d["avg_ms"] or 0)
        d["errors"] = []
        out[d["tool"]] = d
    for r in errs:
        if r["tool"] in out:
            out[r["tool"]]["errors"].append({"kind": r["kind"], "n": r["n"]})
    return out


def tool_entries(days: int = DEFAULT_DAYS, *, items: dict | None = None,
                 stats: dict | None = None) -> list[dict]:
    tools, impl = _all_tools_raw()      # 含已停用：面板要能把它开回来
    refs = _skill_tool_refs(items)
    stats = tool_stats(days) if stats is None else stats
    flags = tool_flags.all_flags()
    out = []
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        flag = flags.get(name) or {}
        out.append({
            "name": name,
            "description": fn.get("description") or "",
            "read_only": name not in agent_tools.WRITE_TOOLS,
            "global_exempt": name in EXEMPT_TOOLS,
            "source": "builtin" if name in harness._IMPL else "action",
            "impl": name in impl,
            "enabled": name not in flags,
            "disabled_reason": flag.get("reason") or "",
            "disabled_at": flag.get("at"),
            "params": [{
                "name": k, "type": (v or {}).get("type") or "any",
                "required": k in required,
                "enum": (v or {}).get("enum"),
                "description": (v or {}).get("description") or "",
            } for k, v in props.items()],
            "used_by": refs.get(name, []),
            "stats": stats.get(name) or {"calls": 0, "ok_calls": 0, "ok_rate": None,
                                         "avg_ms": 0, "last_ts": None, "errors": []},
        })
    out.sort(key=lambda x: (not x["read_only"], x["name"]))
    return out


# ---------------------------------------------------------------- 技能体检

def lint_skill(sk, *, tool_names: set[str] | None = None,
               skills: dict | None = None) -> list[dict]:
    """静态体检。只判"一定会出问题"的结构，不猜语义好坏。

    每条都对应一个真实故障模式，不是风格建议：
    - E_UNKNOWN_TOOL：工具名拼错 → 白名单收成空集，模型无工具可用
    - E_REQUIRE_NOT_ALLOWED：验收要求调一个白名单外的工具 → 这一项永远不通过
    - E_EMPTY_BODY：正文空 → 注入后模型只看到白名单，不知道怎么干
    - W_NO_WHEN：没写 when → 自动路由永远选不中
    """
    tool_names = _tool_names() if tool_names is None else tool_names
    skills = registry.load_all() if skills is None else skills
    out: list[dict] = []

    def add(level: str, code: str, msg: str) -> None:
        out.append({"level": level, "code": code, "msg": msg})

    if sk.disabled:
        add("info", "I_DISABLED", "已停用：不参与自动路由，也不能被组合技能引用")

    for t in sk.tools:
        if t not in tool_names:
            add("error", "E_UNKNOWN_TOOL", f"tools 引用了不存在的工具「{t}」")
    for t in sk.require_tools:
        if t not in tool_names:
            add("error", "E_UNKNOWN_TOOL", f"require_tools 引用了不存在的工具「{t}」")
        elif sk.tools and t not in sk.tools:
            add("error", "E_REQUIRE_NOT_ALLOWED",
                f"验收要求调用「{t}」，但它不在本技能的工具白名单里——这一项永远不会通过")

    if not sk.when:
        add("warn", "W_NO_WHEN", "没有 when：自动路由永远选不中它（只能显式指定）")

    if sk.path:
        stem = Path(sk.path).stem
        if stem != sk.name:
            add("warn", "W_NAME_MISMATCH",
                f"name={sk.name} 与文件名 {stem} 不一致：改的时候容易改错一个")

    if sk.steps:
        if len(sk.steps) > registry.MAX_SEGMENTS:
            add("warn", "W_TOO_MANY_STEPS",
                f"声明了 {len(sk.steps)} 段，超过上限 {registry.MAX_SEGMENTS}，多余的会被丢弃")
        for raw in sk.steps:
            child_name = str((raw or {}).get("skill") or "").strip()
            if not child_name:
                add("error", "E_STEP_EMPTY", "有一段没写 skill 字段")
                continue
            child = skills.get(child_name)
            if child is None:
                add("error", "E_STEP_UNKNOWN", f"引用了不存在的技能「{child_name}」")
                continue
            if child.steps:
                add("error", "E_STEP_NESTED",
                    f"「{child_name}」本身是组合技能：只允许一层，否则段数指数增长")
            if child.disabled:
                add("error", "E_STEP_DISABLED",
                    f"「{child_name}」已停用：这一段会被静默跳过")
            for t in (raw or {}).get("require_tools") or []:
                t = str(t)
                if t not in tool_names:
                    add("error", "E_UNKNOWN_TOOL",
                        f"段「{child_name}」的 require_tools 引用了不存在的工具「{t}」")
                elif child.tools and t not in child.tools:
                    add("warn", "W_STEP_REQUIRE_OUTSIDE",
                        f"段「{child_name}」要求调用「{t}」，但它不在该段技能的白名单里")
    elif not sk.body.strip():
        add("error", "E_EMPTY_BODY",
            "正文为空：注入后模型只看到工具白名单，没有步骤可依")

    return out


def skill_entries(days: int = DEFAULT_DAYS, *, items: dict | None = None,
                  tool_names: set[str] | None = None, conn=None) -> list[dict]:
    items = registry.load_all() if items is None else items
    tool_names = _tool_names() if tool_names is None else tool_names
    own = conn is None
    conn = get_conn() if own else conn
    try:
        st = {r["skill"]: dict(r) for r in registry.stats(conn=conn)}
        run_tools = _run_tools_used(conn=conn)
    finally:
        if own:
            conn.close()

    out = []
    disabled = set(tool_flags.all_flags())
    for sk in items.values():
        issues = lint_skill(sk, tool_names=tool_names, skills=items)
        stat = st.get(sk.name) or {"runs": 0, "ok_runs": 0, "avg_ms": 0}
        runs = int(stat.get("runs") or 0)
        # 停用工具的连带影响要说在体检里：技能白名单里的工具被停用后，
        # 这个技能跑起来必然验收失败（缺必需工具），而 run 记录只会显示"失败"，
        # 看不出原因——那是排查起来最费时的一类。
        hit = sorted({t for t in list(sk.tools) + list(sk.require_tools) if t in disabled})
        if hit:
            issues.append({"level": "warn", "code": "W_TOOL_DISABLED",
                           "msg": f"依赖的工具已被平台停用：{'、'.join(hit)}——"
                                  f"本技能跑起来会因缺必需工具而验收失败"})
        if not sk.disabled and runs == 0:
            issues.append({"level": "info", "code": "I_NEVER_RUN",
                           "msg": "还没跑过一次：路由规则是否有效、白名单是否够用都未经验证"})
        if runs >= 3 and sk.tools:
            used = run_tools.get(sk.name, set())
            unused = [t for t in sk.tools if t not in used]
            if unused and len(unused) == len(sk.tools):
                issues.append({"level": "info", "code": "I_TOOLS_UNUSED",
                               "msg": f"跑过 {runs} 次，白名单里的工具一个都没调过：可能白名单太宽或"
                                      f"SOP 没让模型真的去查"})
        row = sk.as_dict()
        row["path"] = sk.path
        row["body_chars"] = len(sk.body)
        row["stats"] = {"runs": runs, "ok_runs": int(stat.get("ok_runs") or 0),
                        "avg_ms": int(stat.get("avg_ms") or 0),
                        "tokens": int(stat.get("tokens") or 0),
                        "cost_cny": float(stat.get("cost_cny") or 0),
                        "avg_cost": float(stat.get("avg_cost") or 0)}
        row["segments"] = ([s.skill.name for s in registry.expand(sk, skills=items)]
                           if sk.steps else [])
        row["issues"] = issues
        out.append(row)
    out.sort(key=lambda x: (x["disabled"], -x["stats"]["runs"], x["name"]))
    return out


def _run_tools_used(days: int = 90, conn=None) -> dict[str, set[str]]:
    """每个技能实际调过哪些工具。只取近 days 天，避免台账长起来后全表扫。

    I_TOOLS_UNUSED 是"提示"级：它说的是"最近这批运行里白名单没用上"，
    不是"历史上从没用过"。限定窗口反而更准——三个月前的用法说明不了现在。
    """
    own = conn is None
    conn = get_conn() if own else conn
    try:
        rows = conn.execute(
            "SELECT skill, GROUP_CONCAT(tools_used) AS all_used FROM skill_runs "
            "WHERE created_at >= ? GROUP BY skill", (_since(days),)).fetchall()
    finally:
        if own:
            conn.close()
    out: dict[str, set[str]] = {}
    for r in rows:
        out[r["skill"]] = {t for t in (r["all_used"] or "").split(",") if t}
    return out


def _lint_from(skill_rows: list[dict], tool_rows: list[dict]) -> dict:
    """用已经算好的技能表/工具表合成体检结果。

    分开成函数是为了让 overview / lint / board 共用同一份计算：
    原来 lint_all() 自己再跑一遍 skill_entries + tool_entries，
    于是 overview 一次请求把同一份活干了两遍。
    """
    issues: list[dict] = []
    for e in skill_rows:
        for i in e["issues"]:
            issues.append({**i, "skill": e["name"]})

    for t in tool_rows:
        if not t["impl"]:
            issues.append({"level": "error", "code": "E_NO_IMPL", "skill": "-",
                           "msg": f"工具「{t['name']}」有 schema 但找不到实现，模型调用必报未知工具"})
        if not t["used_by"] and not t["global_exempt"]:
            issues.append({"level": "info", "code": "I_ORPHAN_TOOL", "skill": "-",
                           "msg": f"工具「{t['name']}」没有被任何技能引用（通用 agent 仍能用）"})

    counts = {lvl: sum(1 for i in issues if i["level"] == lvl)
              for lvl in ("error", "warn", "info")}
    return {"issues": issues, "counts": counts,
            "score": max(0, 100 - counts["error"] * ISSUE_WEIGHT["error"]
                         - counts["warn"] * ISSUE_WEIGHT["warn"]),
            "tools": len(tool_rows), "skills": len(skill_rows)}


def snapshot(tool_days: int = DEFAULT_DAYS, conn=None) -> dict:
    """面板一次请求只该走这一遍：技能目录、工具目录、体检。

    三者共用同一份 registry.load_all()、同一份工具名集合、**同一个数据库连接**。
    旧版是各算各的：overview 里 skill_entries 被调两次、tool_entries 被调两次，
    加上每次 get_conn() 在本机是 8ms 量级的固定开销（connect+pragma+close，
    WAL 下 close 还可能触发 checkpoint），7 个连接就是 50ms 的纯浪费。
    """
    own = conn is None
    conn = get_conn() if own else conn
    try:
        items = registry.load_all()
        tool_names = _tool_names()
        stats = tool_stats(tool_days, conn=conn)
        tools = tool_entries(tool_days, items=items, stats=stats)
        skills = skill_entries(tool_days, items=items, tool_names=tool_names, conn=conn)
        return {"tools": tools, "skills": skills,
                "lint": _lint_from(skills, tools), "days": tool_days,
                "index_builds": registry._index_builds()}
    finally:
        if own:
            conn.close()


def lint_all() -> dict:
    return snapshot()["lint"]


# ---------------------------------------------------------------- 路由试跑

def route_preview(query: str, hint: str | None = None) -> dict:
    """跑一次路由并解释结果——"为什么没选它"比"选了谁"更难查，也是最常需要回答的。"""
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")

    sk, board = registry.select(query, hint=hint)
    qg = registry._grams(query) - registry.STOP_GRAMS

    # 命中词元直接问倒排索引，不再为每个候选重建一遍 doc 的 gram 集合。
    post = registry._doc_index()
    detail = []
    for b in board:
        hits = [g for g in qg if b["name"] in post.get(g, ())]
        detail.append({**b, "hits": sorted(hits)[:8], "n_hits": len(hits)})

    if hint:
        if sk:
            reason = f"调用方显式指定了技能「{hint}」，跳过打分"
        elif any(b.get("disabled") for b in detail):
            reason = f"指定的技能「{hint}」已停用"
        elif any(b.get("broken") for b in detail):
            reason = (f"指定的技能「{hint}」不可用：组合技能缺段"
                      f"（子技能停用或不存在），跑一半比不跑更糟")
        else:
            reason = f"指定的技能「{hint}」不存在"
    elif len(qg) < registry.MIN_QUERY_GRAMS:
        reason = (f"有效词元只有 {len(qg)} 个（阈值 {registry.MIN_QUERY_GRAMS}）："
                  f"query 太短或全是功能词，不做路由")
    elif sk:
        top = detail[0] if detail else {"score": 0}
        second = detail[1]["score"] if len(detail) > 1 else 0
        reason = (f"「{sk.name}」得分 {top['score']:.3f} ≥ {registry.SELECT_MIN}，"
                  f"且领先第二名 {top['score'] - second:.3f}（阈值 {registry.SELECT_MARGIN}）")
    elif detail and detail[0].get("score", 0) < registry.SELECT_MIN:
        broken = [b for b in detail if b.get("broken")]
        if broken and len(broken) == len([b for b in detail if not b.get("disabled")]):
            reason = (f"唯一相关的「{broken[0]['name']}」不可用：组合技能缺段，"
                      f"缺段跑一半比不跑更糟，所以走通用 agent")
        else:
            reason = (f"最高分只有 {detail[0]['score']:.3f}，低于阈值 {registry.SELECT_MIN}："
                      f"没有一个技能的 when 说中了这件事，走通用 agent")
    elif len(detail) > 1 and detail[0]["score"] - detail[1]["score"] < registry.SELECT_MARGIN:
        reason = (f"「{detail[0]['name']}」与「{detail[1]['name']}」分差 "
                  f"{detail[0]['score'] - detail[1]['score']:.3f} < {registry.SELECT_MARGIN}："
                  f"判定为模糊，不套任何 SOP（套错会改写工具白名单）")
    else:
        reason = "没有技能命中，走通用 agent"

    return {"query": query, "tokens": sorted(qg)[:24], "n_tokens": len(qg),
            "picked": sk.name if sk else None,
            "picked_title": sk.title if sk else None,
            "reason": reason, "board": detail,
            "thresholds": {"min_score": registry.SELECT_MIN,
                           "margin": registry.SELECT_MARGIN,
                           "min_tokens": registry.MIN_QUERY_GRAMS}}


# ---------------------------------------------------------------- 受控编辑

def read_skill(name: str) -> dict:
    sk = registry.get(name)
    if sk is None:
        raise KeyError(name)
    path = registry.SKILLS_DIR / f"{sk.name}.md"
    text = path.read_text(encoding="utf-8")
    return {"name": sk.name, "path": sk.path, "text": text,
            "mtime": _mtime_str(path), "skill": sk.as_dict()}


def save_skill_text(name: str, text: str, *, message: str | None = None) -> dict:
    """保存技能正文。**先校验后落盘**，有一条 error 就整份拒绝。

    为什么不是"先存下来再提示"：技能是运行时的执行路径，存下一个工具名拼错的版本，
    下一次路由选中它就会静默失败，而且失败现场（模型没调工具）与病因（白名单空集）
    相距很远。宁可让保存失败，也不要让坏版本进目录。
    """
    name = (name or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise ValueError("技能名只能是 3~40 位小写字母/数字/下划线，且以字母开头")
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("内容不能为空")

    sk = registry.parse_text(text, path=f"skills/{name}.md")
    if sk is None:
        raise ValueError("frontmatter 解析失败：需要以 --- 包裹的 YAML，且包含 name 字段")
    if sk.name != name:
        raise ValueError(f"frontmatter 里的 name={sk.name} 与文件名 {name}.md 不一致")

    skills = dict(registry.load_all())
    skills[name] = sk
    issues = lint_skill(sk, skills=skills)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        return {"ok": False, "saved": False, "errors": errors, "issues": issues}

    path = registry.SKILLS_DIR / f"{name}.md"
    created = not path.exists()
    old = None if created else path.read_text(encoding="utf-8")
    if not created:
        _backup(path)
    _write_atomic(path, text)
    registry.load_all(force=True)
    rec = _commit_version(name, old, text, message=message, source="save")
    return {"ok": True, "saved": True, "created": created, "issues": issues,
            "version": (rec or {}).get("version"),
            "unchanged": rec is None,
            "skill": (registry.get(name).as_dict() if registry.get(name) else None)}


def set_enabled(name: str, enabled: bool) -> dict:
    """启停技能。走同一套校验：停用只写入 disabled 标记，不删文件、不丢内容。"""
    info = read_skill(name)
    try:
        text = registry.set_disabled_flag(info["text"], not enabled)
    except ValueError as e:
        raise ValueError(str(e)) from e
    # 停用是"想让它别跑"，不该因为文件里别的 error 就拒绝——那样会把人卡死。
    sk = registry.parse_text(text, path=info["path"])
    if sk is None:
        raise ValueError("修改后无法解析，已放弃（文件未改动）")
    path = registry.SKILLS_DIR / f"{sk.name}.md"
    old = info["text"]
    _backup(path)
    _write_atomic(path, text)
    registry.load_all(force=True)
    _commit_version(sk.name, old, text,
                    message="停用（不再参与路由）" if sk.disabled else "启用",
                    source="toggle")
    return {"ok": True, "name": sk.name, "enabled": not sk.disabled}


def backups(name: str) -> list[dict]:
    name = (name or "").strip()
    if not SKILL_NAME_RE.match(name) or not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob(f"{name}.*.md"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return [{"file": p.name, "size": p.stat().st_size, "mtime": _mtime_str(p)}
            for p in files[:20]]


_BACKUP_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}\.\d{8}-\d{6}(-\d{1,3})?\.md$")


def rollback(name: str, filename: str) -> dict:
    name = (name or "").strip()
    filename = (filename or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise ValueError("技能名不合法")
    if not _BACKUP_RE.match(filename) or not filename.startswith(f"{name}."):
        raise ValueError("备份文件名不合法")
    src = BACKUP_DIR / filename
    if not src.exists():
        raise KeyError(filename)
    text = src.read_text(encoding="utf-8")
    sk = registry.parse_text(text, path=f"skills/{name}.md")
    if sk is None or sk.name != name:
        raise ValueError("该备份与当前技能名不匹配，拒绝回滚")
    path = registry.SKILLS_DIR / f"{name}.md"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    _backup(path)          # 回滚前先备份当前版本：回滚本身也要能回滚
    _write_atomic(path, text)
    registry.load_all(force=True)
    _commit_version(name, old, text, message=f"从备份 {filename} 回滚", source="revert")
    return {"ok": True, "name": name, "restored_from": filename}


# ---------------------------------------------------------------- 技能版本
#
# 备份（skills/.backup/）与版本（skill_versions 表）解决的不是同一个问题：
# - 备份回答"文件坏了能恢复到几分钟前吗"，是兜底，按时间戳命名，只保证能回退；
# - 版本回答"when 这一行是哪次改的、为什么改、当时怎么想的"，要能 diff、能写说明。
# 只有备份时，"路由为什么突然选不中了"只能靠猜：时间戳撞了（同一秒两次保存）
# 甚至只剩一份，而两份文件之间到底差了什么，得人肉比。
#
# 版本记录的时机只有三个：保存、启停、回滚。三者都是"正文变了"。

MAX_DIFF_ROWS = 900
CONTEXT_LINES = 2


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise ValueError("技能名只能是 3~40 位小写字母/数字/下划线，且以字母开头")
    return name


def _latest_version(conn, name: str) -> dict | None:
    row = conn.execute(
        "SELECT version, sha FROM skill_versions WHERE skill=? ORDER BY version DESC LIMIT 1",
        (name,)).fetchone()
    return dict(row) if row else None


def _record_version(conn, name: str, text: str, *, message: str | None,
                    source: str) -> dict | None:
    """记一个版本。与最新版内容相同就不记。

    为什么按 sha 去重：启停开关会把 disabled 写进文件，内容确实变了，该记；
    但"保存了但一个字没改"不该记——否则历史会被无意义的空版本刷满，
    真正想看的那次改动被埋在中间。
    """
    text = (text or "").replace("\r\n", "\n")
    sha = _sha(text)
    last = _latest_version(conn, name)
    if last and last["sha"] == sha:
        return None
    ver = (last["version"] + 1) if last else 1
    conn.execute(
        "INSERT INTO skill_versions (skill,version,text,sha,message,source,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, ver, text, sha, (message or "").strip()[:200] or None, source, _now()))
    conn.commit()
    return {"version": ver, "sha": sha, "source": source,
            "message": (message or "").strip()[:200] or None, "created_at": _now()}


def _commit_version(name: str, old_text: str | None, new_text: str, *,
                    message: str | None, source: str) -> dict | None:
    """写入版本：先补 v1（如果表里还没有），再记这一次。

    v1 必须用**改之前**的正文：只在"第一次保存"时才补的话，看到的 v1 就已经是
    改后的内容，最需要对比的那一份反而丢了。
    """
    conn = get_conn()
    try:
        if old_text is not None and _latest_version(conn, name) is None:
            _record_version(conn, name, old_text, message="接入版本管理时的现状",
                            source="import")
        return _record_version(conn, name, new_text, message=message, source=source)
    finally:
        conn.close()


def versions(name: str) -> list[dict]:
    """版本列表，不含正文——30 个版本 × 2.6KB 没必要每次翻历史都传一遍。"""
    name = _check_name(name)
    path = registry.SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise KeyError(name)
    conn = get_conn()
    try:
        if _latest_version(conn, name) is None:
            _record_version(conn, name, path.read_text(encoding="utf-8"),
                            message="接入版本管理时的现状", source="import")
        rows = conn.execute(
            "SELECT version, sha, message, source, created_at, length(text) AS chars "
            "FROM skill_versions WHERE skill=? ORDER BY version DESC LIMIT 60",
            (name,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def ensure_baseline_versions() -> dict:
    """给还没进版本管理的技能补一个 v1（内容是当前磁盘状态）。

    为什么要在启动时做：技能运行台账要回答"跑的是哪一版"，没有版本号就只能写 NULL，
    于是面板上「跑过 15 次」和版本历史完全对不上——改了哪一版变差也就查不出来。
    实测 5 个技能里只有 2 个有版本记录，剩下 3 个跑了 10 次全是无主记录。

    source='import' 表示这是接入时的现状，不是一次真正的改动；幂等，只在
    该技能一条版本都没有时才写。
    """
    items = registry.load_all()
    created = []
    conn = get_conn()
    try:
        for name, sk in items.items():
            if _latest_version(conn, name) is not None:
                continue
            p = Path(sk.path)
            if not p.is_absolute():
                p = BASE_DIR / p       # sk.path 是相对 BASE_DIR 的字符串
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue          # 文件读不了不影响启动，下次再说
            v = _record_version(conn, name, text, message="接入版本管理时的现状",
                                source="import")
            if v:
                created.append({"skill": name, **v})
    finally:
        conn.close()
    return {"created": created, "count": len(created)}


def version_board(name: str) -> dict:
    """版本列表 + 每个版本的实际运行表现 + 一个诚实的标记：磁盘内容是否已在历史里。

    dirty=True 说明文件是被平台外改的（手改 md、git 切分支、脚本生成）。
    不说出来的话，用户会以为"历史里有最新的那份"，而 diff 里永远少一段改动——
    这比没有历史更误导。

    为什么要把 skill_runs 挂到版本上：版本历史单独看只能回答"改了什么"，
    回答不了"改得对不对"。跑一次算一次账之后，才能看出 v2 比 v1 慢了 3 秒、
    贵了 4 分钱、成功率掉了 20%——那才是决定回不回滚的依据。
    unversioned_runs 单独返回：版本管理接上之前的运行记录 skill_version 为空，
    如果只按版本聚合，它们会凭空消失，让人以为"这个技能根本没跑过几次"。
    """
    vs = versions(name)
    perf = {r["version"]: r for r in registry.version_stats(name)}
    for v in vs:
        p = perf.get(v["version"]) or {}
        v["runs"] = p.get("runs", 0)
        v["ok_runs"] = p.get("ok_runs", 0)
        v["ok_rate"] = (round(p["ok_runs"] / p["runs"], 3)
                        if p.get("runs") else None)
        v["avg_ms"] = round(p["avg_ms"]) if p.get("avg_ms") else None
        v["cost_cny"] = p.get("cost_cny") or 0
        v["last_at"] = p.get("last_at")
    unv = perf.get(None) or {}
    path = registry.SKILLS_DIR / f"{name}.md"
    cur = _sha(path.read_text(encoding="utf-8")) if path.exists() else None
    latest = vs[0]["sha"] if vs else None
    return {"versions": vs, "current_sha": cur,
            "dirty": bool(cur and latest and cur != latest),
            "unversioned_runs": unv.get("runs", 0),
            "unversioned_cost_cny": unv.get("cost_cny") or 0}


def _read_version(conn, name: str, ver: int) -> dict | None:
    row = conn.execute(
        "SELECT version, text, sha, message, source, created_at FROM skill_versions "
        "WHERE skill=? AND version=?", (name, ver)).fetchone()
    return dict(row) if row else None


def diff_versions(name: str, a: int, b: int) -> dict:
    """两个版本的行级差异。

    返回结构化行而不是统一 diff 字符串：前端要按左右行号渲染成红绿块，
    字符串还得再解析一遍，而且解析错了没人会发现（看起来像 diff，其实错位）。
    """
    name = _check_name(name)
    a, b = int(a), int(b)
    conn = get_conn()
    try:
        va = _read_version(conn, name, a)
        vb = _read_version(conn, name, b)
    finally:
        conn.close()
    if va is None:
        raise KeyError(f"版本不存在: v{a}")
    if vb is None:
        raise KeyError(f"版本不存在: v{b}")

    al, bl = va["text"].split("\n"), vb["text"].split("\n")
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    rows: list[dict] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            n = i2 - i1
            if n > CONTEXT_LINES * 2 + 1:
                # 折叠中间的长相同段：差异被大段正文挤到看不见时，diff 等于没有
                for k in range(CONTEXT_LINES):
                    rows.append({"op": "equal", "a": i1 + k + 1, "b": j1 + k + 1,
                                 "text": al[i1 + k]})
                rows.append({"op": "skip", "n": n - CONTEXT_LINES * 2})
                for k in range(n - CONTEXT_LINES, n):
                    rows.append({"op": "equal", "a": i1 + k + 1, "b": j1 + k + 1,
                                 "text": al[i1 + k]})
            else:
                for k in range(n):
                    rows.append({"op": "equal", "a": i1 + k + 1, "b": j1 + k + 1,
                                 "text": al[i1 + k]})
        else:
            # replace 拆成"先删后加"：两栏渲染时一行只承担一种语义，不用猜
            for k in range(i1, i2):
                rows.append({"op": "del", "a": k + 1, "b": None, "text": al[k]})
                removed += 1
            for k in range(j1, j2):
                rows.append({"op": "add", "a": None, "b": k + 1, "text": bl[k]})
                added += 1

    meta = lambda v: {"version": v["version"], "message": v["message"],
                      "source": v["source"], "created_at": v["created_at"]}
    return {"skill": name, "a": meta(va), "b": meta(vb),
            "rows": rows[:MAX_DIFF_ROWS], "truncated": len(rows) > MAX_DIFF_ROWS,
            "stats": {"added": added, "removed": removed}}


def revert_to(name: str, version: int, message: str | None = None) -> dict:
    """把某个历史版本恢复成当前正文。**回滚也是一个新版本**，不是"退回去"。

    为什么不直接把指针移回去：文件是唯一事实源。覆盖文件之后必须再记一版，
    否则历史会分叉——面板显示 v5，文件实际是 v2 的内容，下一次保存的父版本就错了。
    """
    name = _check_name(name)
    version = int(version)
    conn = get_conn()
    try:
        row = _read_version(conn, name, version)
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"版本不存在: v{version}")
    text = row["text"]
    sk = registry.parse_text(text, path=f"skills/{name}.md")
    if sk is None or sk.name != name:
        raise ValueError("该版本与该技能名不匹配（可能是改名前的版本），拒绝回滚")

    path = registry.SKILLS_DIR / f"{name}.md"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    _backup(path)          # 文件级备份保留：它是"绕过平台直接改坏了"时的兜底
    _write_atomic(path, text)
    registry.load_all(force=True)
    rec = _commit_version(name, old, text,
                          message=message or f"回滚到 v{version}", source="revert")
    return {"ok": True, "name": name, "restored_from": version,
            "version": (rec or {}).get("version"), "text": text}


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _backup(path: Path) -> None:
    if not path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"{path.stem}.{ts}.md"
    if dst.exists():
        dst = BACKUP_DIR / f"{path.stem}.{ts}-{int(time.time() * 1000) % 1000}.md"
    shutil.copy2(path, dst)


# ---------------------------------------------------------------- 概览与调用记录

def overview(days: int = 1, *, snap: dict | None = None, conn=None) -> dict:
    own = conn is None
    conn = get_conn() if own else conn
    try:
        snap = snapshot(conn=conn) if snap is None else snap
        entries, tools, lint = snap["skills"], snap["tools"], snap["lint"]
        since = _since(days)
        row = conn.execute(
            "SELECT COUNT(*) AS calls, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
            "AVG(CASE WHEN cached=0 AND latency_ms>0 THEN latency_ms END) AS avg_ms "
            "FROM tool_calls WHERE ts >= ?", (since,)).fetchone()
    finally:
        if own:
            conn.close()
    calls = int(row["calls"] or 0)
    errors = int(row["errors"] or 0)

    from ..agents.orchestrator import ROLE_LABEL, ROLE_TOOLS  # 局部导入：避免拉起整条 agent 链
    return {
        "tools": {"total": len(tools),
                  "enabled": sum(1 for t in tools if t.get("enabled", True)),
                  "read_only": sum(1 for t in tools if t["read_only"]),
                  "write": sum(1 for t in tools if not t["read_only"]),
                  "orphan": sum(1 for t in tools if not t["used_by"]),
                  "disabled": sum(1 for t in tools if not t.get("enabled", True)),
                  "no_impl": sum(1 for t in tools if not t["impl"])},
        "skills": {"total": len(entries),
                   "enabled": sum(1 for e in entries if not e["disabled"]),
                   "disabled": sum(1 for e in entries if e["disabled"]),
                   "composite": sum(1 for e in entries if e.get("steps")),
                   "never_run": sum(1 for e in entries if not e["stats"]["runs"])},
        "window": {"days": days, "calls": calls, "errors": errors,
                   "error_rate": round(errors / calls, 4) if calls else 0.0,
                   "avg_ms": int(row["avg_ms"] or 0)},
        "health": {"score": lint["score"], "counts": lint["counts"]},
        "roles": [{"role": r, "label": ROLE_LABEL.get(r, r), "tools": list(ts)}
                  for r, ts in ROLE_TOOLS.items()],
        "thresholds": {"select_min": registry.SELECT_MIN,
                       "select_margin": registry.SELECT_MARGIN,
                       "min_tokens": registry.MIN_QUERY_GRAMS,
                       "max_segments": registry.MAX_SEGMENTS},
    }


def board(days: int = 1, *, call_limit: int = 30,
          tool_days: int = DEFAULT_DAYS) -> dict:
    """面板一次请求拿全部数据：整页只开一个连接、只构建一份快照。

    前端原来并发 5 个接口，每个都在服务端重算同一份技能/工具快照，
    而本机每次 get_conn() 是 8ms 量级的固定开销（connect+pragma+close），
    7 个连接就是 50ms 的纯浪费——比技能解析本身还贵。
    """
    conn = get_conn()
    try:
        snap = snapshot(tool_days, conn=conn)
        return {"overview": overview(days, snap=snap, conn=conn),
                "tools": snap["tools"], "skills": snap["skills"], "lint": snap["lint"],
                "calls": recent_calls(call_limit, conn=conn),
                "daily": daily_calls(tool_days, conn=conn),
                "days": tool_days, "index_builds": snap["index_builds"]}
    finally:
        conn.close()


def recent_calls(limit: int = 50, tool: str | None = None,
                 status: str | None = None, *, conn=None) -> list[dict]:
    limit = max(1, min(int(limit or 50), 300))
    where, args = [], []
    if tool:
        where.append("tool=?")
        args.append(tool)
    if status in ("ok", "error"):
        where.append("status=?")
        args.append(status)
    sql = ("SELECT id, ts, tool, source, skill, session_id, turn, read_only, status, "
           "error_kind, error, latency_ms, result_chars, retried, cached "
           "FROM tool_calls")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    own = conn is None
    conn = get_conn() if own else conn
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        if own:
            conn.close()


def daily_calls(days: int = DEFAULT_DAYS, conn=None) -> list[dict]:
    own = conn is None
    conn = get_conn() if own else conn
    try:
        rows = conn.execute(
            "SELECT substr(ts,1,10) AS day, COUNT(*) AS calls, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors "
            "FROM tool_calls WHERE ts >= ? GROUP BY day ORDER BY day", (_since(days),)).fetchall()
    finally:
        if own:
            conn.close()
    return [dict(r) for r in rows]


def _since(days: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(time.time() - max(1, int(days)) * 86400))


def _mtime_str(p: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    except OSError:
        return ""
