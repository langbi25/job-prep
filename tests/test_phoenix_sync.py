"""P2 双写层集成测试：需要本机 Phoenix（6006），不可达则整组跳过。

覆盖四件事：
1. 数据集 upsert 幂等：同内容第二次跳过上传、返回同一 dataset id；
2. read-after-write 可见性窗口：upsert 后立即取 examples 也能拿到（内部重试）；
3. experiment + run + evaluation 全链路落库，可从服务端读回；
4. 重复推 run 的 409 幂等：成功 run 不可变，重推不炸、不重复。
"""
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.observability import phoenix_sync as ps

PHOENIX_UP = ps.available()
NOW = datetime.datetime.now(datetime.timezone.utc)



def test_dataset_upsert_idempotent():
    if not PHOENIX_UP:
        print("SKIP: Phoenix 不可达"); return
    inputs = [{"query": "什么是反压？"}, {"query": "checkpoint 怎么选？"}]
    outputs = [{"expected_card_id": "kb-a"}, {"expected_card_id": "kb-b"}]
    a = ps.upsert_dataset("_test_upsert", "t", inputs, outputs)
    b = ps.upsert_dataset("_test_upsert", "t", inputs, outputs)
    assert a["id"] == b["id"], "同内容 upsert 应回到同一 dataset"
    assert b["skipped"] is True, "同内容第二次应跳过上传"
    assert len(a["examples"]) == 2, f"应有 2 个 example，实际 {len(a['examples'])}"
    # content 变了 → 走上传，产出新版本但仍是同一 dataset（action=update 语义）
    c = ps.upsert_dataset("_test_upsert", "t", inputs,
                          [{"expected_card_id": "kb-a2"}, {"expected_card_id": "kb-b"}])
    assert c["id"] == a["id"] and c["skipped"] is False
    assert len(c["examples"]) == 2
    print("ok 数据集 upsert 幂等 + 内容变更走新版本")


def test_experiment_run_eval_roundtrip():
    if not PHOENIX_UP:
        print("SKIP: Phoenix 不可达"); return
    ds = ps.upsert_dataset("_test_roundtrip", "t",
                           [{"q": "hi"}], [{"exp": 1}])
    exp_id = ps.create_experiment(ds["id"], f"_test_exp {NOW:%H%M%S}", description="t")
    eid = ds["examples"][0]["id"]
    run_id = ps.log_run(exp_id, eid, {"got": 1}, start=NOW, end=NOW)
    assert run_id, "run 应成功创建"
    ps.log_evaluation(run_id, "hit", score=1.0, label="ok", explanation="测试")
    # 重复推同一 run：409 → 返回空串，不炸
    rid2 = ps.log_run(exp_id, eid, {"got": 1}, start=NOW, end=NOW)
    assert rid2 == "", "重复 run 应被 409 幂等吸收"
    # 服务端读回
    import httpx
    with httpx.Client(base_url=ps.BASE, trust_env=False, timeout=10) as c:
        runs = c.get(f"/v1/experiments/{exp_id}/runs").json()["data"]
        assert len(runs) == 1 and runs[0]["output"] == {"got": 1}
    print("ok experiment/run/evaluation 落库可读回 + 409 幂等")


def test_spa_html_trap():
    """不存在的路由返回 200+HTML（SPA 兜底）：重试封装必须能识别并重试到超时。"""
    if not PHOENIX_UP:
        print("SKIP: Phoenix 不可达"); return
    raised = False
    try:
        ps._get_json_with_retry(ps._client(), "/v1/no/such/route-with-200-html")
    except RuntimeError as e:
        raised = True
        assert "重试" in str(e)
    assert raised, "HTML 兜底页不能被当成 JSON 命中"
    print("ok SPA HTML 兜底不被误判为命中")


if __name__ == "__main__":
    test_dataset_upsert_idempotent()
    test_experiment_run_eval_roundtrip()
    test_spa_html_trap()
    print("3/3 通过" if PHOENIX_UP else "0/3（Phoenix 未启动，全部跳过）")
