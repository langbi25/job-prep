"""摘要裁剪的回归测试（零 LLM 调用）。

背景（2026-11-15 20 轮真实会话实测）：摘要从 377 字一路长到 863 字，
压缩比跌到 0.6——"压缩"变成了"扩写"。prompt 里写 300 字模型不一定听，
所以代码侧必须兜底：超预算时按 进度 → 事实 → 约束 的顺序裁，
**约束段永不整段丢**（它在最后，硬切必然先切掉它）。

跑法：
    python tests/test_summary_trim.py
    pytest tests/test_summary_trim.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.context.summarizer import (  # noqa: E402
    MAX_SUMMARY_CHARS, TRIM_MARK, _render_sections, _split_sections, _trim_to_budget)


def _long_summary(progress_lines: int = 12) -> str:
    progress = "\n".join(
        f"- 已答第 {i} 个主题：完整的方法细节与边界条件都写在这里，占位填充内容若干" for i in range(progress_lines))
    return (
        "事实：\n- 用户服务器 2C2G 单机，技术栈 Flink/Kafka/Hive\n"
        f"进度：\n{progress}\n"
        "约束：\n- 方案中不要出现 Redis、Kafka 集群等重组件\n- 按 2C2G 单机来设计"
    )


def test_short_text_untouched():
    text = "事实：\n- 2C2G\n进度：\n- 已答 Kafka\n约束：\n- 不要重组件"
    assert _trim_to_budget(text, 600) == text


def test_progress_trimmed_before_facts_and_constraints():
    text = _long_summary(20)
    out = _trim_to_budget(text, MAX_SUMMARY_CHARS)
    assert len(out) <= MAX_SUMMARY_CHARS
    # 约束必须逐字完整保留
    assert "方案中不要出现 Redis、Kafka 集群等重组件" in out
    assert "按 2C2G 单机来设计" in out
    # 事实优先于进度保留
    assert "技术栈 Flink/Kafka/Hive" in out


def test_constraint_never_dropped_whole():
    text = _long_summary(40)
    out = _trim_to_budget(text, 400)
    assert len(out) <= 400
    assert "约束：" in out
    assert "2C2G" in out


def test_no_sections_falls_back_to_hard_cut():
    text = "这是一段没有三段格式的摘要。" * 100
    out = _trim_to_budget(text, 200)
    assert len(out) <= 200
    assert out.endswith(TRIM_MARK)


def test_sections_roundtrip():
    text = "事实：\n- a\n进度：\n- b\n约束：\n- c"
    secs = _split_sections(text)
    assert [s[0] for s in secs] == ["事实", "进度", "约束"]
    assert _render_sections(secs) == text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(fns)} passed")
