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
```

## 3. 后端一：Phoenix（本机无 Docker 的推荐路径）

```bash
pip install arize-phoenix
phoenix serve                       # UI http://localhost:6006
# .env 加：TRACING_ENDPOINT=http://localhost:6006
python -m app.mcp.server            # 或重启 uvicorn，跑一轮对话
```

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
```

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
