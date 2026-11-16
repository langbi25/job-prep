"""压测脚本聚合与判定逻辑的测试（零网络——只测纯函数）。

口径错了的压测比没有压测更糟：把 429 算进错误率，"限流在正常工作"
会被判成"系统故障"；用插值分位数，3 个样本能报出一个不存在的 P95。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import load_test  # noqa: E402


# --- aggregate：单阶段口径 ------------------------------------------------------

def test_aggregate_empty_is_error_not_zero():
    out = load_test.aggregate([], [], [], 10.0)
    assert out["all_failed"] is True and out["error_rate"] == 1.0


def test_aggregate_429_is_limited_not_error():
    """开了限流后 429 = 闸门在工作，单列不算错误率。"""
    out = load_test.aggregate([10.0] * 8, [200] * 6 + [429, 429], [""] * 8, 2.0)
    assert out["error_rate"] == 0.0
    assert out["limited_rate"] == 0.25
    assert out["ok"] == 6 and out["limited_429"] == 2


def test_aggregate_counts_real_errors():
    statuses = [200, 500, 502, 404, 0, 0]  # 0 = 异常没拿到状态码
    errors = [""] * 4 + ["TimeoutError: x", "ConnectError: y"]
    out = load_test.aggregate([10.0] * 6, statuses, errors, 1.0)
    assert out["server_err"] == 2 and out["client_err"] == 1 and out["exc_count"] == 2
    assert out["error_rate"] == round(5 / 6, 4)  # 全部非 200/429 都算真错误


def test_aggregate_percentile_nearest_rank():
    """最近秩：10 个样本的 P95 = 第 10 个（插值会造出第 9.55 个这种不存在值）。"""
    lat = [float(i) for i in range(1, 11)]  # 1..10 ms
    out = load_test.aggregate(lat, [200] * 10, [""] * 10, 1.0)
    assert out["p50_ms"] == 5.0
    assert out["p95_ms"] == 10.0
    assert out["p99_ms"] == 10.0


def test_aggregate_rps_uses_wall_time():
    out = load_test.aggregate([10.0] * 40, [200] * 40, [""] * 40, 4.0)
    assert out["rps"] == 10.0


# --- judge：跨阶段判定 ----------------------------------------------------------

def _stage(conc, p95, err=0.0, n=100, limited=0.0):
    return {"concurrency": conc, "requests": n, "p95_ms": p95,
            "error_rate": err, "limited_rate": limited}


def test_judge_pass_when_flat():
    stages = [_stage(1, 50), _stage(4, 60), _stage(8, 70)]
    assert load_test.judge(stages)["pass"] is True


def test_judge_fails_on_p95_blowup():
    stages = [_stage(1, 50), _stage(8, 150)]  # 3x 基线
    v = load_test.judge(stages)
    assert v["pass"] is False
    assert any("P95" in r for r in v["reasons"])


def test_judge_fails_on_error_rate():
    stages = [_stage(1, 50, err=0.02), _stage(8, 55, err=0.10)]
    v = load_test.judge(stages)
    assert v["pass"] is False
    assert any("错误率" in r for r in v["reasons"])


def test_judge_no_requests_at_all():
    assert load_test.judge([_stage(1, 0, n=0)])["pass"] is False


def test_judge_skips_limiter_dominated_stages():
    """混合场景：非限流阶段当基线，限流主导阶段（429 ≥ 50%）只判错误率不比 P95。"""
    stages = [_stage(1, 50), _stage(8, 500, limited=0.9)]  # 后者 P95 10 倍也不报
    assert load_test.judge(stages)["pass"] is True


def test_judge_all_limited_is_inconclusive_fail():
    """所有阶段都被限流封顶 = 这次压测没测到服务，明确 FAIL 并给出处置建议。"""
    stages = [_stage(1, 50, limited=0.92), _stage(4, 60, limited=0.95)]
    v = load_test.judge(stages)
    assert v["pass"] is False
    assert any("限流主导" in r for r in v["reasons"])


def test_judge_limiter_dominated_stage_still_checks_errors():
    stages = [_stage(1, 50, limited=0.9), _stage(8, 500, err=0.2, limited=0.9)]
    v = load_test.judge(stages)
    assert v["pass"] is False
    assert any("错误率" in r for r in v["reasons"])
