"""向量层：bge-small-zh-v1.5（fastembed/ONNX）+ SQLite 存向量。

设计决策：数据量个人级（<1万条），余弦相似度直接 numpy 全量算，
不引入 Chroma 常驻服务——2核2G 服务器友好，架构可讲："按数据规模选型"。
模型走 HF 镜像下载；fastembed 未安装时全链路优雅降级（回退 FTS）。
"""
import hashlib
import json
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # 国内模型下载镜像
os.environ.setdefault(
    "FASTEMBED_CACHE_PATH", os.path.expanduser("~/.cache/huggingface/hub")
)  # 模型缓存放稳定目录（Temp 会被清理）

import numpy as np

from .db import get_conn

_model = None
EMBED_DIM = 512  # bge-small-zh-v1.5


def available() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    return [np.asarray(v, dtype=np.float32) for v in model.embed(texts)]


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def ensure_indexed(entity_type: str, items: list[tuple[str, str]]) -> int:
    """items: [(entity_id, text)]。为内容变更/新增的条目建立向量。返回新增数。"""
    if not available() or not items:
        return 0
    conn = get_conn()
    try:
        known = {
            r["entity_id"]: r["text_hash"]
            for r in conn.execute("SELECT entity_id, text_hash FROM embeddings WHERE entity_type=?", (entity_type,))
        }
    finally:
        conn.close()
    todo = [(i, t) for i, t in items if _hash(t) != known.get(i)]
    if not todo:
        return 0
    vectors = embed([t for _, t in todo])
    conn = get_conn()
    try:
        for (eid, text), v in zip(todo, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (entity_type, entity_id, text_hash, vector) VALUES (?,?,?,?)",
                (entity_type, eid, _hash(text), json.dumps(v.tolist())),
            )
        conn.commit()
    finally:
        conn.close()
    return len(todo)


def search_similar(query: str, entity_type: str, top_k: int = 5) -> list[tuple[str, float]]:
    """返回 [(entity_id, cosine)]，按相似度降序。未启用向量时返回空。"""
    if not available():
        return []
    qv = embed([query])[0]
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT entity_id, vector FROM embeddings WHERE entity_type=?", (entity_type,)
        ).fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        v = np.asarray(json.loads(r["vector"]), dtype=np.float32)
        denom = float(np.linalg.norm(qv) * np.linalg.norm(v))
        cos = float(np.dot(qv, v) / denom) if denom else 0.0
        scored.append((r["entity_id"], cos))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]
