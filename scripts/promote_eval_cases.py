"""把 data/eval_intake.jsonl 里的失败素材提升进 evals/ 评测集。

用法：python scripts/promote_eval_cases.py [--dry-run]

两阶段设计的第二步：自动进料（turn_facts 钩子）把失败轮原文攒进 data/，
这一步由人触发，把它们去重后并入 evals/feedback_cases.jsonl。**提升不等于
入集**——并进去的还是未标注素材，打分口径（golden_answers.jsonl 的 band）
要人工补齐后才参与 band_accuracy 门禁。原文件不清空：data/ 里的原始记录
留着对账，重复条目靠 key 去重跳过。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "data" / "eval_intake.jsonl"
DST = BASE / "evals" / "feedback_cases.jsonl"


def _read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[skip] 半截行: {line[:80]}")
    return out


def main() -> int:
    dry = "--dry-run" in sys.argv
    src = _read(SRC)
    if not src:
        print(f"没有可提升的素材（{SRC} 不存在或为空）")
        return 0
    dst = _read(DST)
    known = {c.get("key") for c in dst}
    fresh = [c for c in src if c.get("key") and c["key"] not in known]
    print(f"素材 {len(src)} 条 / 已在集内 {len(dst)} 条 / 本次新增 {len(fresh)} 条")
    if dry:
        for c in fresh[:10]:
            print(f"  - [{c.get('source')}] {str(c.get('question'))[:60]}")
        if len(fresh) > 10:
            print(f"  ... 其余 {len(fresh) - 10} 条")
        return 0
    if not fresh:
        print("没有新增，跳过写入")
        return 0
    DST.parent.mkdir(parents=True, exist_ok=True)
    with DST.open("a", encoding="utf-8") as f:
        for c in fresh:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"已追加 {len(fresh)} 条到 {DST}。下一步：人工标注（补 expect/band 字段）后入 golden。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
