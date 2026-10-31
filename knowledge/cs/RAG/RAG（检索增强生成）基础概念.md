---
id: kb-cs-rag-003
domain: cs
topic: RAG
title: RAG（检索增强生成）基础概念
mastery: 0
srs_due: ""
tags: ["Agent建议"]
related: []
source: extract
created: 2026-10-31
updated: 2026-10-31
---

# RAG（检索增强生成）基础概念

**Q：** 先说说，什么是RAG?

## 要点

- RAG全称Retrieval-Augmented Generation，即检索增强生成，核心思路是模型回答问题前先从外部知识库检索相关材料，再将这些材料放入上下文，让模型基于材料生成答案。
- 常见流程为：文档 -> 切chunk -> embedding -> 向量库 -> top-k检索 -> 拼进prompt -> LLM生成答案。
- 适用于FAQ、产品文档问答、知识库检索等场景。
- 价值在于让模型从“只靠参数记忆回答”变成“能引用外部资料回答”。

## 易错

- （无）
