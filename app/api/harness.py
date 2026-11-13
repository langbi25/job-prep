"""Harness 管理台接口：工具目录、技能编辑、路由试跑、体检、调用台账。

写接口只有四类：改技能正文、技能启停、单工具启停、回滚备份。
**没有"新建工具"的接口**：工具是代码（schema + 实现 + 权限），不是配置；
能在这加一个工具，就意味着运行时能凭空长出一个能改库的入口。工具只能经代码评审进来。
单工具启停不违反这条：它只关掉已有能力，不制造新能力。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import tool_flags
from ..services import harness_admin as ha

router = APIRouter(prefix="/api/harness", tags=["harness"])


class SkillBody(BaseModel):
    text: str
    message: str | None = None      # 这次改了什么，写进版本历史


class ToggleBody(BaseModel):
    enabled: bool = True


class ToolToggleBody(BaseModel):
    enabled: bool = True
    reason: str | None = None      # 为什么停用，写在面板和台账里给人看


class RollbackBody(BaseModel):
    file: str


class RevertBody(BaseModel):
    version: int
    message: str | None = None


class RouteBody(BaseModel):
    query: str
    hint: str | None = None


@router.get("/board")
def board(days: int = 1, limit: int = 30):
    """整页数据一次拿全：一个连接、一份快照。

    前端原来并发 5 个接口，服务端每个都在重算同一份技能/工具目录
    （本机每次 get_conn() 约 8ms，7 个连接就是 50ms 的纯开销）。
    """
    return ha.board(days, call_limit=min(limit, 200))


@router.get("/overview")
def overview(days: int = 1):
    """面板顶部：工具/技能计数、24h 调用与失败率、健康分、四角色工具权限。"""
    return ha.overview(days)


@router.get("/tools")
def tools(days: int = 14):
    return {"tools": ha.tool_entries(days), "days": days,
            "flags": tool_flags.snapshot()}


@router.post("/tools/{name}/toggle")
def tool_toggle(name: str, body: ToolToggleBody):
    """停用 / 启用单个工具。

    三层同时生效，缺一层"停用"就只是显示效果：
    1. 工具清单（模型看不见）；2. 技能白名单过滤（技能限不住它）；
    3. 执行层闸门（历史消息里的 tool_calls、编排的 ROLE_TOOLS 走这条路被拒，
       并在 tool_calls 台账留一条 error_kind='disabled'）。

    affected_skills 一并返回：停用一个工具最直接的后果是"有技能从此必然验收失败"，
    管理员应该在按下开关的那一刻就知道会波及谁，而不是等下次跑技能才发现。
    """
    names = {(t.get("function") or {}).get("name") for t in ha._all_tools_raw()[0]}
    if name not in names:
        raise HTTPException(404, f"工具不存在: {name}")
    try:
        res = tool_flags.set_enabled(name, body.enabled, body.reason or "")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    affected = sorted({r["skill"] for r in ha._skill_tool_refs().get(name, [])})
    return {**res, "affected_skills": affected}


@router.get("/skills")
def skills():
    return {"skills": ha.skill_entries()}


@router.get("/skills/{name}")
def skill_detail(name: str):
    try:
        return ha.read_skill(name)
    except KeyError:
        raise HTTPException(404, f"技能不存在: {name}") from None


@router.put("/skills/{name}")
def skill_save(name: str, body: SkillBody):
    """保存技能正文。校验有 error 时返回 200 + ok=false + errors 明细。

    这里故意不用 4xx：前端要把"哪一条不合法"渲染在编辑器旁边，
    而 4xx 会被拦截器统一弹成一条 toast，明细就丢了。
    """
    try:
        return ha.save_skill_text(name, body.text, message=body.message)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/skills/{name}/toggle")
def skill_toggle(name: str, body: ToggleBody):
    try:
        return ha.set_enabled(name, body.enabled)
    except KeyError:
        raise HTTPException(404, f"技能不存在: {name}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/skills/{name}/versions")
def skill_versions(name: str):
    """技能的提交历史（不含正文）+ 磁盘内容是否已被平台记录。"""
    try:
        return ha.version_board(name)
    except KeyError:
        raise HTTPException(404, f"技能不存在: {name}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/skills/{name}/diff")
def skill_diff(name: str, a: int, b: int):
    """两个版本的行级差异。a/b 是版本号，不是备份文件名。"""
    try:
        return ha.diff_versions(name, a, b)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/skills/{name}/revert")
def skill_revert(name: str, body: RevertBody):
    """回滚到某个历史版本——回滚本身会记成一个新版本，不是改写历史。"""
    try:
        return ha.revert_to(name, body.version, body.message)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/skills/{name}/backups")
def skill_backups(name: str):
    return {"backups": ha.backups(name)}


@router.post("/skills/{name}/rollback")
def skill_rollback(name: str, body: RollbackBody):
    try:
        return ha.rollback(name, body.file)
    except KeyError:
        raise HTTPException(404, f"备份不存在: {body.file}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/route")
def route(body: RouteBody):
    try:
        return ha.route_preview(body.query, body.hint)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/lint")
def lint():
    return ha.lint_all()


@router.get("/calls")
def calls(limit: int = 50, tool: str | None = None, status: str | None = None):
    return {"calls": ha.recent_calls(limit, tool, status),
            "daily": ha.daily_calls()}
