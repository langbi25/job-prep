# Trace 采集：OTel + 可插拔后端（Phoenix / Langfuse）

> 目标不是"接个观测工具"，而是给自进化闭环（评测→归因→分析→优化→再评测）供数据：
> 归因要读的每一类信号——LLM 看到了什么、检索命中了什么、上下文降级到哪级、
> 工具成没成——都必须能按一次请求串起来。

## 1. 架构

```
进程内 OTel SDK（app/observability/tracing.py，OpenInference 语义约定）
  AGENT  agent.console / agent.study_plan      根 span，trace_id 落库关联
  ├─ CHAIN     chain.assemble                  预算占用、dropped、degraded、fallback
  │   └─ RETRIEVER retriever.hybrid            query、top_k、命中数、score
  ├─ LLM       llm.chat                        model、prompt/completion（截断2k）、token 数
  └─ TOOL      tool.search_kb / tool.draft…    参数摘要、结果字符、read_only
        ↓ OTLP/HTTP（BatchSpanProcessor）
  后端可插拔：Phoenix（pip，本地 UI）或 Langfuse v3（docker compose）
```

- 子 span 的父子关系靠 OTel 的 contextvars 上下文传播，业务函数**没有穿任何一个参数**；
- 本地 SQLite 的账本表（runs / ctx_traces / mcp_calls）照旧——它们是领域审计，
  Trace 平台是分析层。`console_messages.ctx_json.trace_id` 是两边的关联键。
- span 的父子上文在**同一进程同一调用链**内成立；MCP 走子进程时是两棵树
  （server 进程自己的 TOOL span），用 `tool.source=mcp:{client}` 区分。

## 2. 开关与配置（.env）

```bash
TRACING_ENDPOINT=            # 空则完全关闭（评测/测试零开销）；不以 traces 结尾会自动补 /v1/traces
TRACING_HEADERS=             # "k=v;k2=v2"，Langfuse 需要 Authorization 头时用
TRACING_SERVICE=job-prep
TRACING_FLUSH_MS=5000        # 批处理窗口：默认 5s；演示/调试设 500 换亚秒级可见
```

### 2.1 实时性实测（2026-11-11）

观测链路是**准实时**的：span 不是逐条发送，而是按批处理窗口打包导出。实测数据：

| 配置 | 单个 span 结束→可见 | 形态 |
|---|---|---|
| 默认 `TRACING_FLUSH_MS=5000` | 3.7 ~ 5.2s | 一批 span 同时出现 |
| `TRACING_FLUSH_MS=500` | 0.3 ~ 0.6s | 逐 span 到达，根 span 未结束就能看到已完成的子 span |

关键结论：**请求还在跑的时候，Phoenix 里已经有部分 span 了**。实测一次 console 对话
（4.1s）——HTTP 响应返回后 0.02s 用 trace_id 就能查到 4 个子 span，根 span
`agent.console` 要等它自己结束后的下一批才到。也就是说「实时」的边界不是请求结束，
而是「span 结束 + 批处理窗口」。

想看秒级瀑布就把窗口调小（`TRACING_FLUSH_MS=500`）；生产保持默认，别为观测
付不必要的网络开销。

### 2.2 代理坑（会伪装成「span 丢一半」）

OTel 的 HTTP 导出器默认 `trust_env=True`，会读系统 `HTTP_PROXY`。本机代理环境下，
发往 `127.0.0.1:6006` 的 OTLP 请求可能被代理劫持，表现为**第一批 span 成功、
后续批次 404，trace 里只剩一半节点**。曾在同一条件下复现两次 100% 触发。

修法：导出器用 `requests.Session()` 并 `trust_env = False`（已写进 `tracing.py`）。
判据：如果 Phoenix 里 trace 缺根或缺子节点、日志里有 `Failed to export spans batch code: 404`，
先怀疑代理，而不是后端。

## 2.3 中文可观测面板（前端内置，日常看这个就够）

Phoenix 原生界面是英文且独立部署（6006 端口），日常排查不必打开它。前端已内置
**「可观测中心」**（左侧菜单，`/observability`），同一端口 8765 即可访问：

- **概览统计**：累计请求数、token 用量、P50/P95 延迟
- **最近请求列表**：中文名称（Agent 对话 / LLM 生成回答 / 工具·xxx / 知识检索 / 请求处理…）、
  耗时、步骤数、状态；超过 3s 标橙色，异常标红
- **调用链瀑布图**：点任意一条请求，看内部每一步的嵌套树 + 时间条，
  可展开/折叠；点行可看该步骤的输入输出摘要（模型名、场景、检索命中数、组装 token 等）
- **自动刷新**：默认 5s 轮询；配合 `TRACING_FLUSH_MS=500`，操作后约 1s 内即可看到新 trace

实现：`app/services/obs_service.py`（Phoenix GraphQL 只读代理 + 中英翻译 + 组树）
→ `app/api/observability.py`（`/api/obs/overview|traces|traces/{id}`，
Phoenix 不可用时返回 `available:false` 降级而非 500）→ `frontend/src/views/Observability.vue`。
测试：`tests/test_observability.py`（假 GraphQL 数据，不依赖真实 Phoenix）。

GraphQL 踩坑记录（换 Phoenix 版本时先复核）：
- 解析器要求参数右括号后必须有空格：`(first: 1) {` 合法，`(first:1){` 直接语法错
- 根 span 过滤是 Python 风格：`filterCondition: "parent_id is None"`（`== null` 匹配不到）
- `attributes` 字段返回 JSON **字符串**而非对象，需二次 `json.loads`
- 本项目 span 属性为嵌套式：`llm.model_name` / `tool.name` / `input.value` / `retriever.n_hits`

## 2.4 埋点清单与业界标准对照（2026-11-12 补全）

对照 OTel GenAI 语义约定 + OpenInference 逐项补齐后的现状：

| 业界标准项 | 状态 | 实现 |
|---|---|---|
| LLM token 消耗（prompt/completion/total） | ✅ | `llm.py` 从 API usage 双写：`gen_ai.usage.*`（OTel 约定）+ `llm.token_count.*`（OpenInference，Phoenix token 列只认这套） |
| 首 token 延迟（TTFT） | ✅ | LLM 调用改流式（`LLM_STREAM=0` 可关），span 属性 `llm.ttft_ms` + 事件 `first_token`；流式失败自动降级非流式并记 `stream_fallback` 事件 |
| 推理（LLM 调用） | ✅ | `llm.chat` span：模型/场景/温度/输入输出摘要 |
| 工具调用 | ✅ | `tool.*` span（console / harness / MCP 三条路径统一） |
| 子 Agent | ✅ | `agent.study_plan` 等嵌套在父 agent span 树下；面板显示「子Agent·xxx」 |
| MCP | ✅ | MCP server 子进程独立成树，`tool.source=mcp:{client}` 区分 |
| Session / 轮次 | ✅ | `session.id` + `session.turn` 上下文自动注入所有 span；Phoenix Sessions 聚合；面板「会话→轮次→trace」三级下钻；轮次 = 用户一句话触发的完整 agent 循环（客观侧口径） |
| 动作留痕（写库/文件） | ✅ | `record_tool_action`：从工具返回值识别 inserted/drafted/retro_id/path 等，记 `tool.action` 事件 + 属性（文件修改、写了几条记录） |
| 命令执行 | ⚠️ 不适用 | 本项目 Agent 没有执行 shell 的工具；若未来加 shell 工具，在实现里 `tr.span("tool.shell", …)` + action 打点即可 |
| Span events 前端呈现 | ✅ | 瀑布图点行展开：事件时间线（⚡首token+TTFT / 🔢token用量 / ✏️工具动作 / ⚠️降级 / ❌异常） |
| 消息级事件（gen_ai.system.message 等） | ❌ 有意不做 | prompt 全文已在 span 属性里（截断 2k），消息级事件会成倍膨胀存储；单机个人项目收益不成比例 |

**经验**：Phoenix 的 token 统计列只认 `llm.token_count.*`——只写 `gen_ai.usage.*` 时面板永远显示 0，双写才是正解。TTFT 只有流式才拿得到，非流式调用的"总耗时"掩盖了首字等待。

## 3. 后端一：Phoenix（本机无 Docker 的推荐路径）

```bash
pip install arize-phoenix
# 21.x 不支持 --host/--port 参数，用环境变量（首次启动约 20~30 秒）
PHOENIX_HOST=127.0.0.1 PHOENIX_PORT=6006 python -m phoenix.server.main serve
# .env 加：TRACING_ENDPOINT=http://127.0.0.1:6006/v1/traces
#（写基地址也行，采集层会自动补 /v1/traces）
# 验证入库：curl -s -X POST http://127.0.0.1:6006/graphql -H "Content-Type: application/json" \
#   -d '{"query":"{ projects { edges { node { name traceCount } } } }"}'
```

实测（2026-11-10）：跑一次学习教练 Agent，1 个 LLM span + 8 个工具 span 全部落进
Phoenix，UI 里能看到完整瀑布图和每段属性。注意：Phoenix 未就绪时导出会报 404
（BatchSpanProcessor 会重试下一批），启动后等 UI 可访问再跑业务流。

## 4. 后端二：Langfuse（装了 Docker 后十分钟切换）

Langfuse v3 原生吃 OTLP：`docker compose up`（官方 langfuse/langfuse-compose 仓库，
需要 Postgres + ClickHouse + MinIO，预留 ~4GB 内存）后：

```bash
# .env：TRACING_ENDPOINT=http://localhost:3000/api/public/otel
#       TRACING_HEADERS=Authorization=Basic <base64(public_key:secret_key)>
```

**代码零改动**。换后端 = 换两个环境变量——这是"埋点与后端解耦"的验收标准。

## 5. 测试与自检

```bash
python tests/test_tracing.py        # InMemorySpanExporter 断言 span 树，不需要后端
python tests/test_phoenix_sync.py   # 双写层集成测试（需要 Phoenix 在 6006，否则整组跳过）
```

## 6. 评测结果双写（P2）

**职责边界**：本地 `run_evals` 仍是执行与门禁的唯一事实源（results-*.json / thresholds /
RAG 看板）；Phoenix 是分析层——数据集和逐用例结果以 dataset + experiment 形式上去，
供瀑布对照与后续归因。刻意**不走** phoenix-client 的 `run_experiment`：那会让平台的
task runner 重新执行业务逻辑，造出评测的第二条执行路径（同坑 6 的教训）。

```bash
python scripts/run_evals.py --skip-llm --phoenix   # 评测时顺带双写（零额外 LLM 调用）
python -m app.observability.phoenix_sync            # 重推 evals/ 最新报告（不重跑评测）
```

双写内容：4 个 golden 数据集（retrieval / search-tool / scoring / extract-judge +
agent-tasks）+ 每个 dimension 一个 experiment，逐用例 run 带 output，
evaluations 带 hit5 / reciprocal_rank / band_match / task_ok 等分数。

**幂等设计**（重推不产生垃圾）：

- 数据集按内容 hash 判断，同内容跳过上传；
- experiment 同名先查再建（服务端不查重）；
- 成功 run 不可变，服务端 409 → 双写层吸收，报告里显示 0 条新 run。

**实测踩坑**（Phoenix 21.0.0，都已写进 `phoenix_sync.py` 模块注释）：

1. `POST /v1/datasets/upload?sync=true` 返回 200 后立即 GET 该数据集可能 404
   ——异步写入的可见性窗口。官方 client 3.7.0 的 `create_dataset` 就栽在这里；
   所以 read-after-write 一律带重试（40 次 × 0.25s）。
2. 不存在的路由返回 SPA HTML（200 + text/html），不能拿状态码当命中，必须验
   content-type。
3. inputs/outputs/metadata 必须等长，不然 422。

覆盖：未配置时零开销零异常；AGENT→LLM/TOOL 父子关系；OpenInference kind 与
GenAI token 属性；场景标签（set_scene 走 contextvar，不走参数）；长文本截断 2000 字。

## 6. 已知取舍

- **PII/截断**：prompt/completion 属性截断 2000 字（MAX_ATTR_CHARS），面经原文不整份出境；
- **采样**：单机全采；生产应换 tail sampling（慢/错 trace 优先），归因主要看失败样本；
- **stdio 下的 MCP**：MCP server 是独立进程、独立 trace，跨进程关联要靠
  traceparent 注入（P2 再做，当前用 tool.source 标记区分）；
- **幂等**：configure 幂等；全局 TracerProvider 只能 set 一次，所以 tracer 从自己的
  provider 取——重复 configure 后 span 仍打到本模块的后端。

## 7. 自进化闭环路线（本文件是 P0/P1）

P2 评测上平台（datasets/experiments 双写）→ P3 归因引擎（失败分类学六类根因）→
P4 优化执行器 + eval gate（prompt vN+1 自动晋级/回滚）→ P5 业务线 YAML spec。
