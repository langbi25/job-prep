"""记忆检索：决定"这一轮该想起什么"。

**两条通道，一个门**

词面通道（2-gram 重叠 + 主题命中）擅长精确命中：用户说"Kafka 丢消息"，
记忆 key 就是 Kafka，命中是确定的；但它对改写无能为力——"消息不丢失"
与"丢消息"没有一个 2-gram 相同，纯词面会漏。

向量通道（bge-small-zh，复用 core/embedder.py）擅长语义：上面那对能拿到
0.7+ 的 cosine。代价是一次 embedding（几十毫秒）和"不可解释"。

融合方式不是加权平均，是 **max 取优**：

    rel = max(词面分, 向量分)

理由：两个通道的失效场景不重叠，平均会让"一个通道确定命中 + 另一个通道噪声"
时分数被拉低，反而更容易掉到阈值下。max 保证任一路强命中就够。

（第一版给向量乘了 0.85 的折扣，理由是"用户真说了这个词比语义相近更可信"。
实测发现这条折扣只影响"仅向量命中"的场景——那时词面已经是 0，没有任何东西
需要它去让位，折扣纯粹是压低召回率。删掉。）

**为什么向量必须过绝对阈值（VEC_MIN）**

这是上向量最容易踩的坑：cosine 没有绝对零点，bge 对两段任意中文的相似度
通常落在 0.3~0.45，真正相关才 0.55+。如果直接把 cosine 当相关性，等于给
所有记忆都发了一张及格券——上一版"无关 query 靠新鲜度凑分"的 bug 会以
更隐蔽的形式复发（这次是"所有记忆都靠语义凑分"）。

所以向量分先过门再映射：

    vec_rel = 0                                     if cos < VEC_MIN
    vec_rel = 0.30 + 0.68 × (cos-VEC_MIN)/(VEC_GOOD-VEC_MIN)   otherwise

"过门即 0.30"是有意的：过了绝对门就意味着"至少有点相关"，给一个及格分，
而不是让它线性爬到 0.05 再被 MIN_SCORE 拦掉。具体数值见常量处的实测记录。

**为什么相关性必须是"门"，不能是加项**

第一版把时间/置信度当成独立加分项，结果**完全无关**的 query 也能拿到
0.12(时间)+0.09(置信)=0.21 分，越过阈值被注入——模型会把不相关的记忆当成
前提，比没有记忆更糟。现在相关性为 0 直接判 0，时间再新也进不来。
时间/置信度/访问热度只是 0.6~1.0 的修正系数，只能扣分不能救活。

偏好类例外（PREF_BASE）：偏好只有几条，且几乎总是与当前会话相关
（"讲题先给结论"），字面匹配不中不代表不适用。给它一个基础相关性，
但仍参与排序与预算竞争。

**降级**：fastembed 没装、模型没下载、离线——任一情况都在进程内永久关掉
向量通道（`_vec_state["off"]`）。不这么做的话每次召回都会重试一次模型加载，
一轮对话卡几秒，而用户只会觉得"这个 agent 很慢"。
"""
import datetime
import re

from ..core import config
from . import store

HALF_LIFE_DAYS = 30
MIN_SCORE = 0.15
PREF_BASE = 0.30          # 偏好类的基础相关性（字面不中时兜底）
KIND_WEIGHT = {"preference": 1.0, "fact": 1.0, "task_state": 0.9, "episode": 0.7}
KIND_LABEL = {"preference": "偏好", "fact": "事实", "task_state": "任务状态", "episode": "情景"}
SESSION_BONUS = 0.05      # 同一场会话里的记忆，比全局的略优先
SNIPPET_CHARS = 240
EMBED_MAX_CHARS = 600     # bge-small 的 512 token 上限，中文约 500 字，留余量

# 阈值是按**实测分布**校准的，不是拍脑袋（2026-11-12，8 条真实记忆 + 5 个 query）：
#   无关（"今天天气怎么样"）          0.32 ~ 0.33
#   语义等价但措辞完全不同（"看懂代码却答不上" vs "学过但调不出来"） 0.49
#   同主题改写（"负载不均" vs "数据倾斜"）                          0.55
#   直接命中（"Flink 数据倾斜怎么优化"）                            0.68
# 第一版按想象取了 0.52 / 0.90，结果把 0.495 那条改写挡在门外——
# 向量通道存在的意义就是接住词面接不住的改写，门槛定在"明显相关"之上等于自废武功。
VEC_MIN = 0.45            # 绝对下限：低于它与"无关"不可区分
VEC_GOOD = 0.72           # 明显相关：超过它按满分计
# 映射不是线性归一到 [0,1]：bge 的分数挤在 0.3~0.75，直接归一后 0.55 只剩 0.22 分，
# 再乘 0.9 的修正系数就掉到阈值以下——"过门了却召回不到"比不过门更让人困惑。
# FLOOR 取 0.22 是反推出来的：0.22 × 0.9(mult) × 0.7(episode 权重) ≈ 0.14，
# 刚好贴着 MIN_SCORE 0.15。含义是"刚过门 = 刚好够格"，既不会白占一个注入位，
# 也不会让 0.495 这种真实弱相关信号因为归一化被稀释到召不回来。
VEC_FLOOR = 0.22
VEC_TOP = 0.98

_PUNCT = re.compile(r"[\s\u3000·・,，.。;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_=+*#~`-]+")

# 进程内状态：向量通道挂了就不要再试，否则每次召回都白等一次模型加载
_vec_state = {"off": False, "reason": ""}


def _grams(text: str, n: int = 2) -> set[str]:
    t = _PUNCT.sub("", (text or "").lower())
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _age_days(ts: str) -> float:
    try:
        return max(0.0, (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 999.0


def vector_status() -> dict:
    """给面板/接口看的向量通道状态。'为什么没走语义召回'要能回答。"""
    if config.MEMORY_VECTOR in ("0", "off", "no", "false"):
        return {"enabled": False, "reason": "config_off"}
    if _vec_state["off"]:
        return {"enabled": False, "reason": _vec_state["reason"] or "unavailable"}
    try:
        from ..core import embedder
        if not embedder.available():
            return {"enabled": False, "reason": "fastembed_missing"}
        return {"enabled": True, "reason": "", "model": embedder.MODEL_ID,
                "min_cos": VEC_MIN}
    except Exception as e:  # noqa: BLE001
        return {"enabled": False, "reason": f"probe_error:{type(e).__name__}"}


def _embed_text(row: dict) -> str:
    """索引文本 = key + value。key 是"主题"（Kafka 重复消费），value 是"内容"，
    只嵌 value 会让同主题的多条记忆向量挤在一起，只嵌 key 又丢细节。"""
    return f"{row.get('key') or ''}：{row.get('value') or ''}"[:EMBED_MAX_CHARS]


def _vec_scores(query: str, rows: list[dict]) -> dict[str, float]:
    """返回 {memory_id: cosine}。索引增量维护：文本 hash 没变就不重嵌。

    任何异常都关掉向量通道——宁可这一轮少一路召回，也不要让对话卡在模型加载上。
    """
    st = vector_status()
    if not st["enabled"] or not rows:
        return {}
    try:
        from ..core import embedder
        embedder.ensure_indexed("memory", [(r["id"], _embed_text(r)) for r in rows])
        return dict(embedder.search_similar(query, "memory", top_k=len(rows)))
    except Exception as e:  # noqa: BLE001
        _vec_state["off"] = True
        _vec_state["reason"] = f"{type(e).__name__}: {str(e)[:120]}"
        return {}


def score(query: str, row: dict, vec: float | None = None) -> float:
    """单条记忆的相关性。vec 为 None 表示向量通道没开/没算出来。"""
    qg = _grams(query)
    if not qg:
        return 0.0
    mg = _grams(f"{row.get('key', '')}{row.get('value', '')}")
    overlap = len(qg & mg) / max(1, len(qg))

    key = (row.get("key") or "").lower()
    qn = _PUNCT.sub("", (query or "").lower())
    if key and (key in qn or (len(key) >= 2 and qn in key)):
        key_hit = 1.0
    elif key and len(_grams(key) & qg) >= 2:
        key_hit = 0.5
    else:
        key_hit = 0.0

    rel_lex = 0.45 * overlap + 0.25 * key_hit
    rel_vec = 0.0
    if vec is not None and vec >= VEC_MIN:
        t = min(1.0, (vec - VEC_MIN) / (VEC_GOOD - VEC_MIN)) if VEC_GOOD > VEC_MIN else 1.0
        rel_vec = VEC_FLOOR + (VEC_TOP - VEC_FLOOR) * t

    rel = max(rel_lex, rel_vec)
    if rel <= 0:
        if row.get("kind") != "preference":
            return 0.0
        rel = PREF_BASE
    decay = 0.5 ** (_age_days(row.get("updated_at") or row.get("created_at") or "") / HALF_LIFE_DAYS)
    conf = float(row.get("confidence") or 0.7)
    access = min(1.0, float(row.get("access_count") or 0) / 5.0)

    mult = 0.6 + 0.20 * decay + 0.15 * conf + 0.05 * access
    return round(rel * mult * KIND_WEIGHT.get(row.get("kind"), 0.8), 4)


def _match_path(row: dict, query: str, vec: float | None) -> str:
    """命中路径，写进返回结果。面板上"为什么想起这条"必须能答。"""
    qg = _grams(query)
    mg = _grams(f"{row.get('key', '')}{row.get('value', '')}")
    lex = bool(qg & mg)
    semantic = vec is not None and vec >= VEC_MIN
    if lex and semantic:
        return "both"
    if semantic:
        return "vec"
    return "lex" if lex else "kind"


def recall(query: str, *, session_id: str | None = None, kinds: list[str] | None = None,
           top_k: int = 6, max_tokens: int | None = None, mark_access: bool = True) -> list[dict]:
    """按 query 取相关记忆。返回 [{id,kind,key,value,score,match,vec,confidence,...}]，分数降序。

    范围：全局记忆 + 当前会话记忆。会话记忆只在同一 session_id 内可见——
    "这次面试聊到一半的判断"不该泄漏到下一次面试。
    """
    if not (query or "").strip():
        return []
    pool = store.list_memory(scope="global", kinds=kinds, limit=store.MAX_PER_SCOPE)
    if session_id:
        pool += store.list_memory(scope="session", scope_id=session_id, kinds=kinds,
                                  limit=store.MAX_PER_SCOPE)
    if not pool:
        return []

    vecs = _vec_scores(query, pool)

    scored = []
    for row in pool:
        v = vecs.get(row["id"])
        s = score(query, row, v)
        if s <= 0:
            continue
        bonus = SESSION_BONUS if row.get("scope") == "session" and row.get("scope_id") == session_id else 0.0
        scored.append({**row, "score": round(s + bonus, 4),
                       "match": _match_path(row, query, v),
                       "vec": round(v, 3) if v is not None else None})

    scored.sort(key=lambda x: -x["score"])
    hits = [h for h in scored if h["score"] >= MIN_SCORE][:max(1, top_k)]

    if max_tokens:
        kept, used = [], 0
        for h in hits:
            t = max(1, int(len(h["value"]) * 0.7)) + 8
            if used + t > max_tokens:
                break
            kept.append(h)
            used += t
        hits = kept

    if mark_access and hits:
        store.touch([h["id"] for h in hits])
    return hits


def format_for_prompt(hits: list[dict], max_chars: int | None = None) -> str:
    """包成注入 system 的一段。带来源标注，让模型知道这是"记得"而不是"刚查到的"。"""
    if not hits:
        return ""
    lines = ["【关于用户的既有记忆（来自之前对话，可能过时；与本轮冲突时以用户当前说法为准）】"]
    used = 0
    for h in hits:
        line = f"- [{KIND_LABEL.get(h['kind'], h['kind'])}] {h['key']}：{h['value'][:SNIPPET_CHARS]}"
        if max_chars and used + len(line) > max_chars:
            lines.append("- …（更多记忆已按预算省略）")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
