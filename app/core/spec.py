"""业务线 spec 加载器（P5）—— 跨业务通用化的唯一读取入口。

为什么存在：此前「有哪些业务线」散在 6 处硬编码里（config 元组、两份工具 schema 的
enum、kb_tree 的标签映射、API 默认值、eval_suite 的文件路径）。加一条业务线要改
五六个文件，漏一处就静默漂移。现在唯一定义点是 specs/business_lines.yaml，
本模块是唯一读取入口——其他模块不许直接 open 那份 YAML。

设计约束：
- 不 import config（config 反向依赖本模块），避免循环；自己算项目根。
- lru_cache 单次加载；测试改 YAML 后调 reload()。
- validate() 把「spec 声明的」与「数据里实际存在的」对齐做检查，
  供 tests/test_spec.py 与将来 CI 使用——配置漂移在测试期暴露，不在运行期爆炸。
"""
import functools
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC_PATH = ROOT / "specs" / "business_lines.yaml"


@functools.lru_cache(maxsize=1)
def _spec() -> dict:
    data = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("lines"):
        raise ValueError(f"业务线 spec 缺少 lines 定义：{SPEC_PATH}")
    return data


def reload() -> None:
    """测试/热更新用：清缓存，下次读取重新加载 YAML。"""
    _spec.cache_clear()


def _lines_raw() -> dict:
    return _spec()["lines"]


def _defaults() -> dict:
    return _spec().get("defaults") or {}


# ---------- 业务线枚举 ----------

def lines() -> tuple[str, ...]:
    """业务线 id 元组，按 spec 里的 order 排序。"""
    items = sorted(_lines_raw().items(), key=lambda kv: (kv[1].get("order", 99), kv[0]))
    return tuple(k for k, _ in items)


def enum() -> list[str]:
    """工具 schema 用的 JSON enum（list 形态）。"""
    return list(lines())


def is_valid(line_id: str | None) -> bool:
    return bool(line_id) and line_id in _lines_raw()


def default() -> str:
    """API / 工具参数缺省业务线。spec 没配就取第一个。"""
    d = _defaults().get("direction")
    return d if is_valid(d) else lines()[0]


# ---------- 标签 ----------

def labels() -> dict[str, str]:
    return {k: str(v.get("label") or k) for k, v in _lines_raw().items()}


def label(line_id: str, fallback: str | None = None) -> str:
    """业务线中文名。未知 id（如 review 的 cs 域）回退 fallback 或原值。"""
    lab = labels().get(line_id)
    if lab:
        return lab
    return fallback if fallback is not None else line_id


# ---------- 评测文件路径 ----------

def eval_path(name: str, line: str | None = None) -> Path:
    """评测文件绝对路径。解析顺序：业务线覆盖 > defaults.evals。

    line 参数为将来的按线评测预留；现在套件级评测传 None 即可。
    """
    if line and is_valid(line):
        override = (_lines_raw()[line].get("evals") or {}).get(name)
        if override:
            return ROOT / override
    rel = (_defaults().get("evals") or {}).get(name)
    if not rel:
        raise KeyError(f"spec 未声明评测文件 {name!r}，请在 business_lines.yaml defaults.evals 里补上")
    return ROOT / rel


# ---------- 校验 ----------

def validate() -> list[str]:
    """返回问题清单（空列表 = 通过）。检查三类漂移：

    1. spec 自身合法性：order 唯一、label 非空、defaults.direction 已声明
    2. evals 声明的文件真实存在
    3. 评测数据里出现的 direction 已在 spec 声明（反向漂移：数据有、spec 没有）
    """
    problems: list[str] = []
    raw = _lines_raw()
    orders = [v.get("order") for v in raw.values() if v.get("order") is not None]
    if len(orders) != len(set(orders)):
        problems.append(f"order 重复：{orders}")
    for k, v in raw.items():
        if not v.get("label"):
            problems.append(f"业务线 {k} 缺 label")

    d = _defaults().get("direction")
    if d and not is_valid(d):
        problems.append(f"defaults.direction={d!r} 未在 lines 中声明")

    for name, rel in (_defaults().get("evals") or {}).items():
        p = ROOT / rel
        if not p.exists():
            problems.append(f"evals.{name} 指向的文件不存在：{rel}")

    # 评测数据 → spec 反向校验（目前 agent_tasks 是唯一带 direction 字段的评测集）
    tasks_file = (_defaults().get("evals") or {}).get("agent_tasks")
    if tasks_file and (ROOT / tasks_file).exists():
        import json
        for i, line_text in enumerate((ROOT / tasks_file).read_text(encoding="utf-8").splitlines()):
            if not line_text.strip():
                continue
            try:
                case = json.loads(line_text)
            except json.JSONDecodeError:
                problems.append(f"{tasks_file} 第 {i + 1} 行不是合法 JSON")
                continue
            dv = case.get("direction")
            if dv and not is_valid(dv):
                problems.append(f"{tasks_file} 里的 direction={dv!r} 未在 spec 声明")
    return problems
