"""SSE 流基础设施：带缓冲的事件总线 + 断线续传。

为什么单独一个模块：console 和编排（长任务）都需要同一套东西——后台任务持续产出
事件，客户端可能中途断开。编排一次 20~35 秒，断开概率比单轮对话更高；如果只在
对话里做续传，编排那边切个标签页就前功尽弃。

设计要点都在 StreamBus 里，见其 docstring。
"""
import json
import threading
import time
import uuid

from fastapi.responses import StreamingResponse

STREAM_IDLE_TIMEOUT_S = 300   # 单次任务无产出的硬上限，防客户端死等
STREAM_TTL_S = 600            # 结束后缓冲保留多久，够用户切回标签页续看


class StreamBus:
    """一次任务的事件缓冲：可反复读、可从任意序号续读。

    为什么需要它：SSE 断开（切标签页、锁屏、网络闪断）时后台任务还在跑，
    但事件已经全部丢失。用户回来看到空白，只能重发一遍——白花十几秒和几分钱。
    有了缓冲，重连后从断点补齐即可，后台任务不受影响。

    事件只追加不删除：一次任务最多几百条、每条几百字节，占几十 KB，
    TTL 到期随流一起回收。用环形缓冲省这点内存不值当，索引语义会变得难解释。
    """

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._closed = False
        self._closed_at = 0.0
        self._cond = threading.Condition()

    @property
    def closed(self) -> bool:
        return self._closed

    def expired(self, now: float | None = None, ttl: float = STREAM_TTL_S) -> bool:
        now = time.monotonic() if now is None else now
        return self._closed and now - self._closed_at > ttl

    def publish(self, ev: dict) -> None:
        with self._cond:
            self._events.append({**ev, "seq": len(self._events) + 1})
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._closed_at = time.monotonic()
            self._cond.notify_all()

    def iter_from(self, after: int = 0):
        """从 after（上次收到的 seq）之后开始吐，流关闭且吐完就返回。

        锁只在块内持有：yield 时还持锁的话，慢客户端会把 publisher 也堵住，
        表现成"后台任务卡死"，其实是消费端太慢。
        """
        i = max(0, int(after or 0))
        deadline = time.monotonic() + STREAM_IDLE_TIMEOUT_S
        while True:
            timed_out = False
            with self._cond:
                while i >= len(self._events) and not self._closed:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        timed_out = True
                        break
                    self._cond.wait(timeout=min(1.0, remain))
                batch = self._events[i:]
                closed = self._closed
                if not timed_out:
                    timed_out = i >= len(self._events) and not closed and time.monotonic() >= deadline
            if timed_out:
                yield {"type": "error", "seq": i + 1,
                       "message": "这一轮超过 5 分钟没有产出，已中断"}
                return
            for ev in batch:
                i += 1
                yield ev
            if closed and i >= len(self._events):
                return


class StreamHub:
    """流登记表。create 时顺手回收过期流，不必单开清理线程。"""

    def __init__(self) -> None:
        self._streams: dict[str, StreamBus] = {}
        self._lock = threading.Lock()

    def create(self, prefix: str = "st") -> tuple[str, StreamBus]:
        sid = f"{prefix}-{uuid.uuid4().hex[:12]}"
        bus = StreamBus()
        with self._lock:
            for old in [k for k, b in self._streams.items() if b.expired()]:
                self._streams.pop(old, None)
            self._streams[sid] = bus
        return sid, bus

    def get(self, stream_id: str) -> StreamBus | None:
        with self._lock:
            return self._streams.get(stream_id)


hub = StreamHub()


def sse_response(bus: StreamBus, stream_id: str, after: int = 0) -> StreamingResponse:
    """把事件序列写成 SSE。带 id: 行，标准客户端（EventSource）重连时会自动回传
    Last-Event-ID；我们自己的前端用 fetch 读流，靠 seq 字段手工续传。"""

    def gen():
        for ev in bus.iter_from(after):
            yield f"id: {ev.get('seq', 0)}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "X-Stream-Id": stream_id})


def emit_safe(bus: StreamBus):
    """包一层：事件的回调绝不能把业务逻辑带崩。

    流式是体验增强，不是关键路径——编排里某一步发事件失败，不该让整条 DAG 失败。
    """
    def emit(ev: dict) -> None:
        try:
            bus.publish(ev)
        except Exception:  # noqa: BLE001
            pass
    return emit
