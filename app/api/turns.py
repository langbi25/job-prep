"""回合质量接口：自进化的读数面板。

和 `/api/traces`（调用树）的分工：
- `/api/traces` 回答"这一轮内部是怎么跑的"——span 瀑布、每步耗时
- `/api/turns`  回答"这一轮跑得好不好、为什么不好"——一行事实 + 归因

两者数据源不同（spans 表 / turn_facts 表），但**都在本地**。原来这里写的是
"obs 的 span 在后端（Phoenix/Langfuse）"，自 2026-11-12 夜 span 落库后就不成立了：
质量面板不再依赖"有没有起观测后端"，这是那次改动最实际的一个收益。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import turn_facts

router = APIRouter(prefix="/api/turns", tags=["turns"])


class FeedbackBody(BaseModel):
    score: int                      # 1 有用 / -1 没用
    note: str | None = None


class SeatFeedbackBody(FeedbackBody):
    session_id: str
    turn: int


@router.get("/quality")
def quality(days: int = 14):
    """总览：回合数、失败率、点踩率、追问率、耗时分布、按规则/技能/天的拆分。"""
    return {"summary": turn_facts.summary(days), "params": turn_facts.params()}


@router.get("/coverage")
def coverage(days: int = 14):
    """落账覆盖率自检：真实发生过多少轮、记下来多少轮。

    这张表的意义是给上面所有比例划边界。覆盖率 40% 时看到"失败率 5%"，
    真实失败率可能是 12%——漏掉的恰恰是最可能失败的（早退、异常、没走到落账）。
    放在面板最上方，就是为了让人先看分母再看分子。
    """
    return turn_facts.coverage(days)


@router.get("/clusters")
def clusters(days: int = 14, per_rule: int = 3):
    """失败簇：按规则聚合，每簇带真实样本——自进化改 prompt 时的取样入口。"""
    return turn_facts.clusters(days, per_rule=per_rule)


@router.get("/improvement")
def improvement(days: int = 14, per_skill: int = 3):
    """按技能给出「该改哪里」——归因的决策出口，不是又一个统计表。"""
    return turn_facts.improvement(days, per_skill=per_skill)


@router.get("/regression-candidates")
def regression_candidates(days: int = 14, limit: int = 50):
    """把真实出问题的回合整理成回归用例草稿（must_mention 留空，等人补）。"""
    rows = turn_facts.regression_candidates(days, limit=limit)
    return {"candidates": rows, "count": len(rows),
            "note": "must_mention 必须人工补：让模型自己定「该提到什么」等于自评自判"}


@router.get("")
def recent(days: int = 14, limit: int = 30, only_flagged: bool = False):
    rows = turn_facts.recent(days, limit=limit, only_flagged=only_flagged)
    return {"turns": rows, "count": len(rows)}


@router.get("/{turn_id}")
def detail(turn_id: str):
    row = turn_facts.detail(turn_id)
    if row is None:
        raise HTTPException(404, "没有这条回合记录")
    return row


@router.post("/{turn_id}/feedback")
def feedback(turn_id: str, body: FeedbackBody):
    row = turn_facts.set_feedback(turn_id, body.score, body.note)
    if row is None:
        raise HTTPException(404, "没有这条回合记录")
    return row


@router.post("/feedback")
def feedback_by_seat(body: SeatFeedbackBody):
    """按「会话 + 轮次」写反馈。

    前端手里只有"这是第几条消息"，没有 turn_id（那是落库时才生成的）。
    让它去猜 id 不如给它一个稳定坐标——(session_id, turn) 是唯一索引，
    一轮只会有一行。
    """
    row = turn_facts.by_turn(body.session_id, body.turn)
    if row is None:
        raise HTTPException(404, "这一轮没有质量记录（可能是升级前发生的对话）")
    out = turn_facts.set_feedback(row["id"], body.score, body.note)
    return out or row
