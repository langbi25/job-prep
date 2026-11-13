import os
from pathlib import Path

from dotenv import load_dotenv

from .spec import default as _spec_default, lines as _spec_lines

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
KB_DIR = BASE_DIR / "knowledge"
IV_DIR = BASE_DIR / "interviews"
INBOX_DIR = BASE_DIR / "inbox"
TPL_DIR = BASE_DIR / "templates"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "kb.sqlite"

# 业务线定义的唯一定义点在 specs/business_lines.yaml（P5），此处只是导入期快照。
# "cs" 是通用计算机基础域（review/薄弱主题归属用），不是业务线，保留在 DOMAINS。
DIRECTIONS = _spec_lines()
DOMAINS = DIRECTIONS + ("cs",)
DEFAULT_DIRECTION = _spec_default()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

APP_PORT = int(os.getenv("APP_PORT", "8765"))

# ---- API 鉴权 + 审计（app/core/guard.py）----
# 静态 token，拦 /api/*；空 = 关闭（本地脚本/测试零开销，行为与之前完全一致）。
# 放行 /health（容器 healthcheck）、前端静态页、/docs——页面没有数据，数据都在 /api 后面。
# 多端如何带 token：浏览器（前端自动）、curl/脚本用 X-API-Token 或 Authorization: Bearer。
# MCP 不走这里：stdio 靠进程边界免鉴权，HTTP 侧沿用它自己的 MCP_TOKEN。
APP_TOKEN = os.getenv("APP_TOKEN", "").strip()
# 收紧 CORS：前端与后端同源（8765），开发时 vite 5173 经 proxy 也不跨源；
# 列白名单只为以后万一有别的页面直连 API 的场景，不再允许任意来源。
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:8765,http://localhost:8765,http://127.0.0.1:5173,http://localhost:5173"
).split(",") if o.strip()]

# ---- harness 的工具来源 ----
# local：进程内直调 _IMPL（默认，快，评测/批量跑用）
# mcp  ：走 MCP stdio（tools/list + tools/call，和 Claude Desktop 完全同一条路径）
# 只作为**验证与演示**开关：生产同时维护两套工具来源就是分叉的开始，
# 所以默认 local，切换靠环境变量，代码里只有一个循环。
HARNESS_TOOL_SOURCE = os.getenv("HARNESS_TOOL_SOURCE", "local").strip().lower()

# ---- Trace 采集（OTel / OpenInference，见 app/observability/tracing.py）----
# 空 = 完全关闭（评测/测试零开销）。配了就自动补 /v1/traces：
#   Phoenix  ：http://localhost:6006
#   Langfuse ：http://localhost:3000/api/public/otel  （TRACING_HEADERS 设 Basic 凭据）
TRACING_ENDPOINT = os.getenv("TRACING_ENDPOINT", "").strip()
TRACING_HEADERS = os.getenv("TRACING_HEADERS", "").strip()   # "k=v;k2=v2"，Langfuse 用 Basic auth
TRACING_SERVICE = os.getenv("TRACING_SERVICE", "job-prep")

# ---- 上下文预算（app/context）----
# 窗口取模型侧真实值；软上限是"成本/延迟"的自我约束——能塞 64K 不代表该塞 64K。
CTX_WINDOW = int(os.getenv("CTX_WINDOW", "64000"))       # deepseek-chat：64K 上下文 / 8K 输出
CTX_SOFT_CAP = int(os.getenv("CTX_SOFT_CAP", "12000"))   # 单次输入不超这个数（超出降级）
CTX_OUTPUT_RESERVE = int(os.getenv("CTX_OUTPUT_RESERVE", "1024"))
CTX_SAFETY = int(os.getenv("CTX_SAFETY", "512"))
CTX_RECENT_TURNS = int(os.getenv("CTX_RECENT_TURNS", "6"))   # 最近 N 轮永久保留
CTX_RETRIEVE_TOPK = int(os.getenv("CTX_RETRIEVE_TOPK", "3"))

# ---- 长期记忆（app/memory）----
# 抽取每轮多一次小 LLM 调用（异步，不阻塞响应）。批跑评测/测试时关掉，省 token 也省不确定性。
MEMORY_EXTRACT = os.getenv("MEMORY_EXTRACT", "1") != "0"
MEMORY_RECALL_TOPK = int(os.getenv("MEMORY_RECALL_TOPK", "6"))
# 记忆召回的向量通道：auto = 装了 fastembed 就用（模型下载失败自动降级到纯词面）；
# 1 = 强制开（不可用也尝试，失败降级）；0 = 关（评测/离线环境，省掉一次 embedding）。
MEMORY_VECTOR = os.getenv("MEMORY_VECTOR", "auto").strip().lower()

# ---- 成本与并发闸门（app/core/budget.py）----
# 日预算：当天 llm_usage 折算总额的硬上限。0 = 不限。
# 默认给 5 元：个人复习场景一天 100 轮对话约 1 元，5 元足够宽裕；但一旦出现
# "编排里某步循环重试""批量建卡跑飞"这类事故，它能在变成几十块之前截住。
LLM_DAILY_BUDGET_CNY = float(os.getenv("LLM_DAILY_BUDGET_CNY", "5"))
# 同时进行的重任务数（对话轮 / 批量建卡 / 编排步骤）。2C2G 单机上这是硬约束：
# 每个任务一个线程 + 一个 SQLite 连接 + 一个在飞的 LLM 请求，超了就是互相拖慢。
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))


def ensure_dirs():
    for d in (DATA_DIR, KB_DIR, IV_DIR, INBOX_DIR, INBOX_DIR / "pending", INBOX_DIR / "archived", REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
