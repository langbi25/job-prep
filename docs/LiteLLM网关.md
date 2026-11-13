# LiteLLM 网关

> 2026-11-14 接入。背景：第十三节之前的项目决策是"只有一个上游，不做 LLM 网关"
> （见《Agent能力补强》14 节前的盘点）。后来明确要做——理由不再是"必须多上游"，
> 而是**模型路由是会发生的**（reasoner 处理硬任务、评测换模型对照、上游涨价/限流时
> 一处切换），与其每次都改代码，不如把切换点收进一份 YAML。

## 1. 接法：代理网关，不是 SDK

LiteLLM 有两种用法，选了**代理（网关服务）**：

| | SDK（库） | 代理（网关容器，本项目选择） |
|---|---|---|
| 应用改动 | `llm.py` 全部重写 | **零改动**（本来就讲 OpenAI 协议） |
| 流式 TTFT / tool_calls 聚合 | 要在 SDK 回调上重做 | 保留（应用侧自己聚合，网关只是管道） |
| 换/加模型 | 改代码 | 改 `litellm/config.yaml` |
| 多一个常驻进程 | 无 | 有（约 300MB 内存，2C2G 上是真实代价） |

核心判断：`app/core/llm.py` 已经是一套完整的 OpenAI 协议客户端——流式聚合、
错误语义（`LLMError.retryable`）、并发池、预算闸门、成本台账。换 SDK 等于推倒重来，
还要重学它的错误语义；网关对它只是一个"更聪明的上游"。

## 2. 职责边界：网关只管路由，记账不外移

- **成本台账 / 日预算 / 并发池留在应用本地**（`pricing.py` / `budget.py` /
  `llm.py` 的池）。网关的 spend tracking 需要背一个 Postgres，单机不值；
  而且台账必须和 span / 评测 / 归因在同一张 `llm_usage` 表里才查得动。
- **网关层重试只兜网络抖动和 429/5xx**（`num_retries: 2`）。语义错误（400）
  透传给应用：应用侧 `retryable` 判定建立在"同一 body 重试必然同样失败"上，
  网关乱重试会把一次必败请求变成三次必败请求。
- `request_timeout: 90` 与应用侧 httpx 对齐——别让网关比客户端先放弃。

## 3. 文件与流量路径

```
litellm/config.yaml        模型清单：deepseek-chat + deepseek-reasoner（备用）
docker-compose.yml         litellm 服务（默认启动，api/mcp depends_on healthy）
.env                       LLM_BASE_URL=http://127.0.0.1:4000/v1（宿主视角）
                           LLM_API_KEY=<网关门票>
                           LITELLM_MASTER_KEY / DEEPSEEK_API_KEY（上游凭据）
```

```
应用容器 ──http://litellm:4000/v1──> LiteLLM ──deepseek/deepseek-chat──> api.deepseek.com
本地uvicorn ──http://127.0.0.1:4000/v1──┘
```

容器内地址由 compose 的 `environment: LLM_BASE_URL` 覆盖（容器里 127.0.0.1
指容器自己，不通）；`.env` 留宿主视角，本地直跑 uvicorn 也能连网关。

安全：真实 DeepSeek key 只在 `.env` 的 `DEEPSEEK_API_KEY` 出现一次，由网关持有；
应用侧拿到的是网关门票（master key）。宿主端口绑 `127.0.0.1`，外部看不到。

## 4. 验证清单（接入时逐项跑过）

```bash
# 1. 网关活着
curl -s http://127.0.0.1:4000/health/liveliness

# 2. 非流式 + 鉴权（错 key 必须 401）
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"回复ok"}]}'

# 3. 流式 + usage 透传（最后一个 chunk 必须带 usage，否则本地台账 token 记 0）
curl -sN ... -d '{"model":"deepseek-chat","stream":true,
  "stream_options":{"include_usage":true}, ...}' | grep usage

# 4. json_object 透传（DeepSeek 的"prompt 必须含 json"约束原样生效）
# 5. 端到端：容器里跑一次真实调用，llm_usage 表落账、cost_cny 正常
python -c "from app.core import llm; print(llm.chat_parsed('输出JSON','{\"ok\":true}'))"
```

其中第 3 项是接入时唯一担心的点：应用台账依赖流式末 chunk 的真实 usage
（`stream_options.include_usage`）。验证通过后本地记账不受网关影响。

## 5. 切模型 / 加供应商 / 回退

- **切模型**：改 `.env` 的 `LLM_MODEL`（清单里已有 `deepseek-reasoner`）。
  硬任务（编排打分、归因）以后值得单独路由，届时在调用点传模型名即可。
- **加供应商**：`config.yaml` 的 `model_list` 加一项；fallback（主模型 5xx 时
  自动切备用）也是这个文件里的 `fallbacks` 配置，目前只有一个上游，配了没有意义。
- **回退直连**：删 compose 的 litellm 段 + api/mcp 的 `depends_on` 与
  `LLM_BASE_URL` 覆盖，`.env` 换回注释里的直连两行。这就是当初"应用零改动"
  换来的退路。

## 6. 踩坑记录

- **官方 ghcr 镜像拉不动**：`ghcr.io/berriai/litellm:main-stable`，token/manifest
  接口 curl 可达（~1-2s），但 daemon 层下载 10 分钟停滞；`ghcr.m.daocloud.io`
  同样卡在 manifest 阶段。最终**放弃官方镜像，改 pip 装进应用镜像**：requirements.txt
  加 `litellm>=1.74`（阿里云源下载稳定），compose 服务与 api 共用
  `job-prep-api:latest`，命令换 `litellm --config ... --port 4000`。代价是
  镜像大 ~300MB，换来一个镜像两个用途（api + 网关）。
- **compose 探测不到缺失变量**：`LITELLM_MASTER_KEY: ${...:?...}` 让缺失时
  compose 直接拒绝启动并打印原因，与 MCP_TOKEN 的"宁可起不来，不要静默裸奔"同一原则。
