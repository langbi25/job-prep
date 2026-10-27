"""LLM 客户端（OpenAI 兼容协议）。

所有 prompt 统一经 prompts.py 注册表加载（YAML 版本化），
本模块只负责：渲染 → 调用 → JSON 解析。prompt 内容看 prompts/*.yaml。
未配置 LLM_API_KEY 时依赖 LLM 的功能抛 RuntimeError，其余功能不受影响。
"""
import json
import re

import httpx

from . import config, prompts

_call_count = 0  # 进程级统计（评测脚本用）


def stats() -> dict:
    return {"calls": _call_count}


def extract_json(text: str) -> dict:
    """健壮 JSON 提取：剥 markdown 围栏 → 整体解析 → 截取首个完整对象。

    模型即使被 response_format=json_object 约束，仍会偶发两种走样：
    输出 ```json 围栏，或把两个 JSON 对象拼在一起（"Extra data: line 1 column N"）。
    这类失败会直接打崩 json_validity，所以解析必须容错且只在一处实现。
    """
    if not isinstance(text, str):
        return text
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start >= 0:
        # raw_decode 只消费第一个完整对象，天然忽略后面的冗余内容
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"无法从模型输出中解析出 JSON：{raw[:200]}")


def _chat_json(system: str | None, user: str | None, tools: list | None = None,
               messages: list | None = None, temperature: float = 0.1) -> dict:
    """发起对话补全。tools 模式返回原始 message（可能含 tool_calls），否则返回解析后的 JSON message。"""
    global _call_count
    if not config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY 未配置（见 .env），无法调用 LLM")
    if messages is None:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    body = {"model": config.LLM_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    else:
        body["response_format"] = {"type": "json_object"}
    resp = httpx.post(
        f"{config.LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        json=body,
        timeout=90,
    )
    _call_count += 1
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def chat_parsed(system: str, user: str, **kw) -> dict:
    """非 tools 场景的统一入口：调用 → 解析 → 解析失败重试一次（附纠错指令）。"""
    msg = _chat_json(system, user, **kw)
    try:
        return extract_json(msg["content"])
    except (ValueError, json.JSONDecodeError):
        repair = [{"role": "system", "content": system},
                  {"role": "user", "content": user},
                  {"role": "assistant", "content": (msg.get("content") or "")[:4000]},
                  {"role": "user", "content": "上面的输出不是合法 JSON（可能有多余内容或围栏）。"
                                              "请只输出一个 JSON 对象，不要任何解释、不要代码块。"}]
        return extract_json(_chat_json(None, None, messages=repair, temperature=0)["content"])


# ============ 面经采集（阶段 1） ============

def extract_interview(raw_text: str, source_url: str = "") -> dict:
    system, user, version = prompts.render(
        "extract_interview", source_url=source_url or "未知", raw_text=raw_text[:12000])
    data = chat_parsed(system, user)
    if not isinstance(data.get("questions"), list):
        data["questions"] = []
    data["_prompt"] = {"name": "extract_interview", "version": version}
    return data


def tag_card(title: str, content: str) -> dict:
    system, user, version = prompts.render("tag_card", title=title, content=content[:6000])
    data = chat_parsed(system, user)
    data["_prompt"] = {"name": "tag_card", "version": version}
    return data


# ============ 模拟面试（阶段 3~5） ============

def generate_questions(direction: str, jd_text: str, n: int, avoid: list[str]) -> list[dict]:
    system, user, _ = prompts.render(
        "generate_questions", direction=direction, jd_text=jd_text or "无", n=n,
        avoid=json.dumps(avoid[:20], ensure_ascii=False))
    data = chat_parsed(system, user)
    return [q for q in data.get("questions", []) if q.get("text")][:n]


def evaluate_answer(question: dict, answer: str, persona: str, context: str = "") -> dict:
    """逐题评分。context 由 context_builder 组装（知识库校准材料，上下文工程）。"""
    system, user, version = prompts.render(
        "evaluate_answer", persona=persona or "互联网大厂资深面试官",
        qtype=question.get("qtype", ""), question_text=question["text"],
        answer=answer[:6000], context=context)
    data = chat_parsed(system, user)
    data.setdefault("scores", {"correctness": 0, "depth": 0, "structure": 0, "highlight": 0})
    data["_prompt"] = {"name": "evaluate_answer", "version": version}
    return data


def overall_report(direction: str, persona: str, qa_records: list[dict]) -> dict:
    system, user, _ = prompts.render(
        "overall_report", persona=persona or "互联网大厂资深面试官", direction=direction,
        qa_records="\n\n".join(
            f"题：{r['text']}（均分{round(sum((r.get('scores') or {'a': 0}).values()) / 4, 1)}）\n"
            f"回答摘要：{(r.get('answer') or '(跳过)')[:300]}\n点评：{(r.get('feedback') or '')[:200]}"
            for r in qa_records))
    return chat_parsed(system, user)
