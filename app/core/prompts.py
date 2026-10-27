"""Prompt 注册表：YAML 版本化 + 变量渲染 + 输出 schema 声明。

用法：system, user, version = prompts.render("evaluate_answer", persona=..., ...)
所有 LLM 调用的 prompt 都从 prompts/*.yaml 读取，禁止硬编码——
prompt 是这个项目的"源代码"，改 prompt = 改逻辑，必须可版本化、可回溯。
"""
import functools
from pathlib import Path

import yaml

from .config import BASE_DIR

PROMPTS_DIR = BASE_DIR / "prompts"
REGISTRY_VERSION = "2026-10-27"


class Prompt:
    def __init__(self, name: str, data: dict, path: Path):
        for key in ("name", "version", "system", "user"):
            if key not in data:
                raise ValueError(f"prompt {path.name} 缺少字段 {key}")
        self.name = name
        self.version = int(data["version"])
        self.description = data.get("description", "")
        self.system = data["system"]
        self.user = data["user"]
        self.output_schema = data.get("output_schema", {})


@functools.lru_cache(maxsize=32)
def load(name: str) -> Prompt:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise KeyError(f"prompt 不存在: {name}（位于 {PROMPTS_DIR}）")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Prompt(name, data, path)


def render(name: str, **variables) -> tuple[str, str, int]:
    """渲染 prompt，返回 (system, user, version)。缺变量会直接抛 KeyError（fail fast）。"""
    p = load(name)
    return (
        p.system.format(**variables).strip(),
        p.user.format(**variables).strip(),
        p.version,
    )
