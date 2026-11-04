---
id: kb-cs-context-001
domain: cs
topic: Context管理
title: LLM 上下文管理策略：Token 预算、滑动窗口、摘要压缩与长期记忆检索
status: active
mastery: 0
srs_due: ""
tags: ["Agent建议"]
related: []
source: extract
created: 2026-11-04
updated: 2026-11-04
---

# LLM 上下文管理策略：Token 预算、滑动窗口、摘要压缩与长期记忆检索

**Q：** 随着用户对话轮数增加，Context会出LLM 的Token窗口限制，你会如何设计上下文管理策略?

## 要点

- Token 预算控制：根据模型窗口上限预留输出 Token 和安全余量，合理分配 System Prompt、历史对话、检索内容等部分的上下文预算。
- 短期记忆保留：采用滑动窗口机制，固定保留 System Prompt 和最近 N 轮对话，优先满足近期交互的连续性。
- 历史摘要压缩：通过异步 Summarize 将较早的对话提炼为 Key Points，保留关键事实、任务进度和重要约束，降低上下文占用。
- 长期记忆检索：将用户偏好、关键实体和历史任务等信息存入长期记忆，需要时通过 Vector DB 进行 Top-K 检索，按相关性补充上下文。
- 动态管理：关注时效性和 Token 占用，避免重复、过时或冲突的信息进入上下文，并持续监控效果。

## 易错

- （无）
