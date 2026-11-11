"""LLM 客户端（OpenAI 兼容协议）。

所有 prompt 统一经 prompts.py 注册表加载（YAML 版本化），
本模块只负责：渲染 → 调用 → JSON 解析。prompt 内容看 prompts/*.yaml。
未配置 LLM_API_KEY 时依赖 LLM 的功能抛 RuntimeError，其余功能不受影响。
"""
import json
import os
import re
import time

import httpx

from . import config, prompts

_call_count = 0  # 进程级统计（评测脚本用）
_last_usage: dict | None = None   # 最近一次调用的真实 token 用量，用来校准本地估算


def stats() -> dict:
    return {"calls": _call_count, "last_usage": _last_usage}


def last_usage() -> dict | None:
    """最近一次 API 返回的 usage（prompt_tokens 等），用于校准 `context.count_tokens` 的估算。"""
    return _last_usage


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


def _consume_stream(body: dict):
    """流式消费一次 chat/completions。返回 (msg, usage, ttft_ms)。

    为什么流式：非流式拿不到首 token 时间（TTFT），而 TTFT 是 LLM 可观测的
    核心体验指标——用户感知的"卡"取决于 TTFT + 生成速度，不是总耗时。
    stream_options.include_usage 让最后一个 chunk 带真实 usage（DeepSeek/OpenAI 均支持）。
    tool_calls 的 delta 按 index 聚合：首个 delta 带 id/name，后续 chunk 只补 arguments 片段。
    """
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    t0 = time.perf_counter()
    ttft = None
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    usage = None
    with httpx.stream(
        "POST", f"{config.LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
        json=body, timeout=90,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    if ttft is None:
                        ttft = round((time.perf_counter() - t0) * 1000, 1)
                    content_parts.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index") or 0
                    acc = tool_calls.setdefault(
                        idx, {"id": "", "type": "function",
                              "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        if ttft is None:
                            ttft = round((time.perf_counter() - t0) * 1000, 1)
                        acc["function"]["arguments"] += fn["arguments"]
    msg = {"content": "".join(content_parts) or None}
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return msg, usage, ttft


def _chat_json(system: str | None, user: str | None, tools: list | None = None,
               messages: list | None = None, temperature: float = 0.1,
               json_mode: bool = True) -> dict:
    """发起对话补全。tools 模式返回原始 message（可能含 tool_calls），否则返回解析后的 JSON message。

    json_mode=False 用于"我要的是一段话，不是 JSON"的场景（如摘要压缩）：
    强制 json_object 时模型会把整段文字塞进 {"content": "..."} 里，
    下游拿到的是 JSON 字符串而不是摘要——这个坑真的踩过。
    """
    global _call_count, _last_usage
    if not config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY 未配置（见 .env），无法调用 LLM")
    if messages is None:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    body = {"model": config.LLM_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    elif json_mode:
        body["response_format"] = {"type": "json_object"}
    # LLM span：这是此前完全没留痕的一段——prompt、返回、token 数、耗时、TTFT，
    # 是排查"模型当时看到了什么"的第一现场。属性对齐 GenAI/OpenInference 语义约定。
    from ..observability import tracing as tr
    stream = os.getenv("LLM_STREAM", "1") != "0"
    with tr.span("llm.chat", tr.KIND_LLM, {
        tr.ATTR_MODEL: config.LLM_MODEL,
        "llm.scene": tr.current_scene() or "unknown",
        "llm.n_messages": len(messages),
        "llm.tools": len(tools) if tools else 0,
        "input.value": json.dumps(messages, ensure_ascii=False),
        "input.mime_type": "application/json",
        "llm.temperature": temperature,
        "llm.stream": stream,
    }) as s:
        usage = None
        try:
            if stream:
                msg, usage, ttft = _consume_stream(body)
                if s is not None and ttft is not None:
                    s.set_attribute("llm.ttft_ms", ttft)
                    tr.add_event(s, "first_token", ttft_ms=ttft)
            else:
                resp = httpx.post(
                    f"{config.LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                    json=body, timeout=90,
                )
                _call_count += 1
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage")
                msg = data["choices"][0]["message"]
        except Exception:
            if stream:
                # 流式失败降级非流式重试一次：可用性优先于观测细节
                tr.add_event(s, "stream_fallback", reason="stream failed, retry non-stream")
                resp = httpx.post(
                    f"{config.LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                    json=body, timeout=90,
                )
                _call_count += 1
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage")
                msg = data["choices"][0]["message"]
            else:
                raise
        _call_count += 1
        _last_usage = usage
        if s is not None:
            u = usage or {}
            pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
            # 双写：gen_ai.* 是 OTel GenAI 语义约定；llm.token_count.* 是 OpenInference
            # 约定（Phoenix 的 token 统计列只认这一套——只写 gen_ai 时面板永远显示 0）。
            s.set_attribute(tr.ATTR_PROMPT_TOKENS, pt)
            s.set_attribute(tr.ATTR_COMPLETION_TOKENS, ct)
            s.set_attribute("llm.token_count.prompt", pt)
            s.set_attribute("llm.token_count.completion", ct)
            s.set_attribute("llm.token_count.total", pt + ct)
            tr.add_event(s, "gen_ai.usage", prompt_tokens=pt, completion_tokens=ct)
            s.set_attribute("output.value", json.dumps(msg.get("content") or "", ensure_ascii=False))
            s.set_attribute("output.mime_type", "application/json")
        return msg


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

_COMPLETENESS = ("none", "partial", "complete")
_DOC_TYPES = ("interview", "single_question", "answer_snippet")


def _as_text(v) -> str:
    """答案字段可能是 str / list / dict，统一成一段可直接写库的纯文本。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return "\n".join(f"- {str(x).strip()}" for x in v if str(x).strip())
    if isinstance(v, dict):
        return "；".join(f"{k}：{x}" for k, x in v.items() if str(x).strip())
    return str(v).strip()


def extract_interview(raw_text: str, source_url: str = "") -> dict:
    """抽取 + 判定（doc_type / 有无答案 / 答案完整度）。

    **为什么判定必须在这里做**：它决定后面走哪条路——答案完整 → 用原文规范化成卡
    （低风险，仍走审批但可批量通过）；没有答案 → LLM 从零起草（必须逐条人审）。
    判定错 = 数据流向错，所以下面逐项校正，不信任模型自报：
      - doc_type 用题目条数硬校正（两道以上不可能是"单题摘录"）；
      - has_answer 由 answer_completeness 派生，不单独采信；
      - 声称有答案但 answer 抽不出来 → 降级 none（没有正文就不算有答案）；
      - 有正文却被判 none → 升到 partial（有证据就留，交人审，不静默丢内容）；
      - 自称 complete 却列出了 gaps → 自相矛盾，降级 partial；
      - 非面经材料里公司/轮次若在原文中找不到字面依据 → 清空（防幻觉）。
    """
    system, user, version = prompts.render(
        "extract_interview", source_url=source_url or "未知", raw_text=raw_text[:12000])
    data = chat_parsed(system, user)

    raw_qs = data.get("questions")
    if not isinstance(raw_qs, list):
        raw_qs = []
    questions = []
    for q in raw_qs:
        if not isinstance(q, dict):
            continue
        text = _as_text(q.get("text"))
        if not text:
            continue
        answer = _as_text(q.get("answer"))
        comp = str(q.get("answer_completeness") or "").strip().lower()
        if comp not in _COMPLETENESS:
            comp = "partial" if answer else "none"
        if not answer:
            comp = "none"
        elif comp == "none":
            comp = "partial"
        gaps = q.get("answer_gaps")
        gaps = [str(g).strip() for g in gaps if str(g).strip()] if isinstance(gaps, list) else []
        if comp == "complete" and gaps:
            comp = "partial"
        questions.append({
            "order": len(questions) + 1,
            "text": text,
            "qtype": (q.get("qtype") or "").strip() or "八股",
            "topic_hint": (q.get("topic_hint") or "").strip() or None,
            "has_answer": comp != "none",
            "answer_completeness": comp,
            "answer": answer or None,
            "answer_gaps": gaps,
            # True = 需要模型从零起草（没有现成答案可规范化）；只有 complete 才是 false。
            # 注意这不等于"不建卡"：留用的题都建卡，这个字段只用来区分走哪条 prompt。
            "suggest_build_card": comp != "complete",
        })
    data["questions"] = questions

    dt = str(data.get("doc_type") or "").strip().lower()
    if dt not in _DOC_TYPES:
        dt = "interview" if len(questions) >= 2 else "single_question"
    if len(questions) >= 2:
        dt = "interview"
    data["doc_type"] = dt
    if dt != "interview":
        for k in ("company", "position", "round", "interview_date"):
            v = data.get(k)
            if v and str(v).strip() not in raw_text:
                data[k] = None
        data["result"] = "unknown"

    data["judgement"] = {
        "doc_type": dt,
        "n_questions": len(questions),
        "n_bare": sum(1 for q in questions if q["answer_completeness"] == "none"),
        "n_partial": sum(1 for q in questions if q["answer_completeness"] == "partial"),
        "n_complete": sum(1 for q in questions if q["answer_completeness"] == "complete"),
        "suggest_build": [q["order"] for q in questions if q["suggest_build_card"]],
    }
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


# ============ 上下文压缩（阶段 8） ============

def summarize_context(prev_summary: str, transcript: str) -> str:
    """把被窗口挤出去的旧对话压成三段式要点。

    注意这里**不能**用 chat_parsed：那条路会强制 response_format=json_object，
    而摘要要的是给人/模型看的三段短文，压成 JSON 反而更难注入。
    """
    system, user, _ = prompts.render(
        "ctx_summarize", prev=(prev_summary or "（无）")[:1500], transcript=transcript[:12000])
    msg = _chat_json(system, user, temperature=0.2, json_mode=False)
    return (msg.get("content") or "").strip()


def overall_report(direction: str, persona: str, qa_records: list[dict]) -> dict:
    system, user, _ = prompts.render(
        "overall_report", persona=persona or "互联网大厂资深面试官", direction=direction,
        qa_records="\n\n".join(
            f"题：{r['text']}（均分{round(sum((r.get('scores') or {'a': 0}).values()) / 4, 1)}）\n"
            f"回答摘要：{(r.get('answer') or '(跳过)')[:300]}\n点评：{(r.get('feedback') or '')[:200]}"
            for r in qa_records))
    return chat_parsed(system, user)
