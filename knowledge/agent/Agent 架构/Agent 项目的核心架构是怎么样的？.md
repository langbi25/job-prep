---
id: kb-agent-agent-001
domain: agent
topic: Agent 架构
title: Agent 项目的核心架构是怎么样的？
mastery: 0
srs_due: ""
tags: ["Agent架构", "编排层", "工具调用", "记忆", "RAG", "可观测性"]
related: []
source: agent
created: 2026-10-31
updated: 2026-10-31
---

# Agent 项目的核心架构是怎么样的？

**Q：** Agent 项目的核心架构是怎么样的?

## 要点

- 我先给整体分层：接入层（API/WebSocket）→ 编排层（Agent Runtime/状态机）→ 能力层（LLM 调用、工具/插件、RAG 检索、记忆）→ 基础设施层（向量库、消息队列、可观测）。
- 编排层是核心，我用（填入：LangGraph / 自研状态机 / Dify 等）实现，把一次任务拆成 Plan→Act→Observe 循环，用显式状态对象在节点间传递，避免把上下文全塞进 prompt。
- 工具调用走统一的 Tool Schema（name/description/参数 JSON Schema），由 LLM 输出结构化 function call，Runtime 做参数校验、超时和重试后再执行，不让模型直接拼命令。
- 记忆分三层：会话短期记忆放（填入：Redis / 进程内），长期事实存向量库做语义召回，用户画像/偏好存结构化 DB；写入前做摘要压缩，控制 token 预算。
- RAG 链路是 query 改写→向量召回 topK（填入：如 5~10）→ rerank→拼装上下文，并在回答里带引用，降低幻觉、方便排查。
- 可靠性上，LLM 调用和工具执行都设超时+指数退避重试，关键步骤做幂等（用 request_id 去重），失败可从中断的 checkpoint 恢复而不是整轮重跑。
- 可观测性：每次请求打 trace_id，记录每步的 prompt、token 数、工具入参出参和耗时，用（填入：LangSmith / 自建日志）做链路回放，线上问题能定位到具体节点。
- 成本与延迟权衡：简单意图走小模型或规则路由，复杂任务才进多步 Agent；对可并行的工具调用并发执行，并设最大步数（填入：如 10 步）防止死循环烧钱。

## 易错

- 只讲用了什么框架，讲不清自己设计的状态流转和失败恢复，会被追问到答不上。
- 被追问多步任务如何防死循环、如何控制 token 成本，要提前准备具体数值。
- 工具调用失败/模型输出非法 JSON 时的兜底策略要能说清楚。
- 别把 RAG 和 Agent 混为一谈，要说明检索在架构中的位置。
