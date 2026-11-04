"""滑动窗口 + 关键轮次锚定。

纯滑窗的老问题：越早的内容越容易被当成"不重要"，但面试/工程对话里恰恰相反——
第一句往往就是**约束**（"按我们公司 2C2G 的机器来""别引外部组件"）。
把约束滑掉之后，模型后面说的话全是错的，而且从上下文里看不出错了。

规则：**最近 N 轮必留 + 更早的轮次按关键度分级锚定**。
关键不靠模型判断（每轮都判一次太贵且不稳），靠可验证的字面特征：

| 级别 | 特征 | 压力下的处置 |
|---|---|---|
| P0 约束 | 必须/不要/记住/统一用/别/禁止… | 最近 4 条**永不丢**；更早的降级为 P1 |
| P1 引用 | 业务 id（q-/kb-/fi-…）、评分结论（得分/均分/N 分） | 预算不够时从最旧的开始丢 |

为什么要给 P0 设"最多 4 条"而不是无限：**保护也要有上界**，否则一段"每句都是要求"
的对话会让保护逻辑把预算吃满，等于没有预算。第 5 条及更早的约束降级成 P1，
这时它和普通轮次公平竞争——这是刻意的取舍，写在这里免得以后被当成 bug 修掉。
"""
import re
from dataclasses import dataclass, field

from .token_budget import count_messages

# (正则, 理由, 优先级)  0 = P0 约束，1 = P1 引用
KEY_PATTERNS: list[tuple[str, str, int]] = [
    (r"(?:必须|不要|不许|别|记住|统一用|只用|只能|禁止|务必)", "用户约束", 0),
    (r"\b(?:q|kb|fi|ing|act|plan|mock|cs)-[0-9a-zA-Z][0-9a-zA-Z-]{3,}", "业务 id", 1),
    (r"(?:得分|均分|评分|预测分|\d+(?:\.\d+)?\s*分)", "评分结论", 1),
]

MAX_P0 = 4          # 最近这么多条"约束轮"永不丢


@dataclass
class Selection:
    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    anchored: int = 0
    reasons: list[str] = field(default_factory=list)
    tokens: int = 0
    dropped_tokens: int = 0
    force_dropped: int = 0            # 预算不够、连锚定轮也被挤掉的次数（降级信号）
    protected: int = 0                # 实际受 P0 保护的轮数

    def as_dict(self) -> dict:
        return {"kept_msgs": len(self.kept), "dropped_msgs": len(self.dropped),
                "anchored": self.anchored, "protected": self.protected,
                "tokens": self.tokens, "dropped_tokens": self.dropped_tokens,
                "force_dropped": self.force_dropped, "reasons": self.reasons[:10]}


def key_hits(msg: dict) -> list[tuple[str, int]]:
    """这条消息命中了哪些锚定特征：[(理由, 优先级)]，空列表 = 不锚定。"""
    if msg.get("role") not in ("user", "assistant"):
        return []
    text = msg.get("content") or ""
    if not text.strip():
        return []
    return [(why, pri) for pat, why, pri in KEY_PATTERNS if re.search(pat, text)]


def key_reasons(msg: dict) -> list[str]:
    return [why for why, _ in key_hits(msg)]


def is_constraint(msg: dict) -> bool:
    """是否是 P0（用户约束）。assembler 丢最旧轮次时会跳过这类。"""
    return any(pri == 0 for _, pri in key_hits(msg))


def group_turns(messages: list[dict]) -> list[list[dict]]:
    """按 user 消息切轮；开头若没有 user（如 system 残留）自成一组。"""
    turns: list[list[dict]] = []
    for m in messages:
        if m.get("role") == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return turns


def select(messages: list[dict], budget_tokens: int, recent_turns: int = 6) -> Selection:
    """挑出要发给模型的历史消息。

    优先级：最近 N 轮 > P0 约束（最近 MAX_P0 条）> P1 引用 > 更早的普通轮。
    只有 P1 与普通轮会被预算挤掉；P0 与最近 N 轮不动——预算真不够时宁可超一点，
    也不静默丢掉用户说过的规则（超了由 assembler 记账，能看见）。
    """
    turns = group_turns(messages or [])
    if not turns:
        return Selection()

    tail = turns[-recent_turns:] if recent_turns > 0 else []
    head = turns[: len(turns) - len(tail)]

    prio: dict[int, int] = {}          # id(turn) -> 0/1
    for t in head:
        hits = [pri for m in t for _, pri in key_hits(m)]
        if hits:
            prio[id(t)] = 0 if min(hits) == 0 else 1

    p0_turns = [t for t in head if prio.get(id(t)) == 0]
    protected_ids = {id(t) for t in p0_turns[-MAX_P0:]}
    # 更早的 P0 降级成 P1：保护有上界，否则"每句都是要求"的对话等于没有预算

    sel = Selection()
    sel.anchored = len([t for t in head if id(t) in prio])
    sel.protected = len(protected_ids)

    keep = {id(t) for t in head} | {id(t) for t in tail}
    tokens = count_messages([m for t in turns if id(t) in keep for m in t])

    # 丢的顺序：普通轮（最旧先）→ 非保护的锚定轮（最旧先）；保护轮与最近 N 轮不动
    droppable = [t for t in head if id(t) not in protected_ids]
    plain = [t for t in droppable if id(t) not in prio]
    p1 = [t for t in droppable if id(t) in prio]
    for t in plain + p1:
        if tokens <= budget_tokens:
            break
        keep.discard(id(t))
        if id(t) in prio:
            sel.force_dropped += 1
        tokens = count_messages([m for tt in turns if id(tt) in keep for m in tt])

    sel.kept = [m for t in turns if id(t) in keep for m in t]
    sel.dropped = [m for t in turns if id(t) not in keep for m in t]
    sel.tokens = count_messages(sel.kept)
    sel.dropped_tokens = count_messages(sel.dropped)
    sel.reasons = [f"锚定：{'/'.join(sorted({w for m in t for w in key_reasons(m)}))}"
                   for t in head if id(t) in prio and id(t) in keep]
    return sel
