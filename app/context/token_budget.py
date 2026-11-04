"""Token 计量与预算分配。

**为什么不装 tiktoken**：它算的是 OpenAI 的切分，和 DeepSeek 也不同源——装上也对不准，
还要为"数数"多引一个带 C 扩展的包（单机 2C2G 部署）。所以用可解释的估算式：

    CJK 字 × 0.7 + ASCII 字符 × 0.28 + 空白 × 0.1 + 其他 × 0.5

系数来源不是拍的：DeepSeek 官方口径「1 个中文字符 ≈ 0.6 token、1 个英文字符 ≈ 0.3 token」，
再往上抬一点做保守估计（宁可少喂，别打到 4xx）。

**实测误差**（`scripts/ctx_ab_test.py --calibrate`，与 API 返回的 prompt_tokens 对比）：
三条探针 −34.7% / +11.9% / −7.5%，均值 −10%。短 prompt 偏差大是因为固定开销占比高；
千字级样本稳定在 ±12% 以内。**这个误差不需要修**：软上限 12000 vs 窗口 64000，
10% 的估算误差换不回任何风险，真正起作用的约束是 soft_cap 而不是窗口。

**预算怎么分**：`window - 输出预留 - 安全余量` 是可用的输入预算，再和 `soft_cap` 取小。
soft_cap 是成本和延迟的自我约束——能塞 64K 不代表该塞 64K，长上下文既慢又贵，
还会让模型"看见太多无关内容"而分心。
"""
import math
import os
from dataclasses import dataclass, field

# CJK 统一表意文字 + 中文标点区间
_CJK = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x3000, 0x303F), (0xFF00, 0xFFEF),
)

CJK_PER_CHAR = 0.7
ASCII_PER_CHAR = 0.28
SPACE_PER_CHAR = 0.1
OTHER_PER_CHAR = 0.5
MSG_OVERHEAD = 4          # 每条消息的角色/分隔符开销（OpenAI 系普遍按 3~4 计）


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK)


def count_tokens(text: str | None) -> int:
    """估算一段文本的 token 数（偏保守：向上取整）。"""
    if not text:
        return 0
    total = 0.0
    for ch in text:
        if ch.isspace():
            total += SPACE_PER_CHAR
        elif _is_cjk(ch):
            total += CJK_PER_CHAR
        elif ch.isascii():
            total += ASCII_PER_CHAR
        else:
            total += OTHER_PER_CHAR
    return max(1, math.ceil(total))


def count_messages(messages: list[dict]) -> int:
    """一组 OpenAI 格式消息的总 token（含每条消息的固定开销）。"""
    n = 0
    for m in messages or []:
        n += MSG_OVERHEAD + count_tokens(m.get("content"))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            n += count_tokens(fn.get("name")) + count_tokens(fn.get("arguments"))
    return n


def tokens_to_chars(tokens: int) -> int:
    """token → 字符的粗略反推，用于"按 token 预算裁文本"。

    取 2.0 chars/token（中英混排的经验中位），宁断得早一点。
    """
    return max(1, int(tokens * 2.0))


@dataclass
class Budget:
    """一次请求的预算。分区比例沿用方案：system 15% / 检索 25% / 历史 60%。"""

    window: int = 64000
    output_reserve: int = 1024
    safety: int = 512
    soft_cap: int = 12000
    shares: dict = field(default_factory=lambda: {"system": 0.15, "retrieval": 0.25, "history": 0.60})

    @property
    def capacity(self) -> int:
        """窗口侧允许的最大输入。"""
        return max(1024, self.window - self.output_reserve - self.safety)

    @property
    def usable(self) -> int:
        """实际可用的输入预算 = min(窗口侧, 软上限)。"""
        return min(self.capacity, self.soft_cap) if self.soft_cap else self.capacity

    def alloc(self, section: str) -> int:
        return int(self.usable * self.shares.get(section, 0.0))

    def as_dict(self) -> dict:
        return {"window": self.window, "capacity": self.capacity, "usable": self.usable,
                "shares": dict(self.shares)}


def default_budget() -> Budget:
    from ..core import config
    return Budget(
        window=config.CTX_WINDOW,
        output_reserve=config.CTX_OUTPUT_RESERVE,
        safety=config.CTX_SAFETY,
        soft_cap=config.CTX_SOFT_CAP,
    )


def log_ctx(**fields) -> str:
    """结构化单行日志：字段少而固定，方便 grep 和以后接采集。"""
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
    line = "ctx " + " ".join(parts)
    if os.getenv("CTX_LOG", "1") != "0":
        print(line)
    return line
