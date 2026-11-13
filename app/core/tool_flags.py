"""单工具启停闸门。

**为什么开关放在执行层，而不是只从工具清单里摘掉**：
工具清单是"告诉模型有什么"。摘掉只是让它看不见，不是让它不能用——历史消息里的
tool_calls、组合技能的白名单、编排的 ROLE_TOOLS 都还可能点名它。停用一个工具
往往是因为它正在出错或正在改坏数据，这时候"看不见"远远不够，必须"调不动"，
并且要留下一条"停用期间谁还调过它"的记录。

**为什么用 JSON 文件而不是建表**：
这是平台级开关（一个实例一份），量级在个位数到几十条，还要能直接看、直接改、
跟进 git。为此加一张表 + 一次迁移 + 一个管理接口，成本大于收益。
反过来，技能启停是写在技能 md 的 frontmatter 里的——那是"技能自己的属性"，
工具启停是"平台的策略"，所以分开放。

存储的语义是**只记被停用的**：文件为空 = 一切正常。这样"恢复默认"就是删一条，
也不会出现"文件里 60 个工具全写着 enabled:true，实际新增的工具默认是什么"的疑问。
"""
import datetime
import json
import os
import threading

from .config import BASE_DIR

FLAGS_PATH = BASE_DIR / "data" / "tool_flags.json"
_lock = threading.Lock()


def _read() -> dict:
    try:
        with open(FLAGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}          # 文件坏了按"全部启用"处理，不能因为开关坏了把工具全锁死


def _write(data: dict) -> None:
    FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(FLAGS_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FLAGS_PATH)      # 原子替换：半截文件会被读成"全部启用"


def all_flags() -> dict:
    """{tool: {enabled: False, reason, at}}，只含被停用的。"""
    return _read()


def is_enabled(name: str) -> bool:
    return name not in _read()


def blocked_reason(name: str) -> str | None:
    """被停用时返回原因（可能为空串），启用时返回 None。

    dispatch 用它做闸门：返回 None 才继续执行。
    """
    item = _read().get(name)
    if item is None:
        return None
    if item.get("enabled", False):
        return None            # 兼容手工写成 enabled:true 的文件
    return item.get("reason") or ""


def set_enabled(name: str, enabled: bool, reason: str = "") -> dict:
    """开关一个工具。返回落盘后的这一条状态。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("工具名不能为空")
    with _lock:
        data = _read()
        if enabled:
            data.pop(name, None)          # 启用 = 从文件里消失，回归默认
        else:
            data[name] = {"enabled": False,
                          "reason": (reason or "").strip()[:200] or "平台停用",
                          "at": datetime.datetime.now().isoformat(timespec="seconds")}
        _write(data)
    return {"name": name, "enabled": enabled, "reason": (reason or "").strip()[:200]}


def snapshot() -> dict:
    """给面板用：停用总数 + 明细。"""
    data = _read()
    return {"disabled_count": len(data),
            "disabled": [{"name": k, **v} for k, v in sorted(data.items())]}
