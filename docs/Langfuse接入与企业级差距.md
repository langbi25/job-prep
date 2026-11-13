# Langfuse 接入与企业级差距评估

日期：2026-11-13。结论先行：**接入 Langfuse 是配置和运维动作，不是代码重构**——
代码侧的口子（OTLP 导出、数据源切换、读适配器）全部已经就位。
真正要做的决定只有两个设计问题；而"企业级"的差距不在可观测，在鉴权、数据韧性和交付自动化。

---

## 一、接入 Langfuse：需要调整什么

### 1.1 已经就位、不需要动的部分

| 环节 | 现状 | 位置 |
|---|---|---|
| OTLP 导出 | `configure()` 支持 `TRACING_ENDPOINT` + `TRACING_HEADERS`，BatchSpanProcessor 批量 5s（可 `TRACING_FLUSH_MS` 调），已绕系统代理 | `app/observability/tracing.py` |
| 数据源切换 | `OBS_BACKEND=phoenix\|langfuse`，`/api/observability/*` 自动路由到对应 service | `app/api/observability.py` |
| Langfuse 读适配器 | v2 observations API 拉扁平列表、自己重建树、kind 翻译表、字段多候选兜底 | `app/services/langfuse_service.py` |
| 本地事实源 | `spans`/`span_blobs` 照常双写，Langfuse 挂了业务与本地面板完全无感 | `app/observability/tracing.py::_persist` |

### 1.2 要新增的东西

**① 独立的 Langfuse compose（不放业务 compose 里）。**
理由：生命周期必须独立——重启业务 API 不该连观测后端一起重启，
反之亦然。新建 `deploy/langfuse/docker-compose.yml`：
langfuse-server（web+worker）+ postgres + clickhouse + minio，
各自独立 named volume。预计约 3GB 内存、2GB 镜像磁盘。

**② `.env` 增加五个变量：**

```
TRACING_ENDPOINT=http://langfuse:3000/api/public/otel
TRACING_HEADERS=Authorization=Basic <base64(pk-lf-...:sk-lf-...)>
OBS_BACKEND=langfuse
LANGFUSE_URL=http://langfuse:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

注意两点：Basic auth 的凭据是 `public_key:secret_key` 整体 base64；
URL 用 compose 服务名 `langfuse`（同一 Docker 网络内），不是 localhost。

**③ 业务 compose 只改一处**：api 服务加入 Langfuse 所在的外部网络
（`networks: [obs]`，external: true），其余不动。

### 1.3 两个必须先想清楚的设计决策

**决策 A：全文归属哪里。**
本地 `span_blobs` 有 gzip 全文；OTel 导出的属性只有 800 字预览 + 长度标记。
也就是说 **Langfuse 里看不到完整 prompt**。两个选项：

- 接受现状（推荐）：全文是敏感面（简历原文），只留在本地，Langfuse 作为
  分析层天然低敏。跨设备排查时用本地面板补全文。
- 把全文也发过去：需要在导出前把 blob 挂回 OTel 属性，代价是网络流量、
  Langfuse 存储（ClickHouse 对大文本不友好）和数据面扩大。不值得。

**决策 B：写入失败的语义确认。**
BatchSpanProcessor 导出失败只打日志、不重试上抛——这是**想要的**行为
（观测挂了不能影响业务），但意味着 Langfuse 里的数据可能悄悄缺一截。
验收时用"本地 spans 行数 vs Langfuse traces 数"对账，别拿 Langfuse 当完整事实源。

### 1.4 验收清单

1. `docker compose -f deploy/langfuse/docker-compose.yml up -d` 后 `:3000` 可登录；
2. 跑一轮真实对话，Langfuse trace 数 +1，span 树与本地 Traces.vue 结构一致；
3. `GET /api/observability/backend` 返回 langfuse 且 reachable=true；
4. 停掉 Langfuse 容器，业务对话不受影响，本地 span 照常落库；
5. 重启 Langfuse，恢复后的新 trace 正常出现（丢失窗口期的 trace 不补，接受）。

---

## 二、距离企业级 Agent 还有多远

先定义尺度：**个人生产级**（单人天天用、坏了能修、数据不丢）已经接近；
**企业级**（多人用、出事有人管、审计说得清）差三根硬骨头。逐维度看：

| 维度 | 现状 | 企业级要求 | 差距与是否值得补 |
|---|---|---|---|
| 可观测 | 本地埋点+全文+树+分级保留；Langfuse/Phoenix 分析层；**✅ 告警已完成（2026-11-13 晚）**：错误率/慢调用超阈值 webhook 推送（`app/observability/alerts.py`，本地 spans 判定，冷却查表，没送达也落库） | 告警、SLO、跨服务追踪 | 剩余：SLO/跨服务追踪（单服务不需要）。此维度封顶 |
| 安全/鉴权 | **✅ 已完成（2026-11-13）**：`APP_TOKEN` 静态令牌拦 `/api/*`（`app/core/guard.py`），CORS 收紧白名单；`audit_log` 表记写操作与 401，查询接口 `/api/obs/audit`。浏览器弹框输一次，脚本用 X-API-Token | 鉴权、最小权限、操作审计 | 剩余：无 HTTPS（内网可接受）；actor 仍是单身份，多人时需要升级为多 token/会话 |
| 数据韧性 | **✅ 已完成（2026-11-13）**：`backup` sidecar 每天 02:00 在线备份（SQLite backup API，不停服），落宿主 `backups/` 保留 7 份，含 integrity_check 与恢复演练。仍是单库单机，无 PITR | 自动备份、可恢复演练、PITR | 剩余只有 PITR 与异地副本——个人项目不必 |
| 流量治理 | **✅ LLM 并发池已完成（2026-11-13 晚）**：`llm.py` 全局 BoundedSemaphore（LLM_MAX_CONCURRENCY 默认 4）+ 排队超时快速失败（LLMConcurrencyError，retryable 同 429） | 限流、超时、降级 | LLM 上游治理完成；API 层限流个人使用不需要 |
| 交付自动化 | **✅ 已完成（2026-11-13）**：`ci.yml` 跑全量 291 用例 + 前端构建，`evals.yml` 管线由 LLM key 控制；`requirements-dev.txt` 显式声明测试依赖 | CI 门禁、镜像自动构建 | 剩余：镜像未做 CI 构建与推送（本地 compose build 够用） |
| 质量闭环 | 评测集 77 例、回归、归因、双写 Phoenix；**✅ 自动进料已完成（2026-11-13 晚）**：点踩/失败轮自动进 data/eval_intake.jsonl，`promote_eval_cases.py` 提升入 evals；**✅ 任务级评测扩容 2→12 条**（任务文本已进 prompt，基线 12/12） | 发布前自动门禁、会话自动沉淀 | 剩余：素材→golden 的**人工标注**是流程不是代码；发布门禁串联等真有发布节奏再说 |
| 多租户/权限 | 无 | 租户隔离、RBAC | 单人项目**不该补**，补了是负资产 |
| 成本治理 | `pricing.py` 按调用算成本 | 预算、聚合报表 | Langfuse 接入后白拿聚合视图，够用 |

### 差距的真实度量

企业级的三根硬骨头，按补齐顺序：

1. ~~**鉴权 + 审计**（1-2 天）~~——**已完成（2026-11-13）**：token 中间件 + 审计表 + 前端弹框 + CORS 白名单，9 个测试用例，容器实测 401/200/审计落表全过；
2. ~~**自动备份**（半天）~~——**已完成（2026-11-13）**：`backup` sidecar 每日 02:00 在线备份，保留 7 份，integrity_check + 恢复演练都跑过；
3. ~~**CI 门禁**（1 天）~~——**已完成（2026-11-13）**：`ci.yml` 跑 291 用例与前端构建，测试依赖用 `requirements-dev.txt` 显式声明。**注意只有 push 到远端才生效**。

三根骨头都拔了，状态到"小团队生产级"；PG、限流、多租户那些，触发信号到了再补，
现在补属于给单人项目上企业门票，钱花在没人检票的地方。

### 与 Langfuse 的关系

Langfuse 补的是**分析维度**（跨设备 UI、标注、成本聚合、prompt 管理），
不解决上面任何一根骨头。别把它当成企业化的捷径——它接入后，
可观测这个本来已经超配的维度会更加超配，而鉴权仍然是零。
