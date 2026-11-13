"""技能注册表：加载 skills/*.md、按语义选技能、渲染成注入文本。

选择策略刻意保守：
- 词面覆盖率低于 0.10 不选（宁可走通用 agent，也不要错误地套一个 SOP）；
- 第一名与第二名分差小于 0.03 不选（两个技能都"像是"要它时，说明用户没说清，
  套错 SOP 比不套更糟——它会限制工具白名单，把该做的事挡在外面）。

这条规则来自一个真实取舍：技能越强，误选代价越大。工具用错了只是浪费一次调用；
技能选错了会改变整个执行路径，还不容易发现。

**组合技能（嵌套）**

一个 skills/*.md 可以用 `steps:` 声明自己不写 SOP，只引用别的技能：

    steps:
      - skill: weakness_diagnosis      # 只读，定位薄弱点
        goal: 找出最该补的两个主题
      - skill: gap_to_cards            # 写，把主题变成待审卡
        require_tools: [draft_cards]   # 覆盖子技能的验收契约

为什么不是"技能 A 的 SOP 里写一句调用技能 B"：
模型在单轮里自己切 SOP 时，工具白名单是静态的——A 的白名单里没有 B 的工具，
要么报错，要么放开全局白名单（等于不要白名单）。由代码按段串行执行、
每段重新收窄白名单，才能真正做到"第二段能写、第一段只能读"。

为什么只允许一层：两层的段数是指数增长（3×3=9 次 LLM 循环），
而且注入时讲不清"现在在完成哪一段的哪一段"。真需要三层就该上编排。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..core.config import BASE_DIR

SKILLS_DIR = BASE_DIR / "skills"
SELECT_MIN = 0.10
SELECT_MARGIN = 0.03
MAX_BODY_CHARS = 2600
MIN_QUERY_GRAMS = 4       # 有效 2-gram 少于 4 个说明 query 太短/太泛，不路由
MAX_SEGMENTS = 3          # 组合技能最多几段：每段是一轮完整 agent 循环，3 段已是成本上限

# 停用 2-gram：中文里"怎么/什么/今天"这类高频功能词会让任意 query 与任意 SOP 假命中。
# 第一版没有这张表，"今天天气怎么样" 就因为共享 "怎么" 选中了薄弱点诊断——
# 技能选错的代价是工具白名单被改写，比不选严重得多。
STOP_GRAMS = {
    "怎么", "什么", "么样", "怎样", "如何", "哪个", "哪里", "哪些", "为什", "是不", "不是",
    "今天", "明天", "现在", "一下", "帮我", "可以", "我的", "我是", "这个", "那个",
    "能不", "不能", "有没", "没有", "多少", "一些", "需要", "应该", "觉得", "知道",
    "然后", "就是", "还是", "或者", "但是", "因为", "所以", "如果", "已经", "正在",
}

_FRONT = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)$", re.S)
_PUNCT = re.compile(r"[\s\u3000·・,，.。;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_=+*#~`-]+")


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    when: str
    body: str
    tools: tuple[str, ...] = ()
    require_tools: tuple[str, ...] = ()
    risk: str = "read"
    output: str = ""
    max_iters: int = 8
    path: str = ""
    steps: tuple[dict, ...] = ()      # 组合技能：子技能序列（只允许一层）
    disabled: bool = False            # 停用：文件保留、可编辑，但路由与展开都跳过
    # 强信号关键词：中文短句里 2-gram 覆盖率天然很低（"复习"只有一个 gram），
    # 靠覆盖率永远选不中——而"复习"恰恰是最高频的意图。命中等价于用户明说了，
    # 所以给一个越过 MIN_QUERY_GRAMS 门槛的加分，见 _ranked。
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        d = {"name": self.name, "title": self.title, "when": self.when,
             "tools": list(self.tools), "require_tools": list(self.require_tools),
             "risk": self.risk, "output": self.output, "max_iters": self.max_iters,
             "disabled": self.disabled, "aliases": list(self.aliases)}
        if self.steps:
            d["steps"] = [dict(s) for s in self.steps]
        return d


@dataclass(frozen=True)
class Segment:
    """组合技能展开后的一段。"""
    skill: Skill
    goal: str = ""
    require_tools: tuple[str, ...] | None = None   # None = 用子技能自己的契约


def expand(sk: Skill, *, skills: dict[str, Skill] | None = None) -> list[Segment]:
    """把技能展开成有序段落。普通技能 = 单段（自身）。

    展开失败（子技能不存在/子技能本身是组合/子技能已停用）时跳过该段；
    一段都不剩就退回自身单段，让它在通用 agent 模式下跑，比直接报错好——
    用户至少还能拿到答案。

    skills 可传入已加载的目录：面板一次请求要展开全部技能，逐个 get() 会重复
    做目录 stat（每次 ~5 个文件 × N 个技能），传入后就只 stat 一次。
    """
    if not sk.steps:
        return [Segment(skill=sk)]
    segs: list[Segment] = []
    for raw in sk.steps[:MAX_SEGMENTS]:
        if not isinstance(raw, dict):
            continue
        cname = str(raw.get("skill") or "").strip()
        child = skills.get(cname) if skills is not None else get(cname)
        if child is None or child.steps or child.disabled:
            continue
        rt = raw.get("require_tools")
        segs.append(Segment(
            skill=child,
            goal=str(raw.get("goal") or "").strip(),
            require_tools=tuple(str(t) for t in rt) if isinstance(rt, list) else None))
    return segs or [Segment(skill=sk)]


_cache: dict = {"stamp": None, "items": {}}


def _stamp() -> tuple:
    """目录指纹：路径 + 每个文件的名字与 mtime。

    带上目录路径是因为测试会把 SKILLS_DIR 换到临时目录：只比文件名+mtime 时，
    两个临时目录里的同名同秒文件会被误判成"没变"，于是路由用了上一个测试的索引。
    """
    if not SKILLS_DIR.exists():
        return (str(SKILLS_DIR),)
    return (str(SKILLS_DIR),) + tuple(
        sorted((p.name, p.stat().st_mtime_ns) for p in SKILLS_DIR.glob("*.md")))


def _as_list(v) -> list:
    """frontmatter 列表字段兼容两种写法：YAML 数组，或逗号/顿号分隔的字符串。

    写技能文件的人不会总记得加方括号，为一个写法差异让整条规则被静默忽略，
    等于埋一个"改了没生效"的坑。
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [s for s in re.split(r"[,，、;；\s]+", str(v)) if s]


def parse_text(text: str, *, path: str = "") -> Skill | None:
    """从内存文本解析技能。保存前用它校验，不必先写临时文件。

    返回 None 而不是抛异常：调用方（管理台）要把"哪里不合法"作为一条问题展示，
    异常栈对改 md 的人没有意义。
    """
    m = _FRONT.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict) or not meta.get("name"):
        return None
    raw_steps = meta.get("steps") or []
    if not isinstance(raw_steps, list):
        raw_steps = []
    return Skill(
        name=str(meta["name"]).strip(),
        title=str(meta.get("title") or meta["name"]).strip(),
        when=str(meta.get("when") or "").strip(),
        body=m.group(2).strip()[:MAX_BODY_CHARS],
        tools=tuple(str(t) for t in (meta.get("tools") or [])),
        require_tools=tuple(str(t) for t in (meta.get("require_tools") or [])),
        risk=str(meta.get("risk") or "read"),
        output=str(meta.get("output") or "").strip(),
        max_iters=int(meta.get("max_iters") or 8),
        path=path,
        steps=tuple(s for s in raw_steps if isinstance(s, dict)),
        disabled=bool(meta.get("disabled")),
        aliases=tuple(str(a).strip() for a in _as_list(meta.get("aliases")) if str(a).strip()),
    )


def _rel(path: Path) -> str:
    """相对项目根的路径，用于展示。目录可配置（测试会换到临时目录）时必须容错。"""
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def _parse(path: Path) -> Skill | None:
    return parse_text(path.read_text(encoding="utf-8"), path=_rel(path))


_DISABLED_LINE = re.compile(r"^disabled:\s*(?:true|yes|1|false|no|0)\s*$\n?", re.M)


def set_disabled_flag(text: str, disabled: bool) -> str:
    """文本级改开关，不走 yaml.dump。

    dump 会重排字段、丢注释、把长中文 when 折成多行——技能文件是人读的事实源，
    为改一个布尔值把整份重写一遍，diff 会大到没法 review。
    """
    m = _FRONT.match(text)
    if not m:
        raise ValueError("文件缺少 frontmatter")
    head, body = m.group(1), m.group(2)
    if _DISABLED_LINE.search(head):
        head = _DISABLED_LINE.sub("disabled: true\n" if disabled else "", head)
    elif disabled:
        head = head.rstrip("\n") + "\ndisabled: true"
    head = re.sub(r"\n{3,}", "\n\n", head).strip("\n")
    return f"---\n{head}\n---\n{body}"


def load_all(force: bool = False) -> dict[str, Skill]:
    """按 mtime 缓存。改 md 立即生效，不用重启——技能是干活时最常调的东西。"""
    stamp = _stamp()
    if not force and _cache["stamp"] == stamp:
        return _cache["items"]
    items: dict[str, Skill] = {}
    for p in sorted(SKILLS_DIR.glob("*.md")) if SKILLS_DIR.exists() else []:
        sk = _parse(p)
        if sk:
            items[sk.name] = sk
    _cache.update(stamp=stamp, items=items)
    if force:
        # 强制重载说明"文件内容被主动改写了"，倒排索引必须跟着失效。
        # 只靠 stamp 不够：保存后 mtime 变了没问题，但测试会在同一纳秒粒度内改文件。
        _index["stamp"] = None
    return items


def get(name: str) -> Skill | None:
    return load_all().get((name or "").strip())


def catalog() -> list[dict]:
    return [s.as_dict() for s in load_all().values()]


def _grams(text: str, n: int = 2) -> set[str]:
    t = _PUNCT.sub("", (text or "").lower())
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _doc_text(sk: Skill) -> str:
    """参与路由打分的技能文字。顺序无关，但拼法必须与旧版一致，
    否则分数会静默变化（换序不影响集合，这里只是保持可读性）。"""
    return f"{sk.title}{sk.when}{sk.name}"


# ---------------------------------------------------------------- 倒排索引
#
# 旧实现每轮对话对每个技能重建一次 doc 的 2-gram 集合：N 个技能 × 每轮一次，
# 5 个技能 1.6ms 看不出问题，50 个技能就是 16ms，而且这个成本落在**每一轮对话**
# 的入口上。改成 gram → 技能名 的倒排表：查询时只遍历 query 自己的词元，
# 命中谁就给谁 +1，复杂度从 O(N×|doc|) 降到 O(|query|×平均倒排长度)。
#
# 失效条件跟 load_all 一致（技能文件 mtime 变了就重建），所以停用/改 when
# 立刻反映到路由上，不需要重启，也不会有"面板改了但路由还按旧 when 走"。

_index: dict = {"stamp": None, "post": {}, "built": 0}


def _index_builds() -> int:
    """索引重建次数。测试用它断言"跨请求复用"，比时间断言稳定。"""
    return _index["built"]


def _doc_index() -> dict[str, set[str]]:
    skills = load_all()          # 顺带把 _cache["stamp"] 刷新到当前值，省一次目录 stat
    stamp = _cache["stamp"]
    if _index["stamp"] != stamp:
        post: dict[str, set[str]] = {}
        for sk in skills.values():
            for g in _grams(_doc_text(sk)):
                post.setdefault(g, set()).add(sk.name)
        _index.update(stamp=stamp, post=post, built=_index["built"] + 1)
    return _index["post"]


def _ranked(query: str, skills: dict[str, Skill]) -> list[tuple[float, Skill]]:
    """按分数降序返回全部可参与路由的技能（含 0 分）。

    0 分也要返回：面板的"为什么没选"需要看到所有候选，只在内部排序里被压到末尾。

    三档信号，强弱分明：
    1. **别名命中**（+0.45）：用户在句子里写了这个词，等于明说"我要这个技能"。
       这一档要能越过 MIN_QUERY_GRAMS——"复习"只有 1 个有效 gram，
       按覆盖率算永远是 0，而它恰是最高频的意图。
    2. **技能名整体出现**（+0.3）：英文技能名不会出现在中文句子里，
       这档主要是给英文提问和显式指名的场景兜底。
    3. **2-gram 覆盖率**（0~1）：主体信号，短句时不可靠所以有门槛。
    """
    cands = [sk for sk in skills.values() if not sk.disabled and runnable(sk, skills=skills)]
    qg = _grams(query) - STOP_GRAMS
    flat = _PUNCT.sub("", query.lower())
    short = len(qg) < MIN_QUERY_GRAMS

    counts: dict[str, int] = {}
    if not short:
        post = _doc_index()
        for g in qg:
            for name in post.get(g, ()):
                counts[name] = counts.get(name, 0) + 1

    out = []
    for sk in cands:
        base = 0.0 if short else counts.get(sk.name, 0) / len(qg)
        name_hit = 0.3 if sk.name.replace("_", "") in flat else 0.0
        alias_hit = 0.45 if any(a in flat for a in sk.aliases) else 0.0
        out.append((round(base + name_hit + alias_hit, 4), sk))
    out.sort(key=lambda x: -x[0])
    return out


def runnable(sk: Skill, *, skills: dict[str, Skill] | None = None) -> bool:
    """组合技能声明了几段，就得能展开几段。

    缺段（子技能被停用/改名/删掉）时跑一半比不跑更糟：用户看到的是"组合技能已执行"，
    实际只做了诊断、没补卡，而答案不会自己承认漏了一半。所以缺段就不选它，
    由体检报 E_STEP_DISABLED / E_STEP_UNKNOWN 让人去修。
    """
    if not sk.steps:
        return True
    return len(expand(sk, skills=skills)) >= min(len(sk.steps), MAX_SEGMENTS)


def select(query: str, *, hint: str | None = None) -> tuple[Skill | None, list[dict]]:
    """返回 (选中的技能 or None, 候选打分表)。hint 是用户/调用方显式指定的技能名，优先。

    停用的技能不参与打分，也不接受 hint——停用的语义就是"别再用它"。
    但候选表里仍然列出它并标 disabled，否则面板上"为什么没选"会变成"它不见了"。
    """
    skills = load_all()
    if hint:
        sk = skills.get(hint)
        if sk and sk.disabled:
            return None, [{"name": sk.name, "title": sk.title, "score": 0.0,
                           "disabled": True}]
        if sk and not runnable(sk, skills=skills):
            return None, [{"name": sk.name, "title": sk.title, "score": 0.0,
                           "broken": True}]
        if sk:
            return sk, [{"name": sk.name, "score": 1.0, "picked": True}]
    scored = _ranked(query, skills)
    board = [{"name": sk.name, "title": sk.title, "score": s} for s, sk in scored]
    board += [{"name": sk.name, "title": sk.title, "score": 0.0, "disabled": True}
              for sk in skills.values() if sk.disabled]
    board += [{"name": sk.name, "title": sk.title, "score": 0.0, "broken": True}
              for sk in skills.values() if not sk.disabled and not runnable(sk, skills=skills)]
    if not scored or scored[0][0] < SELECT_MIN:
        return None, board
    if len(scored) > 1 and scored[0][0] - scored[1][0] < SELECT_MARGIN:
        # 模糊：两个都沾边。不选，让通用 agent 自己判断，避免用一个 SOP 关掉别的工具
        for b in board[:2]:
            b["tie"] = True
        return None, board
    return scored[0][1], board


def describe(sk: Skill, *, position: tuple[int, int] | None = None,
             goal: str = "") -> str:
    """渲染成注入 system 的一段。工具白名单写成"仅限"，因为它是硬约束不是建议。

    position/goal 只在组合技能分段执行时给：模型必须知道"这只是第 1 段，
    后面还有一段会接手"，否则它会把整件事都做完（第一段就调写工具），
    或者把"本段结论"写成给用户的最终答复。
    """
    title = f"【当前技能：{sk.title}（{sk.name}）】"
    if position and position[1] > 1:
        i, n = position
        title = f"【当前技能：{sk.title}（{sk.name}）｜整个任务的第 {i}/{n} 段】"
    parts = [title]
    if sk.body:
        parts.append(sk.body)
    if goal:
        parts.append("本段要交付：" + goal)
    if position and position[1] > 1:
        tail = "还有后续段落会接着做" if position[0] < position[1] else "这是最后一段，要把结果讲给用户"
        parts.append(f"只做本段的事，做完就收尾（{tail}）。")
    if sk.tools:
        parts.append("本技能允许使用的工具（其他工具本轮不可用）：" + "、".join(sk.tools))
    if sk.output:
        parts.append("验收标准：" + sk.output)
    return "\n\n".join(parts)


def allowed_tools(sk: Skill | None, fallback: list[str]) -> list[str]:
    """技能的工具白名单；技能没写 tools 就放开全部，不要因为漏写字段把模型锁死。

    组合技能自己不执行（展开后逐段跑），它身上的 tools 字段没有意义，
    这里按"没写"处理，避免把调用方的白名单意外收空。
    """
    if sk is None or sk.steps or not sk.tools:
        return list(fallback)
    keep = set(sk.tools)
    return [t for t in fallback if t in keep]


def check(sk: Skill, tools_used: list[str], *, memory_hits: int = 0,
          require_tools: tuple[str, ...] | None = None) -> dict:
    """事后验收：必需工具是否真的调过。

    例外：本轮从长期记忆里召回了相关内容（memory_hits > 0）时，缺工具不算失败。
    依据是"技能要求的是把事做完，不是必须调某个工具"——如果模型靠上一轮的结论
    直接答对了，它比重新查一遍更高效。实测里"先复习哪几张卡"就属于这种：
    第 2 轮没调任何工具，但依据来自第 1 轮抽取的记忆，答案依然准确。

    require_tools 显式传入时覆盖技能自带契约：组合技能的某一段可能走的是
    另一条交付路径（例如"诊断"技能要求查简报，但作为第 1 段时只需要给出结论）。
    """
    need = sk.require_tools if require_tools is None else require_tools
    used = list(dict.fromkeys(tools_used))
    missing = [t for t in need if t not in used]
    via_memory = bool(missing) and memory_hits > 0
    return {"ok": not missing or via_memory, "missing": [] if via_memory else missing,
            "used": used, "via_memory": via_memory}


def record(skill: str, *, session_id: str | None, status: str, steps: int,
           tools_used: list[str], tokens: int = 0, duration_ms: int = 0,
           error: str | None = None, version: int | None = None,
           cost_cny: float = 0.0) -> None:
    """落 skill_runs。失败不影响主流程——它是统计，不是前置条件。

    version 不传时取该技能当前的最高版本：调用方（对话链路）手里只有技能对象，
    没有版本号，而为每条记录都去查一次版本比"在写台账时查"更啰嗦。技能没进
    版本管理时保持 NULL——编一个 0 会让人以为存在"第 0 版"，统计里就会多出一条
    含义不明的版本线。
    """
    import datetime
    from ..core.db import get_conn
    try:
        conn = get_conn()
        try:
            if version is None:
                row = conn.execute("SELECT MAX(version) FROM skill_versions WHERE skill=?",
                                   (skill,)).fetchone()
                version = row[0] if row else None
            conn.execute(
                "INSERT INTO skill_runs (skill,session_id,status,steps_done,tools_used,tokens,"
                "duration_ms,error,skill_version,cost_cny,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (skill, session_id, status, steps, ",".join(tools_used)[:400], tokens,
                 duration_ms, (error or "")[:300] or None, version,
                 round(float(cost_cny or 0.0), 6),
                 datetime.datetime.now().isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def stats(conn=None) -> list[dict]:
    """技能运行统计。conn 可传入，让面板一次请求复用一个连接（见 harness_admin.snapshot）。"""
    from ..core.db import get_conn
    own = conn is None
    conn = get_conn() if own else conn
    try:
        return [dict(r) for r in conn.execute(
            "SELECT skill, COUNT(*) AS runs, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs, "
            "AVG(duration_ms) AS avg_ms, SUM(tokens) AS tokens, "
            "ROUND(SUM(cost_cny), 4) AS cost_cny, "
            "AVG(NULLIF(cost_cny, 0)) AS avg_cost "
            "FROM skill_runs GROUP BY skill ORDER BY runs DESC")]
    finally:
        if own:
            conn.close()


def version_stats(skill: str, conn=None) -> list[dict]:
    """某技能按版本分组的运行表现。

    没有它，"这个技能改过 3 版"只是一句事实；有了它才能回答"v3 比 v2 贵了 40%、
    成功率还降了"——这是决定要不要回滚的唯一依据。
    skill_version 为 NULL 的行归到"未登记版本"，不丢——那是接入版本管理之前的历史，
    丢掉会让"总运行次数"和分版本之和差一截。
    """
    from ..core.db import get_conn
    own = conn is None
    conn = get_conn() if own else conn
    try:
        rows = conn.execute(
            "SELECT skill_version AS version, COUNT(*) AS runs, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs, "
            "AVG(duration_ms) AS avg_ms, SUM(tokens) AS tokens, "
            "ROUND(SUM(cost_cny), 4) AS cost_cny, "
            "MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM skill_runs WHERE skill=? GROUP BY skill_version "
            "ORDER BY skill_version IS NULL, skill_version", (skill,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()

