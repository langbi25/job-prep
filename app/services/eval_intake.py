"""评测集自动进料：把真实会话里的失败轮沉淀成评测素材。

质量闭环原来缺的一块：评测集全靠手写，而真实使用里暴露的毛病（点踩的轮、
报错的轮）不进闭环。这一层把它们自动接住——**进料不等于入集**：进来的 case
没有标注（什么算好答案没人写），直接混进 golden set 会污染评分口径。
所以分两步：

1. 自动进料（本模块）：失败轮原文进 data/eval_intake.jsonl，按 key 去重；
2. 人工提升（scripts/promote_eval_cases.py）：确认有代表性后挪进 evals/，
   补上标注，才成为评测用例。

写文件失败绝不影响对话：进料是统计行为，不是业务前置条件。
测试默认关闭（conftest 统一设 EVAL_INTAKE=0），被测模块自己显式打开。
"""
import datetime
import hashlib
import json
import os

from ..core import config

# 单条正文的入库上限：评测素材需要上下文，但不需要几万字的原文
MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 4000


def enabled() -> bool:
    return os.getenv("EVAL_INTAKE", "1").strip().lower() not in ("0", "false", "off", "no")


def path():
    """进料文件路径。放 data/（容器里是挂载卷）而不是 evals/（仓库目录，
    容器里未必可写），提升动作由脚本把两边对上。"""
    return config.DATA_DIR / "eval_intake.jsonl"


def _case_key(question: str, session_id: str, turn: int) -> str:
    """去重键：同一轮重复点踩/重复失败只进一条。"""
    raw = f"{session_id}|{turn}|{question[:200]}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _existing_keys(p) -> set:
    keys = set()
    if not p.exists():
        return keys
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(json.loads(line).get("key"))
            except json.JSONDecodeError:
                continue          # 半截行（进程中断）跳过，不影响其余
    except OSError:
        pass
    return keys


def record_case(*, question: str, answer: str, reason: str, source: str,
                session_id: str | None = None, turn: int | None = None,
                meta: dict | None = None) -> dict | None:
    """写一条失败素材。返回写入的 case（去重命中返回 None）。

    question/answer 截断不是丢证据：trace 的全文在 span_blobs 里，这里
    留的是"能让标注的人看懂发生了什么"的最小上下文。
    """
    if not enabled():
        return None
    p = path()
    try:
        key = _case_key(question or "", session_id or "", int(turn or 0))
        if key in _existing_keys(p):
            return None
        case = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "key": key,
            "source": source,                       # user_reject / run_failed / ...
            "session_id": session_id,
            "turn": int(turn) if turn else None,
            "question": (question or "")[:MAX_QUESTION_CHARS],
            "answer": (answer or "")[:MAX_ANSWER_CHARS],
            "reason": reason,
            "meta": meta or {},
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
        return case
    except Exception:  # noqa: BLE001  进料失败绝不影响对话
        return None


def _turn_messages(session_id: str, turn: int) -> tuple[str, str] | None:
    """取第 N 轮的（用户问题，助手回答）。第 N 个 user 消息即第 N 轮的问题。"""
    from ..core.db import get_conn  # noqa: PLC0415

    conn = get_conn()
    try:
        q = conn.execute(
            "SELECT content FROM console_messages WHERE session_id=? AND role='user' "
            "ORDER BY id LIMIT 1 OFFSET ?", (session_id, max(int(turn) - 1, 0))).fetchone()
        if not q:
            return None
        qid = conn.execute(
            "SELECT id FROM console_messages WHERE session_id=? AND role='user' "
            "ORDER BY id LIMIT 1 OFFSET ?", (session_id, max(int(turn) - 1, 0))).fetchone()
        a = conn.execute(
            "SELECT content FROM console_messages WHERE session_id=? AND role='assistant' "
            "AND id > ? ORDER BY id LIMIT 1", (session_id, qid["id"])).fetchone()
        return q["content"], (a["content"] if a else "")
    except Exception:  # noqa: BLE001  消息被清掉时进不了料，不报错
        return None
    finally:
        conn.close()


def record_turn_failure(session_id: str, turn: int, reason: str,
                        meta: dict | None = None) -> dict | None:
    """按 (session, turn) 进料一条失败轮。set_feedback / record 的钩子走这里。

    reason 即 turn_facts 的 failure_rule（user_reject / run_failed），进料按它
    分组统计——"哪类失败攒了最多素材"直接对上归因面板的规则口径。
    """
    msgs = _turn_messages(session_id, turn)
    if not msgs:
        return None
    return record_case(question=msgs[0], answer=msgs[1], reason=reason,
                       source=reason, session_id=session_id, turn=int(turn),
                       meta=meta)


def stats() -> dict:
    """进料现状：总数、按原因分布、最近几条。"""
    p = path()
    cases = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    cases.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    by_reason: dict[str, int] = {}
    for c in cases:
        by_reason[c.get("source") or "?"] = by_reason.get(c.get("source") or "?", 0) + 1
    return {"path": str(p), "count": len(cases),
            "by_source": by_reason,
            "latest": cases[-5:][::-1]}
