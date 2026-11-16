"""压测脚本：并发 N 路 → P50/P95/P99、错误率、吞吐，阶梯加压找拐点。

用法（对运行中的服务压）：
    python scripts/load_test.py --stages 1,4,8,16 --duration 10
    python scripts/load_test.py --path /api/search --method POST --payload '{"q":"面试"}'
    python scripts/load_test.py --token xxx --base-url http://127.0.0.1:8000

设计上的三个决定：

1. **目标端点默认 /api/stats**（只读、走 DB、有代表性）。压 /health 没意义
   （不进限流不进 DB），压写接口会把数据压脏——压测默认必须无害。
2. **429 单列不算错误**。开了入口限流后，高并发阶段出现 429 恰恰说明闸门
   在工作；把它算进错误率，"限流正常"和"系统崩了"就分不开了。
3. **拐点判定用相对基线**：任一阶段的 P95 超过最低并发阶段的 2 倍，或错误率
   超 5%，判 FAIL 退出码 1——给 CI 用；人工看表格自己定。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time


def aggregate(latencies_ms: list[float], statuses: list[int],
              errors: list[str], wall_s: float) -> dict:
    """单阶段聚合。纯函数，便于测试钉住口径。"""
    n = len(latencies_ms)
    if n == 0:
        return {"requests": 0, "rps": 0.0, "error_rate": 1.0, "all_failed": True}
    s = sorted(latencies_ms)

    def pct(p: float) -> float:
        # 最近秩（nearest-rank）：第 ceil(p/100*n) 个样本（1 起）。
        # 插值在样本少时会造出不存在的耗时（3 个样本报 P95=第 3.1 个）。
        rank = max(1, math.ceil(p / 100 * n))
        return s[min(rank, n) - 1]

    limited = sum(1 for c in statuses if c == 429)
    ok = sum(1 for c in statuses if c == 200)
    client_err = sum(1 for c in statuses if 400 <= c < 500 and c != 429)
    server_err = sum(1 for c in statuses if c >= 500)
    real_errors = n - ok - limited  # 4xx/5xx/异常——不含 429
    return {
        "requests": n,
        "rps": round(n / wall_s, 1) if wall_s > 0 else 0.0,
        "p50_ms": round(pct(50), 1),
        "p95_ms": round(pct(95), 1),
        "p99_ms": round(pct(99), 1),
        "ok": ok,
        "limited_429": limited,
        "client_err": client_err,
        "server_err": server_err,
        "exc_count": sum(1 for e in errors if e),
        "error_rate": round(real_errors / n, 4),
        "limited_rate": round(limited / n, 4),
        "all_failed": False,
    }


def judge(stages: list[dict], *, max_error_rate: float = 0.05,
          p95_blowup: float = 2.0, limited_dominated: float = 0.5) -> dict:
    """跨阶段判定：错误率绝对阈值 + P95 相对基线（最低并发阶段）。

    429 占比超过 limited_dominated 的阶段算"限流主导"——吞吐被闸门封顶，
    测到的延迟分布是"放行那几条的排队"，和系统容量没关系，跳过 P95 对比。
    错误率检查对所有阶段始终执行（限流主导也可能藏着 500）。
    若所有阶段都被限流主导且无其他问题，判"测不到容量"——压测白跑，给出处置。
    """
    baseline = next((s for s in stages if s["requests"] > 0
                     and s["limited_rate"] < limited_dominated), None)
    reasons: list[str] = []
    for s in stages:
        if s["requests"] == 0:
            continue
        if s["error_rate"] > max_error_rate:
            reasons.append(f"并发 {s['concurrency']}: 错误率 {s['error_rate']:.1%} "
                           f"> {max_error_rate:.0%}")
        if (baseline is not None and s is not baseline
                and s["limited_rate"] < limited_dominated
                and baseline["p95_ms"] > 0
                and s["p95_ms"] > baseline["p95_ms"] * p95_blowup):
            reasons.append(f"并发 {s['concurrency']}: P95 {s['p95_ms']:.0f}ms "
                           f"超基线（{baseline['p95_ms']:.0f}ms）{p95_blowup:.0f} 倍")
    if not reasons and baseline is None:
        return {"pass": False,
                "reasons": ["所有阶段都被限流主导（429 ≥ "
                            f"{limited_dominated:.0%}），测不到服务容量——"
                            "压测前调高 RATE_LIMIT_RPS/BURST 或临时关闭限流"]}
    return {"pass": not reasons, "reasons": reasons}


async def _stage(client, *, method: str, path: str, payload: dict | None,
                 concurrency: int, duration_s: float, timeout_s: float,
                 token: str) -> dict:
    """一个并发阶段：N 个 worker 各自循环打到时间窗结束，收 latencies/statuses。"""
    latencies: list[float] = []
    statuses: list[int] = []
    errors: list[str] = []
    lock = asyncio.Lock()
    headers = {"X-API-Token": token} if token else {}
    deadline = time.monotonic() + duration_s

    async def worker():
        while time.monotonic() < deadline:
            t0 = time.perf_counter()
            status, err = 0, ""
            try:
                kw: dict = {"headers": headers, "timeout": timeout_s}
                if payload is not None:
                    kw["content"] = json.dumps(payload).encode()
                    kw["headers"] = {**headers, "Content-Type": "application/json"}
                resp = await client.request(method, path, **kw)
                status = resp.status_code
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            dt = (time.perf_counter() - t0) * 1000
            async with lock:
                latencies.append(dt)
                statuses.append(status)
                errors.append(err)

    t0 = time.monotonic()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    wall = time.monotonic() - t0
    out = aggregate(latencies, statuses, errors, wall)
    out["concurrency"] = concurrency
    return out


async def run(args) -> tuple[list[dict], dict]:
    import httpx

    token = args.token or os.environ.get("APP_TOKEN", "")
    limits = httpx.Limits(max_connections=max(args.stages) + 10)
    verdict_stages: list[dict] = []
    async with httpx.AsyncClient(base_url=args.base_url, limits=limits) as client:
        # 预检：目标端点不通就直接失败，别压了 30 秒才发现 404
        pre = await client.request(
            args.method, args.path,
            headers={"X-API-Token": token} if token else {},
            content=json.dumps(args.payload).encode() if args.payload else None,
            timeout=args.timeout)
        if pre.status_code >= 500:
            print(f"[预检失败] {args.path} 返回 {pre.status_code}，先修服务再压测",
                  file=sys.stderr)
            sys.exit(2)
        if pre.status_code in (401, 403):
            print("[预检失败] 401/403：传 --token 或设 APP_TOKEN 环境变量", file=sys.stderr)
            sys.exit(2)

        for conc in args.stages:
            print(f"--- 并发 {conc} × {args.duration}s → {args.method} {args.path}")
            out = await _stage(client, method=args.method, path=args.path,
                               payload=args.payload, concurrency=conc,
                               duration_s=args.duration, timeout_s=args.timeout,
                               token=token)
            verdict_stages.append(out)
            note = "  ← 限流主导，只判错误率" if out["limited_rate"] >= 0.5 else ""
            print(f"    {out['requests']} 请求 / {out['rps']} rps / "
                  f"P50 {out['p50_ms']}ms P95 {out['p95_ms']}ms P99 {out['p99_ms']}ms / "
                  f"错误 {out['error_rate']:.1%} 429 {out['limited_rate']:.1%}{note}")
    verdict = judge(verdict_stages, max_error_rate=args.max_error_rate)
    return verdict_stages, verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("用法")[0])
    ap.add_argument("--base-url", default=os.environ.get("LOAD_BASE_URL",
                                                         "http://127.0.0.1:8000"))
    ap.add_argument("--path", default="/api/stats", help="默认 /api/stats（只读无害）")
    ap.add_argument("--method", default="GET")
    ap.add_argument("--payload", default=None, help="JSON 字符串（POST/PUT 时用）")
    ap.add_argument("--token", default=None, help="缺省读 APP_TOKEN 环境变量")
    ap.add_argument("--stages", default="1,4,8", help="并发阶梯，逗号分隔")
    ap.add_argument("--duration", type=float, default=10, help="每阶段秒数")
    ap.add_argument("--timeout", type=float, default=30, help="单请求超时")
    ap.add_argument("--max-error-rate", type=float, default=0.05)
    args = ap.parse_args()
    args.stages = [int(x) for x in str(args.stages).split(",") if x.strip()]
    args.payload = json.loads(args.payload) if args.payload else None

    stages, verdict = asyncio.run(run(args))
    print("\n=== 判定 ===")
    if verdict["pass"]:
        print("PASS：各阶段错误率与 P95 均在阈值内")
        return 0
    for r in verdict["reasons"]:
        print("FAIL:", r)
    return 1


if __name__ == "__main__":
    sys.exit(main())
