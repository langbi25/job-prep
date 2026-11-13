"""调用树接口：本地 span 的读出口。

和 `/api/turns` 的分工（2026-11-12 夜调整过）：
- `/api/traces` 回答"这一轮内部是怎么跑的"——调用树、每层耗时、慢在哪
- `/api/turns`  回答"这一轮跑得好不好"——一行事实 + 归因

原来这条注释写的是"span 在后端（Phoenix/Langfuse）"，现在不成立了：
span 已落本地 spans 表，OTel 只是可选的旁路导出。所以质量面板不再依赖
"有没有起观测后端"——这是这次改动最实际的一个收益。
"""
from fastapi import APIRouter, HTTPException

from ..services import trace_service
from ..observability import tracing as tr

router = APIRouter(prefix="/api/traces", tags=["traces"])


@router.get("/health")
def health(days: int = 7):
    """span 落库健康度 + 写失败计数 + 全文附件容量。

    写失败计数放在最前面：它非零时，下面所有统计都在悄悄丢样本，
    而丢掉的往往是最慢/报错的那些（它们更可能撞锁）。
    """
    return {"store": trace_service.span_health(days),
            "writer": tr.span_stats(),
            "blobs": trace_service.blob_stats(days)}


@router.get("/blob/{span_id}")
def blob(span_id: str, key: str):
    """取某个属性的全文（prompt 原文 / 工具入参 / 工具返回）。

    树接口里只有 800 字预览——一轮的 prompt 加起来能到几百 KB，
    跟着树一起传会让"点开一条 trace"变成一次大文件下载。
    """
    data = trace_service.read_blob(span_id, key)
    if data is None:
        raise HTTPException(404, f"{span_id} 没有 {key} 的全文（可能未超长，或已过期清理）")
    return data


@router.get("/recent")
def recent(days: int = 7, limit: int = 50, session_id: str | None = None):
    return {"traces": trace_service.recent_traces(days, limit, session_id)}


@router.get("/slow")
def slow(days: int = 7, limit: int = 20):
    return {"spans": trace_service.slow_spans(days, limit)}


@router.get("/{trace_id}")
def tree(trace_id: str):
    """整棵树（含 children）。前端瀑布图要层级，所以这里不返回扁平列表。"""
    data = trace_service.trace_tree(trace_id)
    if data is None:
        raise HTTPException(404, f"trace {trace_id} 不存在（可能已过期清理或未落库）")
    return data


@router.get("/{trace_id}/flat")
def flat(trace_id: str):
    """扁平 DFS 列表，给不需要递归的消费方（导出、断言脚本）。"""
    rows = trace_service.flat_tree(trace_id)
    if not rows:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    return {"spans": rows}
