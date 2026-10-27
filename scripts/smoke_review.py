"""阶段 4 冒烟测试（无需 LLM）：SRS 到期/自测 → 掌握度更新 → 历史面试回写错题与缺口卡片 → 每日简报。
运行：python scripts/smoke_review.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main():
    with TestClient(app) as client:
        # 1. 建一张新卡（Kafka 主题，无掌握度记录）-> 应出现在今日到期
        r = client.post("/api/cards", json={
            "title": "Kafka 消息不丢失", "domain": "bigdata", "topic": "Kafka",
            "question": "Kafka 怎么保证消息不丢？", "points": ["producer acks=all", "broker 副本+min.insync.replicas", "consumer 手动提交"]})
        assert r.status_code == 200, r.text
        card_id = r.json()["id"]
        due = client.get("/api/srs/due").json()
        assert any(c["id"] == card_id for c in due), "新卡未进入今日到期"
        print(f"[1/5] 新卡 {card_id} 进入今日到期（共 {len(due)} 张）")

        # 2. 自测通过 -> 掌握度 +0.5，明天再来
        r = client.post("/api/srs/review", json={"card_id": card_id, "result": "pass"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["mastery"] == 0.5 and j["interval_days"] == 1, j
        print(f"[2/5] 自测通过：掌握度 {j['mastery']}，{j['interval_days']} 天后到期")

        # 3. 对历史已结束的模拟面试补跑回写（幂等）
        sid = "mock-20261026-234949-4787"
        r = client.post(f"/api/mock/sessions/{sid}/writeback")
        assert r.status_code == 200, r.text
        wb = r.json()
        print(f"[3/5] 回写完成：错题 {wb['mistakes_created']} 道，卡片调整 {len(wb['cards_updated'])} 张，自动建卡 {wb['cards_created']}")
        assert wb["mistakes_created"] >= 1, "弱项未生成错题"
        assert any(x["delta"] == -1 for x in wb["cards_updated"]), "Flink 卡片掌握度未扣减"

        # 4. 再跑一次验证幂等
        r2 = client.post(f"/api/mock/sessions/{sid}/writeback").json()
        assert r2["mistakes_created"] == 0, f"回写不幂等：{r2}"
        print("[4/5] 幂等验证通过（重复回写 0 新错题）")

        # 5. 每日简报
        b = client.get("/api/daily").json()
        assert b["open_mistakes"] >= 1 and b["due_cards"] >= 1
        print(f"[5/5] 简报：到期 {b['due_cards']} 张，未解决错题 {b['open_mistakes']}，近 3 场均分 {b['recent_mock_avgs']}")

    print("\n=== 阶段 4 冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
