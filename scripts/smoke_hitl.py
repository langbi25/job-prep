"""HITL 冒烟测试（merge_card / archive_card）：全程不调 LLM。

链路：起草（此处直接插入 pending 动作，等价于教练 Agent 的产出）→ 审批执行 →
验证 DB 状态、md frontmatter 同步、向量清理、检索不再返回归档卡、非法用例拦截。
运行：python scripts/smoke_hitl.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import ensure_dirs, get_conn, init_db  # noqa: E402
from app.core import config  # noqa: E402
from app.services import multi_agent as ma  # noqa: E402
from app.services import kb_service, rag_service  # noqa: E402


def _insert_action(kind: str, payload: dict) -> str:
    aid = ma._new_action_id()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_actions (id,run_id,session_id,kind,payload_json,reason,status,created_at) "
            "VALUES (?,NULL,NULL,?,?,'冒烟测试','pending',?)",
            (aid, kind, json.dumps(payload, ensure_ascii=False), ma._now()),
        )
        conn.commit()
    finally:
        conn.close()
    return aid


def main():
    ensure_dirs()
    init_db()
    keeper_id = "kb-bigdata-kafka-002"

    # 1) 造一张重复卡，确保它进了向量索引
    dup = kb_service.create_card({
        "title": "Kafka 消息不丢失（旧版笔记）", "domain": "bigdata", "topic": "Kafka",
        "question": "Kafka 怎么保证消息不丢失？", "points": ["acks=all", "重试"], "tags": ["冒烟"],
    })
    dup_id = dup["id"]
    rag_service.ensure_all_indexed()
    conn = get_conn()
    n_vec = conn.execute("SELECT COUNT(*) n FROM embeddings WHERE entity_type='card' AND entity_id=?",
                         (dup_id,)).fetchone()["n"]
    conn.close()
    assert n_vec == 1, f"重复卡应已有向量，实际 {n_vec}"
    print(f"[1/6] 造重复卡 OK：{dup_id}（向量已建）")

    # 2) merge_card 审批执行
    aid = _insert_action("merge_card", {"keeper_card_id": keeper_id, "duplicate_card_id": dup_id})
    r = ma.decide(aid, True)
    assert r["status"] == "approved", r
    conn = get_conn()
    d = conn.execute("SELECT status, merged_into FROM kb_cards WHERE id=?", (dup_id,)).fetchone()
    k = conn.execute("SELECT status, mastery FROM kb_cards WHERE id=?", (keeper_id,)).fetchone()
    n_vec = conn.execute("SELECT COUNT(*) n FROM embeddings WHERE entity_type='card' AND entity_id=?",
                         (dup_id,)).fetchone()["n"]
    conn.close()
    assert d["status"] == "archived" and d["merged_into"] == keeper_id, dict(d)
    assert k["status"] == "active", dict(k)
    assert n_vec == 0, "归档卡的向量未清理"
    dup_md = (config.KB_DIR / dup["file_path"]).read_text(encoding="utf-8")
    assert "status: archived" in dup_md and f"merged_into: {keeper_id}" in dup_md, "md 未同步"
    print(f"[2/6] merge_card OK：{dup_id} → {keeper_id}（向量已清、md 已同步）")

    # 3) hybrid 检索不再返回归档卡
    hits = [h["id"] for h in rag_service.hybrid_search("Kafka 消息不丢失", top_k=10)]
    assert dup_id not in hits, f"归档卡仍出现在检索结果: {hits}"
    assert keeper_id in hits, f"保留卡反而丢了: {hits}"
    print(f"[3/6] 检索验证 OK：归档卡已消失，保留卡在列")

    # 4) archive_card 审批执行
    obs = kb_service.create_card({
        "title": "过时卡片（冒烟用）", "domain": "cs", "topic": "测试",
        "question": "x", "points": [], "tags": ["冒烟"],
    })
    aid2 = _insert_action("archive_card", {"card_id": obs["id"]})
    r2 = ma.decide(aid2, True)
    assert r2["status"] == "approved", r2
    obs_md = (config.KB_DIR / obs["file_path"]).read_text(encoding="utf-8")
    assert "status: archived" in obs_md
    print(f"[4/6] archive_card OK：{obs['id']}")

    # 5) 非法用例：自己合并自己 → 动作标记 failed
    aid3 = _insert_action("merge_card", {"keeper_card_id": keeper_id, "duplicate_card_id": keeper_id})
    r3 = ma.decide(aid3, True)
    assert r3["status"] == "failed" and "自己" in r3["error"], r3
    print(f"[5/6] 非法用例拦截 OK：{r3['error']}")

    # 6) 幂等：已处理动作重复审批被拒
    try:
        ma.decide(aid, True)
        raise AssertionError("重复审批未被拒绝")
    except ValueError as e:
        assert "已处理" in str(e)
    print(f"[6/6] 幂等校验 OK：重复审批被拒")

    # 清理冒烟造的卡片（md + 库行），动作记录保留作为审计轨迹
    conn = get_conn()
    for c in (dup, obs):
        (config.KB_DIR / c["file_path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM kb_cards WHERE id=?", (c["id"],))
    conn.commit()
    conn.close()
    print("\n=== HITL 冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
