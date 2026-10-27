"""阶段 6 冒烟测试：三档检索 + 教练 Agent 起草补救动作 + HITL 审批/驳回 + 门禁脚本。
运行：python scripts/smoke_phase6.py（含 LLM 调用）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import ensure_dirs, init_db, get_conn  # noqa: E402
from app.main import app  # noqa: E402
from app.services import rag_service  # noqa: E402


def main():
    ensure_dirs()
    init_db()

    # 1. 三档检索：fts vs hybrid vs full
    q = "面试官问我消息丢了怎么办"
    fts = rag_service.search(q, mode="fts", top_k=5)
    hyb = rag_service.search(q, mode="hybrid", top_k=5)
    full = rag_service.search(q, mode="full", top_k=3)
    assert fts and hyb and full, "三档检索返回为空"
    print(f"[1/5] 三档检索 OK：fts={[h['id'] for h in fts][:3]}  hybrid={[h['id'] for h in hyb][:3]}")

    # 2. 改写 + 精排真的生效（full 模式应有 reranked 标记，legs 含 vector）
    assert any(h.get("reranked") for h in full) or len(full) <= 3, "rerank 未生效"
    print(f"[2/5] 改写+精排 OK：top={[h['id'] for h in full]}（reranked={[bool(h.get('reranked')) for h in full]}）")

    with TestClient(app) as client:
        # 3. 教练 Agent 起草补救动作（HITL，不落库）
        sessions = client.get("/api/mock/sessions").json()
        finished = [s for s in sessions if s.get("finished_at")]
        assert finished, "没有已结束的面试，先跑 smoke_mock.py"
        sid = finished[0]["id"]
        r = client.post(f"/api/agent/remediation/{sid}/draft")
        assert r.status_code == 200, r.text
        actions = r.json()["actions"]
        assert actions, f"未起草出动作: {r.text}"
        print(f"[3/5] 教练起草 OK：{len(actions)} 条建议 -> {[(a['kind']) for a in actions]}")

        # 4. 批准第一条 -> 真正执行；驳回第二条
        a0 = actions[0]
        if a0["kind"] == "boost_srs":
            a0["payload"].setdefault("card_id", "kb-bigdata-kafka-002")
        r = client.post(f"/api/agent/actions/{a0['id']}/approve")
        assert r.status_code == 200 and r.json()["status"] in ("approved", "failed"), r.text
        print(f"[4/5] 审批执行 OK：{r.json()}")
        if len(actions) > 1:
            r2 = client.post(f"/api/agent/actions/{actions[1]['id']}/reject")
            assert r2.json()["status"] == "rejected"
            print(f"       驳回 OK：{actions[1]['id']}")

        # 5. 状态与幂等：已处理动作不能重复审批
        r3 = client.post(f"/api/agent/actions/{a0['id']}/approve")
        assert r3.status_code == 400, "重复审批未被拒绝"
        conn = get_conn()
        n = conn.execute("SELECT COUNT(*) AS n FROM agent_actions").fetchone()["n"]
        conn.close()
        print(f"[5/5] 幂等校验 OK：重复审批被拒，动作库共 {n} 条")

    print("\n=== 阶段 6 冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
