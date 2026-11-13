# Langfuse 自托管接入

> 状态：**适配层已完成并通过测试（55 项全绿），代码侧就绪，等 Docker 引擎可用来联调**
> 当前唯一阻塞：`wsl.exe` 在安全策略黑名单里 → Docker Desktop 的 wsl2 引擎起不来

## 0. 先搞清楚定位

Phoenix 和 Langfuse 是**平级的两个 OTel 后端**，不是上下游：

```
业务代码 → OTel SDK（进程内唯一一份） → OTLP/HTTP → [ Phoenix 或 Langfuse ]
                                                      ↑ 由 TRACING_ENDPOINT 决定
```

打点层不感知后端。切后端 = 改一行 `TRACING_ENDPOINT`；中文面板读哪个后端 = 改一行 `OBS_BACKEND`。

## 1. 部署位置选型（2026-11-12 实测）

| 方案 | 内存 | 结论 |
|---|---|---|
| **本机 Docker Desktop** | 15.6 GB / 12 核 | ✅ 唯一可行，端口 3000/5432/6379/8123/9000/9090 全空闲 |
| 远程 ECS `i-wz9d4bc2o1xqc9jn97jz`（8.129.227.178） | 1.8 GB，已用 1.1 GB，swap 已吃 511 MB | ❌ 跑不动。上面已有 blog / canteen / job-prep 三套服务（8 个容器），再塞 Langfuse 必 OOM 并拖垮在线服务 |
| Langfuse Cloud Hobby | 0 | ✅ 备选，不用本地资源；代价是 trace 里的 prompt 内容出公网 |

远程 ECS 的实测数据（`workbench exec` 采集）：Docker 26.1.3 + Compose v2.27 都有，出口 IP
8.129.227.178，磁盘 40 G（20 G 可用），2 核。**卡在内存**：Langfuse v4 要
postgres + clickhouse + redis + minio + web + worker 六个服务，官方最低 4 GB，实际稳定要 6-8 GB。
如果以后给这台机器升配到 8 GB，直接复用本文的连接方式（把 `127.0.0.1` 换成 `8.129.227.178`，
并在安全组放通 3000），代码无需改动。

## 2. 前置条件（本机只需一步）

| 项 | 现状 | 说明 |
|---|---|---|
| Docker Desktop | ✅ 已装 4.99.0（`E:\Docker`） | — |
| WSL2 | ✅ 已装（`System32\wsl.exe` + Ubuntu20.04LTS 发行版） | — |
| `wsl.exe` 可执行 | ❌ 被黑名单拦截 | **安全中心 → 命令安全 → 程序黑名单 → 移除 `wsl.exe`**，然后重启 Docker Desktop |
| 磁盘 | ✅ | 镜像约 2~3 GB |

判据：`docker info` 能返回 Server 段即为就绪；报 `npipe dockerDesktopLinuxEngine` 不存在就是引擎没起。

## 3. 一键起服务

```bash
bash deploy/langfuse-up.sh            # 生成密钥 + 拉镜像 + 起 6 个服务 + 等就绪 + 打印接入配置
bash deploy/langfuse-up.sh --status   # 容器状态与健康检查
bash deploy/langfuse-up.sh --down     # 停服务（数据卷保留）
bash deploy/langfuse-up.sh --reset    # 清空数据重来（需输入 yes）
```

脚本做掉的事：

1. 自动找 `docker`（PATH 没有时回退 `E:\Docker\resources\bin`），引擎不可达时直接给排查指引
2. 首次运行生成 `deploy/.env.langfuse`（已进 `.gitignore`）：随机 `SALT` / `ENCRYPTION_KEY` /
   `NEXTAUTH_SECRET` / PG / ClickHouse / Redis / MinIO 密码
3. 用 `LANGFUSE_INIT_*` 预置组织 `job-prep`、项目 `job-prep-local`、管理员 `admin@local.dev`、
   以及固定的 `pk-lf-local-*` / `sk-lf-local-*`，**省掉手点 UI 建项目拿 key**
4. 只起核心 6 个容器，跳过 `langfuse-ai-gateway`（本项目不用，省一个容器）
5. 轮询 `/api/public/health` 直到就绪，最后打印可直接粘贴的 `TRACING_*` / `OBS_BACKEND` 片段

首次拉镜像约 5-10 分钟（`docker.langfuse.com` + docker.io + cgr.dev 三处）。

## 4. 配 `.env`

把脚本末尾打印的片段贴进 `job-prep/.env` 即可，等价于：

```dotenv
OBS_BACKEND=langfuse

TRACING_ENDPOINT=http://127.0.0.1:3000/api/public/otel
# base64 = echo -n "pk-lf-xxx:sk-lf-xxx" | base64
TRACING_HEADERS=Authorization=Basic <base64>;x-langfuse-ingestion-version=4
TRACING_FLUSH_MS=500

LANGFUSE_URL=http://127.0.0.1:3000
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
```

`TRACING_ENDPOINT` 不必带 `/v1/traces`，`tracing.py` 见到不以 traces 结尾会自动补。
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
