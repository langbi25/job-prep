"""上下文预算 A/B：把"改前"和"改后"放在同一批对话上量出来。

三个口径一起测，因为"改前"到底指什么很容易被含糊过去：

  full     全量拼接（= 改造方案第二节的推断：把历史全部塞进去）
  naive6   改造前的真实实现：最近 6 轮硬截断（HISTORY_TURNS=6），轮内工具结果不裁
  ctx      改造后：窗口+锚定+摘要+检索，超预算按 检索→摘要→最旧轮次 降级

同时记录"关键约束是否还在上下文里"——只看 token 变小是耍流氓，
把约束丢掉之后 token 当然小。

跑法：
    python scripts/ctx_ab_test.py                # 纯本地，零 LLM 调用
    python scripts/ctx_ab_test.py --calibrate    # 额外做 3 次真实小调用，校准估算误差
"""
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.context import assembler, window  # noqa: E402
from app.context.token_budget import Budget, count_messages  # noqa: E402

BUDGET = Budget(window=64000, soft_cap=12000)
NAIVE_TURNS = 6                    # 改造前 console_service.HISTORY_TURNS
CONSTRAINT = "记住：这套系统只跑在 2C2G 单机上，不要引入 Redis、向量库或消息队列。"


def _turn(i: int) -> list[dict]:
    """一轮有代表性的对话：用户提要求 + 助理带结论的汇报（含 id 与评分）。"""
    if i == 1:
        user = CONSTRAINT
    elif i % 5 == 0:
        user = f"q-{i:04d} 这题我答得怎么样？"
    else:
        user = f"第 {i} 轮：帮我把这份面经里的题过一遍，顺便看看缺口。"
    reply = (f"第 {i} 轮处理完了：入池 {i % 4 + 2} 道题，起草 {i % 3} 张待审卡，"
             f"作战地图预测分 {3 + i % 5}.{(i * 7) % 10}。" + "补充说明。" * 240)
    return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]


def _tool_turn(n_calls: int = 8, payload: int = 2600) -> list[dict]:
    """单轮长物料：一次投喂触发 8 次工具调用，每次返回 2600 字（json_safe 的上限）。"""
    msgs = [{"role": "system", "content": "你是执行助理。"},
            {"role": "user", "content": "把这份面经收进去。" + "面经正文。" * 800}]
    for i in range(n_calls):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "ingest_text", "arguments": '{"text":"..."}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "结果" * (payload // 2)})
    return msgs


def _has_constraint(messages: list[dict]) -> bool:
    return any("2C2G" in (m.get("content") or "") for m in messages)


def run_long_session(turns: int = 20) -> dict:
    rows = {"full": [], "naive6": [], "ctx": []}
    kept = {"full": [], "naive6": [], "ctx": []}
    history: list[dict] = []

    for i in range(1, turns + 1):
        new_turn = _turn(i)
        history += new_turn

        # full：全量拼接
        full = [{"role": "system", "content": "你是助理。"}] + history
        rows["full"].append(count_messages(full))
        kept["full"].append(_has_constraint(full))

        # naive6：最近 6 轮硬截断（改造前的真实行为）
        naive = [{"role": "system", "content": "你是助理。"}] + history[-(NAIVE_TURNS * 2):]
        rows["naive6"].append(count_messages(naive))
        kept["naive6"].append(_has_constraint(naive))

        # ctx：窗口 + 锚定（摘要这里当作"已生成"，用占位文本模拟其体积）
        ctx = assembler.assemble(f"ab-long-{i}", "你是助理。", "", history[:-1],
                                 budget=BUDGET, use_retrieval=False, persist=False)
        ctx.messages.append({"role": "user", "content": new_turn[0]["content"]})
        rows["ctx"].append(count_messages(ctx.messages))
        kept["ctx"].append(_has_constraint(ctx.messages))

    return {"tokens": rows, "constraint_kept": kept}


def run_tool_heavy() -> dict:
    msgs = _tool_turn()
    naive = count_messages(msgs)
    trimmed = assembler.enforce_inflight([dict(m) for m in msgs], BUDGET)
    return {"naive": naive, "ctx": trimmed["after"], "trimmed": trimmed["trimmed"],
            "over": trimmed["over"]}


def calibrate(n: int = 3) -> dict:
    """用 API 返回的真实 prompt_tokens 校准估算误差（要花 3 次小调用）。"""
    from app.core import config, llm

    if not config.LLM_API_KEY:
        return {"error": "LLM_API_KEY 未配置"}
    samples = []
    probes = [
        "用一句话说明什么是上下文窗口。",
        "把下面三个词按长度排序：Redis、Kafka、Flink。" + "补充材料。" * 200,
        "总结这段话的要点：" + "长文本。" * 600,
    ]
    for text in probes[:n]:
        # 注意：_chat_json 非 tools 分支会带 response_format=json_object，
        # 而 DeepSeek 要求 prompt 里出现 "json" 字样，否则直接 400。
        msgs = [{"role": "system", "content": "你是简洁的助理，用 JSON 回复：{\"ok\":true}"},
                {"role": "user", "content": text}]
        est = count_messages(msgs)
        try:
            llm._chat_json(None, None, messages=msgs, temperature=0)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        usage = llm.last_usage() or {}
        real = usage.get("prompt_tokens")
        if real:
            samples.append({"est": est, "real": real, "err_pct": round(100 * (est - real) / real, 1)})
    if not samples:
        return {"error": "API 未返回 usage"}
    return {"samples": samples,
            "avg_err_pct": round(sum(s["err_pct"] for s in samples) / len(samples), 1)}


def _fmt(rows: list[int]) -> str:
    return " → ".join(str(x) for x in rows)


def main() -> int:
    print(f"# 上下文预算 A/B（{datetime.date.today().isoformat()}）\n")
    print(f"预算：window={BUDGET.window} 软上限={BUDGET.soft_cap} "
          f"可用={BUDGET.usable}（history 分区 {BUDGET.alloc('history')}）\n")

    long_ = run_long_session()
    print("## 场景一：20 轮长会话（每轮助理回复 ~1200 字）\n")
    print("| 策略 | 第1轮 | 第10轮 | 第20轮 | 峰值 | 均值 | 超预算轮次 | 约束保留 |")
    print("|---|---|---|---|---|---|---|---|")
    for name in ("full", "naive6", "ctx"):
        v = long_["tokens"][name]
        over = sum(1 for x in v if x > BUDGET.usable)
        print(f"| {name} | {v[0]} | {v[9]} | {v[-1]} | {max(v)} | {round(sum(v)/len(v))} "
              f"| {over}/20 | {sum(long_['constraint_kept'][name])}/20 |")

    print("\n## 场景二：单轮长物料（1 次投喂 → 8 次工具调用 × 2600 字）\n")
    t = run_tool_heavy()
    print("| 策略 | 输入 token | 说明 |")
    print("|---|---|---|")
    print(f"| naive（不裁轮内结果） | {t['naive']} | 一次请求就顶到窗口的 1/3 |")
    print(f"| ctx（三档收紧） | {t['ctx']} | 裁了 {t['trimmed']} 条工具结果，仍超预算={t['over']} |")

    calib = None
    if "--calibrate" in sys.argv:
        print("\n## 估算误差校准（真实 API usage）\n")
        calib = calibrate()
        if "samples" in calib:
            for s in calib["samples"]:
                print(f"- 估算 {s['est']} / 真实 {s['real']} → 偏差 {s['err_pct']}%")
            print(f"\n平均偏差 **{calib['avg_err_pct']}%**（正=估多了，安全方向）")
        else:
            print(f"- 跳过：{calib.get('error')}")

    out = ROOT / "docs" / "上下文预算-AB结果.json"
    out.write_text(json.dumps({"budget": BUDGET.as_dict(), "long_session": long_,
                               "tool_heavy": t, "calibration": calib},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
