"""MCP 工具的对外签名层。

每个函数只做三件事：把带类型的入参收成 dict → 交给网关执行 → 返回文本。
真正的实现在 `gateway.SPECS`（读工具复用 harness，写工具复用 agent_tools），
所以 REST、内置 Agent、MCP 三条入口共享同一份逻辑——这是刻意的，不要让它们分叉。

签名用 `Annotated[..., Field(description=...)]` 标注，MCP SDK 会据此生成 inputSchema；
描述写的是「什么时候用」，不是实现细节——工具描述本身就是给模型看的 prompt。
"""
from typing import Annotated

from pydantic import Field

from . import gateway


def search_kb(
    query: Annotated[str, Field(description="检索语句：可以是题目原文、关键词或自然语言问法")],
    topic: Annotated[str | None, Field(description="限定主题，如 Flink / Agent 记忆")] = None,
    top_k: Annotated[int, Field(description="返回卡片数，默认 3", ge=1, le=5)] = 3,
) -> str:
    """在个人知识库做混合检索。需要回忆「我到底记过什么」时用它，不要凭记忆编内容；返回空 = 知识缺口。"""
    return gateway.invoke("search_kb", {"query": query, "topic": topic, "top_k": top_k}).payload


def get_card(
    card_id: Annotated[str, Field(description="知识卡 id，形如 kb-backend-gen-001；一般来自 search_kb 结果")],
) -> str:
    """按 id 取一张知识卡的完整内容。search_kb 只回摘要，要读全文（要点/常见坑/覆盖的题）时调这个。"""
    return gateway.invoke("get_card", {"card_id": card_id}).payload


def get_daily_brief() -> str:
    """今天该干什么的全局视图：到期卡数量、未解决错题、近期模拟面试均分、薄弱卡片。回答「我今天学什么」先调它。"""
    return gateway.invoke("get_daily_brief", {}).payload


def list_due_cards(
    limit: Annotated[int, Field(description="最多返回条数，默认 10", ge=1, le=50)] = 10,
) -> str:
    """列出今日到期的复习卡片（含主题和掌握度）。用户问「今天要复习哪些」时用。"""
    return gateway.invoke("list_due_cards", {"limit": limit}).payload


def get_weak_topics(
    direction: Annotated[str | None, Field(description="方向过滤：backend / agent / bigdata")] = None,
) -> str:
    """薄弱主题清单：被面试打低分的卡 + 近 14 天错题主题。回答「我哪里最弱」时用。"""
    return gateway.invoke("get_weak_topics", {"direction": direction}).payload


def get_top_questions(
    limit: Annotated[int, Field(description="返回条数，默认 5", ge=1, le=20)] = 5,
) -> str:
    """题目池高频题，按面经出现次数排序——频次就是真实考点分布。"""
    return gateway.invoke("get_top_questions", {"limit": limit}).payload


def draft_cards(
    question_ids: Annotated[list[str] | None, Field(description="题目 id 列表；不传则按高频无卡题挑")] = None,
    limit: Annotated[int, Field(description="最多起草几张，默认 2", ge=1, le=3)] = 2,
    only_uncarded: Annotated[bool, Field(description="只挑还没有卡的题")] = True,
) -> str:
    """为题目起草知识卡（写操作）。只产待审批动作，不会直接落库——用户要「帮我建卡」时才调。"""
    return gateway.invoke("draft_cards", {
        "question_ids": question_ids, "limit": limit, "only_uncarded": only_uncarded}).payload


def ingest_text(
    text: Annotated[str, Field(description="面经原文全文，不要截断、不要总结")],
    source_type: Annotated[str, Field(description="来源类型：paste / url / manual")] = "paste",
    draft_cards: Annotated[bool, Field(description="入池后是否顺手起草卡")] = True,
    max_cards: Annotated[int, Field(description="自动起草几张，默认 2", ge=0, le=3)] = 2,
) -> str:
    """把一份面经收进系统（写操作）：存档 → 抽题 → 质量闸门 → 入池 → 起草待审卡。用户粘贴面经说「收进去」时调。"""
    return gateway.invoke("ingest_text", {
        "text": text, "source_type": source_type, "draft_cards": draft_cards,
        "max_cards": max_cards}).payload


REGISTERED = [search_kb, get_card, get_daily_brief, list_due_cards, get_weak_topics,
              get_top_questions, draft_cards, ingest_text]
