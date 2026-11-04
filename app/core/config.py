import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
KB_DIR = BASE_DIR / "knowledge"
IV_DIR = BASE_DIR / "interviews"
INBOX_DIR = BASE_DIR / "inbox"
TPL_DIR = BASE_DIR / "templates"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "kb.sqlite"

DOMAINS = ("backend", "agent", "bigdata", "cs")
DIRECTIONS = ("backend", "agent", "bigdata")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

APP_PORT = int(os.getenv("APP_PORT", "8765"))

# ---- 上下文预算（app/context）----
# 窗口取模型侧真实值；软上限是"成本/延迟"的自我约束——能塞 64K 不代表该塞 64K。
CTX_WINDOW = int(os.getenv("CTX_WINDOW", "64000"))       # deepseek-chat：64K 上下文 / 8K 输出
CTX_SOFT_CAP = int(os.getenv("CTX_SOFT_CAP", "12000"))   # 单次输入不超这个数（超出降级）
CTX_OUTPUT_RESERVE = int(os.getenv("CTX_OUTPUT_RESERVE", "1024"))
CTX_SAFETY = int(os.getenv("CTX_SAFETY", "512"))
CTX_RECENT_TURNS = int(os.getenv("CTX_RECENT_TURNS", "6"))   # 最近 N 轮永久保留
CTX_RETRIEVE_TOPK = int(os.getenv("CTX_RETRIEVE_TOPK", "3"))


def ensure_dirs():
    for d in (DATA_DIR, KB_DIR, IV_DIR, INBOX_DIR, INBOX_DIR / "pending", INBOX_DIR / "archived", REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
