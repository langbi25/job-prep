"""意图识别：给编排目标打标签，让"用户在拿 agent 干什么"变成可统计的数据。

为什么可观测需要意图：agent_plans 里只有 goal 原文，文字看得懂但统计不了。
指标能回答"编排平均花 ¥0.2"，回答不了"是哪类意图在花这个钱、哪类意图最容易
失败"。有意图标签之后这两个问题都变成一行 GROUP BY。

两级分类，规则的优先：
1. **规则命中直接用**。关键词是确定性的，零成本、可测试、可解释——
   "查一下/多少/进度"是查状态这件事不需要模型来判。
2. **规则未命中才调 LLM**。分类 prompt 只有几十 token，成本可忽略，
   但每次编排都多一次往返会拖慢入口，所以只给规则接不住的模糊目标。

刻意不做的事：
- **不做多标签**。一个目标一个主意图。统计口径里"平均每意图成本"在多标签下
  没有意义（一次花 5 毛的编排算给谁？）。
- **置信度不落库**。规则命中就是确定，LLM 的自报置信度没有被校准过，
  存下来只会让人误信。
"""
from ..core import llm, prompts

# 意图枚举。顺序即展示顺序；other 兜底必须是最后一个。
INTENTS: dict[str, str] = {
    "status_query": "查状态",
    "analysis": "分析诊断",
    "content": "写作产出",
    "planning": "规划安排",
    "review": "纠错复盘",
    "other": "其他",
}

# 规则表：(意图, 关键词元组)。命中任一关键词即分类成功。
# 关键词刻意保守——只收"几乎不会误伤"的词；边界词交给 LLM 兜底，
# 宁可多花一次分类调用，也别把"对比一下 A 和 B"错分进查状态。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status_query", ("查一下", "查查", "查下", "还有多少", "有多少", "几个", "哪些",
                      "进度", "清单", "列表", "列出", "状态", "统计")),
    ("analysis", ("分析", "为什么", "为啥", "原因", "根因", "对比", "差异",
                  "哪个更", "哪个该", "优先级", "评估", "诊断")),
    ("content", ("写一份", "写个", "写一篇", "生成", "起草", "总结一份",
                 "报告", "提纲", "简历", "自我介绍", "排版")),
    ("planning", ("计划", "规划", "安排", "排一下", "日程", "接下来怎么",
                  "怎么准备", "备考方案")),
    ("review", ("复盘", "错在哪", "错因", "错题", "纠正", "纠错",
                "哪里错了", "改进", "总结教训")),
)


def _rule_hit(goal: str) -> str | None:
    for intent, words in _RULES:
        if any(w in goal for w in words):
            return intent
    return None


def classify(goal: str) -> dict:
    """意图分类。method: rule（规则命中）/ llm（模型兜底）/ error（模型也挂了）。

    永不抛异常：意图是观测，分类失败绝不能让编排起不来——
    退 other 加 error 标记，指标页能看到"多少目标没分出来"。
    """
    goal = (goal or "").strip()
    if not goal:
        return {"intent": "other", "label": INTENTS["other"], "method": "rule"}

    hit = _rule_hit(goal)
    if hit:
        return {"intent": hit, "label": INTENTS.get(hit, hit), "method": "rule"}

    try:
        from ..observability import tracing as tr
        tr.set_scene("intent:classify")
        system, user, _v = prompts.render("intent_classify", goal=goal[:800])
        data = llm.chat_parsed(system, user)
        intent = str(data.get("intent") or "").strip()
        # 模型报出不存在的类别（幻觉枚举）按未分类处理，不静默映射成别的类——
        # 否则"模型在乱分类"这件事在指标里看不见。
        if intent not in INTENTS:
            return {"intent": "other", "label": INTENTS["other"],
                    "method": "llm", "intent_error": f"unknown:{intent[:20]}"}
        return {"intent": intent, "label": INTENTS[intent], "method": "llm"}
    except Exception as e:  # noqa: BLE001
        return {"intent": "other", "label": INTENTS["other"],
                "method": "error", "intent_error": f"{type(e).__name__}: {e}"[:120]}
