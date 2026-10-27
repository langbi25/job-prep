import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import agent, agent_actions, cards, ingests, mock, search, stats, srs
from .core.config import ensure_dirs
from .core.db import init_db
from .services.syncer import sync_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    adopted = sync_all()
    print(f"[startup] db ready, syncer adopted {adopted} md files")
    # 阶段 5：懒建立向量索引（fastembed 未安装时优雅降级，不阻塞启动）
    try:
        from .services.rag_service import ensure_all_indexed
        idx = ensure_all_indexed()
        print(f"[startup] vector index: {idx}")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] vector index skipped: {e}")
    yield


app = FastAPI(title="Job Prep KB", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 个人内网使用
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cards.router)
app.include_router(ingests.router)
app.include_router(stats.router)
app.include_router(mock.router)
app.include_router(srs.router)
app.include_router(agent.router)
app.include_router(agent_actions.router)
app.include_router(search.router)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ingest")
def bookmarklet_landing(url: str = "", sel: str = ""):
    """bookmarklet 落地提示：告知把内容带到 Streamlit 采集台。"""
    return {
        "message": "请在采集台完成投喂（Streamlit 页面）",
        "url": url,
        "selection_chars": len(sel),
        "tip": "Streamlit 采集台已预填该 URL，粘贴或选中文字即投喂",
    }
