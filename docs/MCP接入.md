# MCP 接入说明

把个人知识库接给任何支持 MCP 的宿主（Claude Desktop / Cursor / WorkBuddy），
数据还是那份 SQLite，多的是一个标准入口。

## 1. 启动

```bash
# stdio（本地宿主拉起，进程边界即权限边界）
python -m app.mcp.server

# streamable HTTP（远程接入）
MCP_TOKEN=your-token python -m app.mcp.server --http --port 8766
```

Claude Desktop 的 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "job-prep": {
      "command": "C:/Users/<you>/.workbuddy/binaries/python/envs/default/Scripts/python.exe",
      "args": ["-m", "app.mcp.server"],
      "cwd": "C:/Users/<you>/WorkBuddy/2026-10-25-22-38-23/job-prep"
    }
  }
}
```

## 2. 暴露了什么

**Tools（8 个，模型决定调用）**

| 工具 | 类型 | 用途 |
|---|---|---|
| `search_kb` | 读 | 混合检索（FTS + 向量），返回卡片要点与置信度 |
| `get_card` | 读 | 按 id 取全文（要点 / 常见坑 / 覆盖的题） |
| `get_daily_brief` | 读 | 到期卡、未解决错题、近期面试均分 |
| `list_due_cards` | 读 | 今日到期清单 |
| `get_weak_topics` | 读 | 薄弱主题，可按方向过滤 |
| `get_top_questions` | 读 | 高频题（真实考点分布） |
| `draft_cards` | 写 | 起草知识卡 → **只产待审批动作** |
| `ingest_text` | 写 | 面经入库 → 抽题 / 质量闸门 / 起草卡 |

**Resources**：`kb://brief/today`、`kb://card/{card_id}`
**Prompts**：`mock_review`（面试复盘）、`card_from_question`（按题建卡）

## 3. 三条硬边界

1. **接口层不含业务逻辑**：每个工具只调 service，REST 和内置 Agent 走同一条实现。
   同一条数据有多个读路径时，不共用查询就等着它们悄悄分叉（见 `docs/架构-知识库.md` 坑 6）。
2. **写工具不落库**：`draft_cards` / `ingest_text` 产的是审批队列条目，人点确认才写。
   页面「试跑」直接拒绝写工具——外部宿主的输入不可信，写入口只留一条。
3. **HTTP 默认要 token**：设了 `MCP_TOKEN` 才允许非 127.0.0.1 绑定，否则拒绝启动。

## 4. 看什么（可观测 + 评测）

页面：侧边栏「MCP 看板」。

- 每次调用落 `mcp_calls`（工具 / 来源 / 耗时 / 状态 / 返回字符 / 参数摘要），
  stdio 传输下 stdout 是协议管道，打日志会破坏协议，只能结构化落库；
- **契约档**：固定参数打读工具，验 schema / 实现 / 返回非空（零 LLM 调用，约 2 秒）；
- **选择档**：只给工具清单 + 一句自然语言请求，让模型自己选工具填参数，
  比对期望后算工具准确率、参数准确率、端到端成功率、越权率。

```bash
python scripts/mcp_eval.py            # 契约档
python scripts/mcp_eval.py --with-llm # 两档都跑（24 次 LLM 调用）
python scripts/mcp_client_smoke.py    # 用官方 SDK 客户端走一遍 stdio 全链路
```

最近一次（2026-11-09，24 用例）：契约 16/16，工具选择 24/24，参数 100%，端到端 100%，越权 0。
样本只有 24 条，只能说明「当前工具描述没有明显歧义」，不能当生产结论。

## 5. 本地 harness 走 MCP 调（客户端方向）

`app/services/harness.py` 原本是进程内直调 `_IMPL`。`app/mcp/client.py` 给了一个 stdio 客户端桥，
同一个 DeepSeek 循环可以改成「工具清单来自 tools/list、执行走 tools/call」：

```bash
python scripts/harness_via_mcp.py "我今天该复习什么？顺便看看 Flink 反压我记过什么"
python scripts/harness_via_mcp.py --bench    # 只测协议开销
```

实测（20 次均值，本机）：

| 方式 | get_top_questions | get_daily_brief |
|---|---|---|
| 进程内直调 | 10.9 ms | 36.0 ms |
| 走 MCP stdio | 30.8 ms | 53.6 ms |

开销约 18~20 ms/次（首次含冷启动 65ms），主要来自 Windows 管道往返 + 每次调用的审计落库。
对「查一次知识库 30ms」这个量级可以接受；对毫秒级小工具就不划算。

**两条路怎么选**：
- 评测、批量跑 → 进程内直调（快、无子进程，24 用例省 0.5 秒）；
- 验证协议 / 演示「任何客户端都能接」→ 走 MCP（和 Claude Desktop 完全同一条路径）。
- 生产只保留一个入口，别让同一个循环同时维护两套工具来源——那是分叉的开始。

## 6. 已知限制

- 单用户单库：没有多租户和行级权限，HTTP 只做了 Bearer 校验；
- 没有实现 resource 订阅与 progress 通知，长任务（如 `ingest_text`）是同步返回；
- 选择档依赖 LLM_API_KEY，未配置时自动跳过并标注，不算失败；
- 24 条用例覆盖 8 个工具，边界问法（同义改写、跨工具组合）还需要扩。
