---
id: kb-agent-mcpskill-001
domain: agent
topic: MCP/Skill/CLI
title: MCP、Skill 和 CLI 的区别是什么？
mastery: 0
srs_due: ""
tags: ["MCP", "Skill", "CLI", "Agent工具调用", "协议对比"]
related: []
source: agent
created: 2026-10-31
updated: 2026-10-31
---

# MCP、Skill 和 CLI 的区别是什么？

**Q：** MCP和Sk训的区别是什么?和CLI的区别呢?

## 要点

- MCP 是 Model Context Protocol，由 Anthropic 提出的开放协议，用标准化的 client-server 架构让模型动态发现和调用外部工具、资源与提示模板。
- MCP 的核心价值是解耦：工具提供方实现 MCP Server，Agent 侧实现 MCP Client，双方通过 JSON-RPC 2.0 通信，新增工具无需改 Agent 代码。
- Skill 是 Agent 内部封装的可复用能力单元，通常由提示词、工具调用序列和少量代码组成，强调在特定任务上的稳定执行与编排。
- Skill 与 MCP 的层次不同：Skill 是 Agent 侧的“怎么用”，MCP 是工具侧的“怎么接”，一个 Skill 可以调用多个 MCP Server 提供的工具。
- CLI 是操作系统层面的命令行接口，Agent 通过 shell 执行命令来操作环境，优点是通用、无需额外协议，缺点是输出非结构化、权限与安全难控。
- 从集成成本看：CLI 最低但最脆弱；MCP 需要实现 Server 但一次接入多处复用；Skill 需要针对任务设计编排逻辑，开发成本最高但执行最稳。
- 从安全边界看：CLI 直接暴露系统权限，风险最大；MCP 可在 Server 侧做鉴权和审计；Skill 可在 Agent 侧做输入校验和调用约束。
- 工程选型上：临时探索用 CLI，跨团队工具复用用 MCP，高频稳定任务用 Skill 固化，三者常组合使用而非互斥。

## 易错

- 容易把 Skill 说成 MCP 的同义词，实际 Skill 是 Agent 侧编排，MCP 是工具侧协议。
- 忽略 CLI 在 Agent 场景下的输出解析和安全沙箱问题，被追问时答不出边界。
- 只背定义不讲选型权衡，面试官会追问“你们项目为什么选这个”。
