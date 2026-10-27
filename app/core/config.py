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


def ensure_dirs():
    for d in (DATA_DIR, KB_DIR, IV_DIR, INBOX_DIR, INBOX_DIR / "pending", INBOX_DIR / "archived", REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
