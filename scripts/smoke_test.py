"""冒烟测试：建库 → 建卡 → md 同步校验 → 投喂 → 确认入库 → 统计。
运行：python scripts/smoke_test.py（不依赖 LLM API Key）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main():
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True
        print("[1/6] health OK")

        r = client.post("/api/cards", json={
            "title": "Redis 持久化 RDB vs AOF", "domain": "backend", "topic": "Redis",
            "question": "Redis 的 RDB 和 AOF 各自原理与取舍？",
            "points": ["RDB：快照，fork 子进程", "AOF：追加命令日志，everysec 折中"],
            "pitfalls": ["别把全量/增量说反"], "tags": ["高频"],
        })
        assert r.status_code == 200, r.text
        card = r.json()
        print(f"[2/6] card created: {card['id']} -> {card['file_path']}")
        assert (Path("knowledge") / card["file_path"]).exists(), "md 文件未生成"

        r = client.get("/api/cards", params={"q": "AOF"})
        assert r.status_code == 200 and any(c["id"] == card["id"] for c in r.json()), "FTS 检索失败"
        print("[3/6] FTS search OK")

        raw = "面试官：先聊聊 Spark shuffle 吧，几种实现说说区别。后面问了数仓分层怎么设计的，为什么这么分。HR 说一周内出结果。"
        r = client.post("/api/ingests", json={"source_type": "paste", "text": raw, "url": "https://www.nowcoder.com/discuss/123"})
        assert r.status_code == 200, r.text
        ingest_id = r.json()["id"]
        print(f"[4/6] ingest created: {ingest_id}")

        r = client.post(f"/api/ingests/{ingest_id}/confirm", json={
            "company": "XX厂", "position": "大数据开发", "direction": "bigdata", "round": "二面",
            "questions": [
                {"keep": True, "text": "Spark shuffle 的几种实现与区别？", "qtype": "八股", "topic": "Spark"},
                {"keep": True, "text": "数仓分层怎么设计的，分层依据是什么？", "qtype": "项目深挖", "topic": "数仓建模"},
                {"keep": False, "text": "HR 说一周内出结果"},
            ],
        })
        assert r.status_code == 200, r.text
        print(f"[5/6] confirmed: {r.json()}")

        # 重复确认第二份含相似题的面经，验证去重 + 频次累加
        r2 = client.post("/api/ingests", json={"source_type": "paste", "text": "Spark shuffle 几种实现与区别再问了一遍。"})
        client.post(f"/api/ingests/{r2.json()['id']}/confirm", json={
            "direction": "bigdata",
            "questions": [{"keep": True, "text": "Spark shuffle 的几种实现与区别？", "qtype": "八股", "topic": "Spark"}],
        })
        s = client.get("/api/stats").json()
        top = {q["id"]: q["freq_count"] for q in s["top_questions"]}
        spark = [k for k, v in top.items() if v >= 2]
        assert spark, f"频次未累加: {top}"
        iv_files = list(Path("interviews").rglob("*.md"))
        assert iv_files, "面经原文未归档"
        print(f"[6/6] stats OK: cards={s['cards_by_domain']}, freq merged {top}, archived={len(iv_files)}")

    print("\n=== 冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
