"""阶段 3 冒烟测试：组卷（题池+生成补充）→ 逐题作答评分 → 总评报告。
运行：python scripts/smoke_mock.py（会真实调用 DeepSeek，约 5 次请求）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

FAKE_ANSWERS = [
    "Spark shuffle 有 hash shuffle 和 sort-based shuffle。早期 hash shuffle 每个 map task 给每个 reduce 写一个文件，小文件太多；"
    "1.2 之后默认 sort shuffle，map 端按 partition 排序写一个数据文件加索引，reduce 端按索引拉取。"
    "Tungsten-sort 又做了堆外内存和序列化优化。我觉得核心问题是磁盘 IO 和小文件。",
    "Flink 反压就是下游消费跟不上上游生产速度，导致上游 buffer 堆积。排查看 Flink WebUI 的 backpressure 标红，"
    "再用 Metrics 看 outPoolUsage 和 inPoolUsage，找吞吐最低的算子，通常是那里有慢逻辑比如访问外部服务。",
    "",
]


def main():
    with TestClient(app) as client:
        r = client.post("/api/mock/sessions", json={
            "direction": "bigdata", "persona": "某大厂数据平台负责人", "question_count": 3})
        assert r.status_code == 200, r.text
        s = r.json()
        sid = s["session_id"]
        print(f"[1/4] 组卷完成 {sid}：{len(s['questions'])} 题（题池 {sum(1 for q in s['questions'])} 道）")
        for q in s["questions"]:
            print(f"    - [{q['qtype']}|{q['topic']}] {q['text'][:40]}")

        for i, ans in enumerate(FAKE_ANSWERS[:len(s["questions"])]):
            r = client.post(f"/api/mock/sessions/{sid}/answer", json={"index": i, "answer_text": ans})
            assert r.status_code == 200, r.text
            ev = r.json()["evaluation"]
            sc = ev["scores"]
            print(f"[2/4] 第{i+1}题评分：正确性{sc['correctness']} 深度{sc['depth']} "
                  f"结构{sc['structure']} 亮点{sc['highlight']}｜{ev['feedback'][:60]}...")

        r = client.post(f"/api/mock/sessions/{sid}/finish")
        assert r.status_code == 200, r.text
        o = r.json()
        print(f"[3/4] 总评：均分{o['avg']} 总分{o['overall_avg']}｜{o['report']['summary'][:80]}...")

        assert Path("..", o["report_path"]).exists() or Path(o["report_path"]).exists(), "报告未归档"
        r = client.get("/api/mock/sessions")
        assert any(x["id"] == sid for x in r.json())
        print(f"[4/4] 报告归档 {o['report_path']}，会话列表可见")

    print("\n=== 阶段 3 冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
