# MCP 接入说明

把个人知识库接给任何支持 MCP 的宿主（Claude Desktop / Cursor / WorkBuddy），
数据还是那份 SQLite，多的是一个标准入口。

## 1. 接入（宿主怎么连）

两种传输，选一种即可：**本地宿主走 stdio**（宿主自己拉起进程，零网络暴露）；
**跨机器 / 别的 agent 走 streamable HTTP**（需要 token）。

### 1.1 stdio：Claude Desktop / Cursor / WorkBuddy

三家的配置形状一样，只是文件位置不同：

| 宿主 | 配置文件 |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json`（或项目内 `.cursor/mcp.json`） |
| WorkBuddy | `~/.workbuddy/mcp.json`（写入后在「连接器管理」右上角信任该 server） |

```json
{
  "mcpServers": {
    "job-prep": {
      "command": "E:/WorkBuddy/2026-10-25-22-38-23/job-prep/.venv/Scripts/python.exe",
      "args": ["-m", "app.mcp.server"],
      "cwd": "E:/WorkBuddy/2026-10-25-22-38-23/job-prep"
    }
  }
}
```

三个字段每一个都不能省：`command` 必须是**解释器绝对路径**（宿主不会继承你的 venv）；
`cwd` 决定 `.env` 和 `data/kb.sqlite` 的相对位置，指错就读到空库；
`args` 少 `-m` 会变成跑一个同名目录。

### 1.2 streamable HTTP：别的 agent / 远程

```bash
MCP_TOKEN=your-token python -m app.mcp.server --http --port 8766
```

客户端填 `http://127.0.0.1:8766/mcp`，请求头 `Authorization: Bearer your-token`。
没设 `MCP_TOKEN` 只允许绑 127.0.0.1，绑非本地地址会直接拒绝启动——宁可起不来，不要静默裸奔。

自检（不带 token 应 401，带 token 应回 capabilities）：

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

### 1.3 接上之后能问什么

| 你说 | 它会调 |
|---|---|
| 「我 Flink 反压记过什么？」 | `search_kb` →（必要时）`get_card` |
| 「今天该复习什么？」 | `get_daily_brief` + `list_due_cards` |
| 「我哪块最弱，这周怎么补？」 | `get_weak_topics` + `search_kb` |
| 「哪些考点出现最多？」 | `get_top_questions` |
| 「把这段面经收进去：…」 | `ingest_text`（只产待审批动作） |

另外两个 Prompt 可直接在宿主里选：`mock_review`（面试复盘）、`card_from_question`（按题建卡）。

## 2. 暴露了什么

**Tools（9 个，模型决定调用；读 6 写 3）**

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
| `ingest_retrospective` | 写 | 复盘文档一条龙：提炼考点 → 入池 → 起草卡 |

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

### 5.1 让内置 harness 自己切来源（不改调用方）

`run_study_plan_agent` 的工具来源由 `config.HARNESS_TOOL_SOURCE` 决定，循环只有一份：

```bash
python -c "from app.services.harness import run_study_plan_agent; print(run_study_plan_agent('backend'))"
HARNESS_TOOL_SOURCE=mcp python -c "from app.services.harness import run_study_plan_agent; print(run_study_plan_agent('backend'))"
```

`SyncMCPToolbox` 用一个后台线程跑常驻事件循环托住 MCP 会话：anyio 要求 cancel scope 的
enter/exit 在同一任务，跨任务退出会抛 `Attempted to exit cancel scope in a different task`
——所以会话生命周期只放在一个协程里，请求走队列进去，而不是每次调用 `asyncio.run`（那还会漏子进程）。

两边的**工具集合不同，但清单只有一个来源**：`app/mcp/registry.py` 登记 18 个工具，
每条带 `surfaces` 标记，两个出口各自过滤——

| 出口 | 数量 | 过滤条件 |
|---|---|---|
| 内置 harness | 17 | `surface="agent"`（细粒度：采集确认、记忆读写、结果翻页都给了） |
| MCP 网关 | 9 | `surface="mcp"`（读 6 写 3，粒度按「一次对话回合能完成的任务」切） |

`get_card` 是唯一只对外的工具（`surfaces=("mcp",)`）：它是「按 id 下钻全文」，
服务外部宿主「先 search 拿摘要、再点进去看全文」的交互习惯；内置循环目前靠 `search_kb`
回的要点摘要，不给全文下钻，避免学习计划多绕一次工具调用。

改工具的正确流程（三处都由注册表派生，只需改两处）：

1. 在 `registry.py` 加/改 `ToolDef`（描述与 JSON Schema 都在这里）；
2. 在 `harness._IMPL` 或 `agent_tools.IMPL` 加实现；
3. 若 `surfaces` 含 `mcp`，再在 `app/mcp/tools.py` 补一个带 `Annotated[..., Field(...)]`
   的签名薄壳（MCP SDK 靠函数签名生成 inputSchema，绕不过去）。

漏第 2 步或第 3 步都会在**导入期**抛错，不会等到模型调用时才发现。
`tests/test_tool_registry.py` 11 条断言把这几层的一致性钉住了，包括
「pydantic 生成的 inputSchema 与注册表的属性名/required/枚举逐项相同」——
`draft_cards` 曾经就因为漏了这个比对而少暴露 `topic` 参数，
外部客户端说「给 Flink 补卡」时传不进主题，只能从全池抓题、补错主题。

**两条路怎么选**：
- 评测、批量跑 → 进程内直调（快、无子进程，24 用例省 0.5 秒）；
- 验证协议 / 演示「任何客户端都能接」→ 走 MCP（和 Claude Desktop 完全同一条路径）。
- 生产只保留一个入口，别让同一个循环同时维护两套工具来源——那是分叉的开始。

## 6. 已知限制

- 单用户单库：没有多租户和行级权限，HTTP 只做了 Bearer 校验；
- 没有实现 resource 订阅与 progress 通知，长任务（如 `ingest_text`）是同步返回；
- 选择档依赖 LLM_API_KEY，未配置时自动跳过并标注，不算失败；
- 24 条用例覆盖 9 个工具，边界问法（同义改写、跨工具组合）还需要扩；
- MCP 的 HTTP 传输代码支持（`--http` + `MCP_TOKEN`），但 **docker-compose 里没有映射 8766 端口**，
  容器内只有 stdio 这条路实际可用；远程接入需要先补端口与 token 注入。
