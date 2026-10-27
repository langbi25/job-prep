"""阶段 5 深化冒烟测试：Prompt 注册表 / 向量索引 / 混合检索 / 上下文组装 / 雷达 / 冲刺组卷 / 学习教练 Agent。
运行：python scripts/smoke_deep.py（首次会下载 bge-small-zh 模型约 100MB；含少量 LLM 调用）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import embedder, prompts  # noqa: E402
from app.core.db import init_db, ensure_dirs  # noqa: E402
from app.main import app  # noqa: E402
from app.services import context_builder, rag_service  # noqa: E402


def main():
    ensure_dirs()
    init_db()  # 确保 embeddings/runs 等新表存在（幂等）
    # 1. Prompt 注册表
    system, user, ver = prompts.render("evaluate_answer", persona="测试官", qtype="八股",
                                       question_text="Q", answer="A", context="CTX")
    assert "{persona}" not in system and "测试官" in system and "CTX" in user
    print(f"[1/6] prompt 注册表 OK（evaluate_answer v{ver}，6 个 prompt 可加载）")
    for name in ("extract_interview", "generate_questions", "overall_report", "tag_card", "study_plan"):
        prompts.load(name)

    # 2. 向量索引（首次运行会下载模型；重复运行 0 新增 = 幂等）
    assert embedder.available(), "fastembed 未安装"
    idx = rag_service.ensure_all_indexed()
    from app.core.db import get_conn
    _conn = get_conn()
    _n = _conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    _conn.close()
    assert idx["enabled"] and _n > 0, f"索引失败: {idx}"
    print(f"[2/6] 向量索引 OK：库内 {[_n]} 条向量（本次新建 {idx['cards']} 卡 + {idx['questions']} 题，bge-small-zh dim={embedder.EMBED_DIM}）")

    # 3. 混合检索：语义换说法也要命中
    hits = rag_service.hybrid_search("持久化 快照 崩溃恢复", scope="cards", top_k=3)
    ids = [h["id"] for h in hits]
    assert "kb-backend-redis-001" in ids, f"混合检索未命中 Redis 卡: {hits}"
    legs = next(h["legs"] for h in hits if h["id"] == "kb-backend-redis-001")
    print(f"[3/6] 混合检索 OK：Redis 卡命中，命中路 {legs}")

    # 4. 上下文组装器：相关、有界
    ctx = context_builder.build_eval_context("Flink", "Flink 反压怎么排查？")
    assert len(ctx) > 20 and len(ctx) <= context_builder.MAX_CHARS + 50, f"上下文异常: len={len(ctx)}"
    print(f"[4/6] 上下文组装 OK：{len(ctx)} 字符（预算 {context_builder.MAX_CHARS}）")

    # 5. 雷达 + 公司冲刺组卷
    with TestClient(app) as client:
        r = client.get("/api/radar")
        assert r.status_code == 200 and "by_domain" in r.json()
        r = client.post("/api/mock/sessions", json={
            "direction": "bigdata", "question_count": 3, "company": "XX厂"})
        assert r.status_code == 200, r.text
        qs = r.json()["questions"]
        print(f"[5/6] 雷达 OK；冲刺组卷 OK（XX厂 优先，{len(qs)} 题）")

        # 6. 学习教练 Agent（真实 LLM + 工具调用 + trace 落库）
        r = client.post("/api/agent/plan", json={"direction": "bigdata"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["status"] == "done", f"Agent 失败: {j}"
        tools_used = {s["tool"] for s in j["trace"] if s.get("tool") != "final_answer"}
        assert len(tools_used) >= 2, f"Agent 工具调用不足: {tools_used}"
        runs = client.get("/api/agent/runs").json()
        assert any(x["id"] == j["run_id"] for x in runs)
        print(f"[6/6] 学习教练 Agent OK：{len(tools_used)} 个工具，{len(j['plan'].get('plan', []))} 条计划，轨迹已落库")

    print("\n=== 阶段 5 深化冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
