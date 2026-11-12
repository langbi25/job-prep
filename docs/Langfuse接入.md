# Langfuse 自托管接入

> 状态：**适配层已完成并通过测试（55 项全绿），等待本机 Docker 环境就绪后联调**
> 阻塞点：本机未装 Docker，且 `wsl.exe` 在安全策略黑名单里（Docker Desktop 依赖 WSL2 后端）

## 0. 先搞清楚定位

Phoenix 和 Langfuse 是**平级的两个 OTel 后端**，不是上下游：

```
业务代码 → OTel SDK（进程内唯一一份） → OTLP/HTTP → [ Phoenix 或 Langfuse ]
                                                      ↑ 由 TRACING_ENDPOINT 决定
```

打点层不感知后端。切后端 = 改一行 `TRACING_ENDPOINT`；中文面板读哪个后端 = 改一行 `OBS_BACKEND`。

## 1. 前置条件（需要手动完成）

| 项 | 检查方式 | 说明 |
|---|---|---|
| WSL2 可执行 | `wsl --status` | 被安全中心拦截时需 **安全中心 → 命令安全 → 程序黑名单** 移除 `wsl.exe` |
| Docker Desktop | `docker --version` | 安装时勾选 "Use WSL 2 based engine" |
| 磁盘空间 | — | 镜像约 2~3 GB |

## 2. 起服务

```bash
cd deploy
docker compose -f langfuse-compose.yml up -d
docker compose -f langfuse-compose.yml ps      # 等 postgres / clickhouse / minio / redis 全部 healthy
```

首次启动约 2~5 分钟。UI: <http://localhost:3000>

**compose 里的 `# CHANGEME` 占位符**：本地自用可直接用默认值；一旦监听地址暴露到局域网，
必须先改 `SALT` / `ENCRYPTION_KEY`（`openssl rand -hex 32`）/ `NEXTAUTH_SECRET` / `CLICKHOUSE_PASSWORD`。

## 3. 初始化并拿 API Key

1. 打开 <http://localhost:3000> 注册第一个账号（即管理员）
2. 建 Organization → 建 Project（项目名建议 `job-prep`）
3. Project → Settings → API Keys → Create new API key
4. 记下 `pk-lf-...`（public）和 `sk-lf-...`（secret），secret 只显示一次

## 4. 配 `.env`

```dotenv
OBS_BACKEND=langfuse

TRACING_ENDPOINT=http://localhost:3000/api/public/otel
# base64 = echo -n "pk-lf-xxx:sk-lf-xxx" | base64
TRACING_HEADERS=Authorization=Basic <base64>;x-langfuse-ingestion-version=4
TRACING_FLUSH_MS=500

LANGFUSE_URL=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
```

`x-langfuse-ingestion-version=4` 不能漏：不带这个头，直接摄入的 OTel 数据最多延迟 10 分钟才可见。

## 5. 验证

```bash
# 1) 探针：打印真实响应结构，用来校准字段名
python -m app.services.langfuse_service

# 2) 打点验证：重启后端后随便聊一句，看 Langfuse UI 有没有 trace
# 3) 面板验证：http://127.0.0.1:8765/#/observability 走 /api/obs/* 读 Langfuse
```

## 6. 实现要点（为什么这么写）

`app/services/langfuse_service.py` 走 **`GET /api/public/v2/observations`**，不用
`/api/public/traces` 和 `/api/public/sessions`——后者已废弃（Cloud 服务至 2026-11-16，
v4 上 sessions 直接 404）。

新模型下没有「会话」实体，也没有 trace 级 input/output：

- trace = 共享同一 `traceId` 的 observation 集合；根 observation = `parentObservationId` 为空
- session = 共享同一 `sessionId` 的 observation 集合
- 轮次 = 会话内 trace 按 `startTime` 排序后的序号

**排序必须用解析后的时间戳，不能用字符串比较**：`'...00Z'` 与 `'...00.5Z'` 逐字符比时
`Z`(0x5A) > `.`(0x2E)，秒级时间会排到小数秒之后，根 span 落到子 span 后面，组树时断成多个根。
Phoenix 的时间格式恰好统一带小数位，所以这个坑只在 Langfuse 暴露。

单位差异：Langfuse 的 `latency` 是**秒**，面板统一毫秒；`usage` 结构在不同小版本里是
`input/output` 或 `promptTokens/completionTokens`，取值一律多候选兜底。

## 7. 回滚

```dotenv
OBS_BACKEND=phoenix
TRACING_ENDPOINT=http://127.0.0.1:6006/v1/traces
```

面板立即回到 Phoenix 数据源，无需改代码。两边可同时收数据（OTel 支持多 exporter，
当前实现是单 exporter，需要双写再扩）。
