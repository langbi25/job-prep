---
id: kb-project-context-001
domain: project
topic: Context管理
title: 【实战】随着用户对话轮数增加，Context会出LLM 的Token窗口限制，你会如何设
status: active
mastery: 0
srs_due: ""
tags: ["Context管理", "Token预算", "滑动窗口", "摘要压缩", "Agent工程", "FastAPI", "实战复盘"]
related: ["kb-cs-context-001"]
source: agent
created: 2026-11-05
updated: 2026-11-05
---

# 【实战】随着用户对话轮数增加，Context会出LLM 的Token窗口限制，你会如何设

**Q：** 随着用户对话轮数增加，Context会出LLM 的Token窗口限制，你会如何设计上下文管理策略?

## 要点

- 新增 app/context/token_budget.py，用 CJK×0.7 + ASCII×0.28 估算 token，按 system 15% / 检索 25% / 历史 60% 分配预算，不装 tiktoken 因与 DeepSeek 不同源。
- window.py 实现分级锚定：P0 约束（必须/不要/记住）最近 4 条永不丢，P1 引用（业务 id/评分）预算不足时从旧丢，最近 6 轮必留。
- summarizer.py 对窗口外旧轮次生成三段式摘要（事实/进度/约束），落 conversation_summary 表，用 covered_to_id 防重复压缩，BackgroundTasks 异步执行。
- memory_retriever.py 复用 rag_service.hybrid_search，以当前问题检索 Top-3 摘要注入，不做跨会话记忆，避免 2C2G 装向量库。
- assembler.py 统一出口，降级顺序为检索 → 摘要 → 最旧轮次，每轮写 ctx_traces 表，前端显示「输入 1115 / 预算 12000 tokens · 含历史摘要」。
- enforce_inflight 轮内裁剪：工具结果非末尾裁到 600 字，再连末尾裁，最后全裁到 200 字，保护最后一条是偏好非保证。
- tests/test_context_budget.py 构造 20 轮对话，断言总 token 不超预算、关键轮次保留、摘要落库；对比脚本记录平均输入 token 与 P95 延迟（待补：改前/改后具体数值）。
- 实测单轮 17516 tokens 顶到窗口 1/3，改后 20 轮平均输入 token 稳定在 3500 以内（待补：改后精确均值），超窗报错 0 次。

## 易错

- 锚定轮自己也会被挤掉：第一版锚定轮仍参与历史预算竞争，第 17 轮起约束保留从 20 掉到 16；改成分级 P0 由 protected_ids 显式保护才解决。
- 摘要被模型包成 JSON：_chat_json 非 tools 分支默认 response_format=json_object，导致摘要存成 {"type":"json_object","content":"事实：…"}；加 json_mode=False 分支修复。
- 摘要写了但永远不会被用上：功能实现且测试通过，但真实会话中 summarizer 结果未注入 assembler 读取路径（推断：需检查摘要读取与注入的衔接逻辑）。
- 这类改造常见的坑（推断）：tokenizer 与线上模型切分不一致导致估算偏差，需留安全余量；检索噪声注入干扰回答，需设相关性阈值与条数上限。
