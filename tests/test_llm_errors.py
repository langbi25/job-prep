"""LLM 错误诊断与 json_mode 前置条件测试。

两个真实故障，都不是理论问题：
1. DeepSeek 的 json_object 模式要求 prompt 里出现 "json"，否则直接 400。编排各步骤的
   prompt 由模型生成，有的写「输出 JSON」有的只写「给出结论」——于是同一份代码在不同
   步骤随机 400，表现为「5 个计划 3 个失败，失败步骤各不相同」。
2. raise_for_status() 把响应体丢了，只剩一行 "400 Bad Request"。看不到服务端说的原因，
   就无法区分「该重试」「该裁上下文」「该改 prompt」——那等于没有错误处理。
"""
import pytest

from app.core import llm


def test_json_hint_appended_when_missing():
    msgs = [{"role": "system", "content": "你是助手"},
            {"role": "user", "content": "给结论"}]
    llm._ensure_json_hint(msgs)
    assert "json" in msgs[-1]["content"].lower(), "补在最后一条 user 上，贴近格式要求的位置"


def test_json_hint_not_duplicated_when_present():
    msgs = [{"role": "user", "content": "请输出 JSON 格式"}]
    before = msgs[0]["content"]
    llm._ensure_json_hint(msgs)
    assert msgs[0]["content"] == before, "已经含 json 就不能再追加，否则每轮都变长"


def test_json_hint_handles_empty_messages():
    msgs = []
    llm._ensure_json_hint(msgs)
    assert len(msgs) == 1
    assert "json" in msgs[0]["content"].lower()


class _StreamResp:
    """模拟流式响应：read() 可读，text 不可读（真实 httpx 就是这样）。"""

    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        raise RuntimeError("流式响应的 text 属性不可用")


def test_fail_reads_body_from_stream_response():
    resp = _StreamResp(400, b'{"error":{"message":"maximum context length is 65536"}}')
    with pytest.raises(llm.LLMError) as ei:
        llm._fail(resp, {"messages": [{"role": "user", "content": "hi"}]})
    err = ei.value
    assert err.status == 400
    assert err.retryable is False, "400 是请求体不合法，用同一个 body 重试必然同样失败"
    assert "maximum context length" in str(err), "服务端给的原因必须带出来"
    assert "messages=1" in str(err), "请求摘要要能区分上下文超限与 messages 结构错误"


def test_fail_marks_transient_statuses_retryable():
    for code in (408, 429, 500, 502, 503, 504):
        with pytest.raises(llm.LLMError) as ei:
            llm._fail(_StreamResp(code, b"{}"), {"messages": []})
        assert ei.value.retryable is True, code


def test_fail_is_noop_below_400():
    llm._fail(_StreamResp(200, b"{}"), {"messages": []})


def test_fail_survives_unreadable_body():
    class _Broken:
        status_code = 400
        def read(self):
            raise RuntimeError("socket closed")
        @property
        def text(self):
            raise RuntimeError("no text")

    with pytest.raises(llm.LLMError) as ei:
        llm._fail(_Broken(), {"messages": []})
    assert "<响应体不可读>" in str(ei.value)
