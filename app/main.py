import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import (agent, agent_actions, cards, console, flywheel, harness, ingests,
                  kb_tree, mcp_board, mock, observability, plans, rag_board,
                  retrospectives, search, stats, srs, traces, turns)
from .core import config
from .core.config import ensure_dirs
from .core.db import init_db
from .services.syncer import sync_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    adopted = sync_all()
    print(f"[startup] db ready, syncer adopted {adopted} md files")
    # 技能版本基线：没进版本管理的技能补一个 v1。不补的话它们的运行记录只能留
    # skill_version=NULL，"这个技能跑过 15 次"和版本历史对不上，改坏了也查不出来。
    try:
        from .services.harness_admin import ensure_baseline_versions
        base = ensure_baseline_versions()
        if base["count"]:
            print(f"[startup] skill version baseline: {base['count']} 个技能补了 v1")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] skill version baseline skipped: {e}")
    # 阶段 5：懒建立向量索引（fastembed 未安装时优雅降级，不阻塞启动）
    try:
        from .services.rag_service import ensure_all_indexed
        idx = ensure_all_indexed()
        print(f"[startup] vector index: {idx}")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] vector index skipped: {e}")
    # span 分级清理：普通 span 留 14 天，error 与慢调用留 3 倍（诊断证据不能和
    # 噪音一起清掉）；行数超保险丝时再从最老的开始删，跳过那两类。
    # 放在启动时做而不是定时任务：单机部署没有调度器，启动一次清一次够了。
    try:
        from .observability import tracing as _tr
        gone = _tr.prune_spans()
        if gone:
            print(f"[startup] spans pruned: {gone} 条（普通 {_tr.SPAN_RETENTION_DAYS} 天 / "
                  f"错误与慢调用 {_tr.SPAN_RETENTION_DAYS * 3} 天）")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] span prune skipped: {e}")
    yield


app = FastAPI(title="Job Prep KB", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # 从 "*" 收紧到白名单：前端与后端同源，开发走 vite proxy，本就不需要任意来源
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 鉴权 + 审计（差距清单"鉴权+审计"项）。APP_TOKEN 为空时自动退化为只写审计，
# 本地脚本/测试零行为差异。挂在 CORS 之外（最后添加的最外层），顺序无实际影响：
# 两者只拦 /api，互不依赖对方的头。
from .core.guard import GuardMiddleware  # noqa: E402
app.add_middleware(GuardMiddleware)

app.include_router(cards.router)
app.include_router(ingests.router)
app.include_router(stats.router)
app.include_router(mock.router)
app.include_router(srs.router)
app.include_router(agent.router)
app.include_router(agent_actions.router)
app.include_router(search.router)
app.include_router(flywheel.router)
app.include_router(plans.router)
app.include_router(console.router)
app.include_router(rag_board.router)
app.include_router(kb_tree.router)
app.include_router(mcp_board.router)
app.include_router(retrospectives.router)
app.include_router(observability.router)
app.include_router(turns.router)
app.include_router(traces.router)
app.include_router(harness.router)


@app.get("/health")
def health():
    return {"ok": True}


# Vue3 前端（frontend/dist 构建产物）。hash 路由，StaticFiles 托管即可，无需 SPA fallback。
# 挂在所有 API 路由之后：/api/* 与 /health 不受影响。
from fastapi.staticfiles import StaticFiles  # noqa: E402

_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
else:
    print(f"[startup] frontend dist not found ({_DIST}), UI served by Streamlit only")


@app.get("/ingest")
def bookmarklet_landing(url: str = "", sel: str = ""):
    """bookmarklet 落地提示：告知把内容带到 Streamlit 采集台。"""
    return {
        "message": "请在采集台完成投喂（Streamlit 页面）",
        "url": url,
        "selection_chars": len(sel),
        "tip": "Streamlit 采集台已预填该 URL，粘贴或选中文字即投喂",
    }
