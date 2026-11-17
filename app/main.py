import json
import signal
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


def _chaos_crash_hook(_signum, _frame):
    """混沌钩子：SIGUSR1 → 硬崩溃（os._exit，绕过优雅退出）。

    为什么存在：演练"进程突然死亡"必须让 PID1 自己死。两个内核/守护进程的
    限制都是实测得出的（2026-11-17，见 scripts/chaos_drill.py）——
    1) 命名空间内发给 PID1 的 SIGKILL/SIGTERM 默认行为被内核忽略；
    2) `docker kill` 被守护进程视为人为停止，restart 策略根本不触发。
    只有"装了 handler 的信号 + 进程自愿退出"才能走出真实的崩溃→重启路径。
    SIGUSR1 系统无占用，约定为本钩子专用；正常业务永不发送它。
    """
    import os as _os
    _os._exit(137)


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
    # 默认任务注册表（app/core/jobdefs.py）：api 进程注册是为了观测面板与
    # 手动触发按钮。调度执行不在这里——容器部署里调度归 worker 容器
    # （python -m app.worker，api 设 JOBS_ENABLED=0），单进程直跑时下面的
    # start_scheduler 照旧在本进程跑，两种形态共用同一份任务定义。
    try:
        from .core import jobdefs as _jobdefs
        _jobdefs.register_default_jobs()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] job registry skipped: {e}")
    # 通用调度器：表驱动任务（jobs 表）+ 文件锁互斥 + 执行历史（job_runs 表）。
    # JOBS_ENABLED=0 时不启动（compose 里 api 就设 0，调度在 worker 容器）。
    # 文件锁落在 DATA_DIR：容器模式下它与 worker 共享 named volume，
    # 手动触发撞上定时执行时后到的一方记 skipped_locked，不会双跑。
    try:
        from .core import jobs as _jobs
        if _jobs.start_scheduler():
            print(f"[startup] job scheduler on: 每 {_jobs.scan_interval()}s 扫一轮到期任务")
        else:
            print("[startup] job scheduler disabled (JOBS_ENABLED=0)")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] job scheduler skipped: {e}")
    # 可观测告警：错误率/慢调用超阈值推 webhook。未配置 ALERT_WEBHOOK_URL 或
    # ALERT_CHECK_SEC=0 时不启动，零开销。循环在守护线程里，判定失败只打日志。
    try:
        from .observability import alerts as _alerts
        if _alerts.start_background():
            c = _alerts.config()
            print(f"[startup] alerts on: 每 {c['check_sec']}s 检查，"
                  f"错误率阈值 {c['error_rate']:.0%} / 慢调用 {c['slow_ms']}ms×{c['slow_count']}")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] alerts skipped: {e}")
    yield
    try:
        from .observability import alerts as _alerts
        _alerts.stop_background()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .core import jobs as _jobs
        _jobs.stop_scheduler()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="Job Prep KB", version="0.1.0", lifespan=lifespan)
# 平台保护：Windows 没有 SIGUSR1（测试在本机跑会 AttributeError）；非主线程
# 导入时 signal.signal 抛 ValueError。钩子只在 Linux 容器里真正有用。
if hasattr(signal, "SIGUSR1"):
    try:
        signal.signal(signal.SIGUSR1, _chaos_crash_hook)
    except ValueError:
        pass

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
# 限流挂最外层（最后添加 = 最外层）：洪水不该享受"先解析 token 再拒绝"的待遇，
# 鉴权也不该被刷爆。只拦 /api/*，静态资源与 /health 不受影响（见 ratelimit.py）。
from .core.ratelimit import RateLimitMiddleware  # noqa: E402
app.add_middleware(RateLimitMiddleware)

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
