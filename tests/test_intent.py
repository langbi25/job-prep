"""意图识别测试（零真实 LLM 调用，LLM 兜底路径全部 monkeypatch）。

盯两条底线：
1. **分类失败绝不能让编排挂掉**——意图是观测，不是功能，classify 的 except
   兜底是硬要求，用"chat_parsed 抛异常"直接钉住；
2. **模型报出不存在的类别不能静默映射**——幻觉枚举映射成别的类后，
   "模型在乱分类"这件事在指标里就看不见了。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents import intent  # noqa: E402


def test_rule_hits_are_free_and_correct():
    """规则命中的目标不该走 LLM——确定性、零成本、不拖慢编排入口。"""
    for goal, want in [
        ("查一下我最近两周的薄弱点", "status_query"),
        ("分析为什么模块三正确率上不去", "analysis"),
        ("写一份面试自我介绍", "content"),
        ("复盘上次的模拟面试，错在哪", "review"),
        ("帮我规划接下来的备考日程", "planning"),
    ]:
        out = intent.classify(goal)
        assert out["intent"] == want, (goal, out)
        assert out["method"] == "rule", out


def test_rule_miss_falls_back_to_llm(monkeypatch):
    """规则接不住的模糊目标走 LLM 兜底，且只调一次。"""
    calls = []

    def fake_chat_parsed(system, user, **kw):
        calls.append(user)
        return {"intent": "review", "reason": "用户要找问题"}

    monkeypatch.setattr(intent.llm, "chat_parsed", fake_chat_parsed)
    out = intent.classify("这两个offer应该怎么选")
    assert out["intent"] == "review"
    assert out["method"] == "llm"
    assert len(calls) == 1


def test_llm_failure_returns_other_not_raise(monkeypatch):
    """模型挂了退 other + error 标记，绝不抛异常拖垮编排。"""
    def boom(*a, **kw):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(intent.llm, "chat_parsed", boom)
    out = intent.classify("随便看看")
    assert out["intent"] == "other"
    assert out["method"] == "error"
    assert "RuntimeError" in out["intent_error"]


def test_llm_hallucinated_intent_is_not_silently_mapped(monkeypatch):
    """模型编造类别 → other，且带 unknown 标记让指标页看得见。"""
    monkeypatch.setattr(intent.llm, "chat_parsed",
                        lambda *a, **kw: {"intent": "shopping", "reason": "想买东西"})
    out = intent.classify("模糊目标 xyz")
    assert out["intent"] == "other"
    assert out["method"] == "llm"
    assert "shopping" in out["intent_error"]


def test_empty_goal_no_llm():
    """空目标直接 other，连 LLM 都不调。"""
    out = intent.classify("")
    assert out["intent"] == "other"
    assert out["method"] == "rule"


def test_intents_taxonomy_has_other_last():
    """other 必须是兜底枚举；指标页和分类 prompt 都依赖这个约定。"""
    keys = list(intent.INTENTS)
    assert keys[-1] == "other"
    assert len(keys) == len(set(keys))
