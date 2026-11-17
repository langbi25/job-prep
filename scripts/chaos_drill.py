"""混沌演练：容器化之后才敢做的事——主动制造故障，验证恢复机制是实弹不是纸面。

用法（宿主机跑，docker CLI 要在 PATH 里）：
    python scripts/chaos_drill.py kill-api        # kill api 容器，量 RTO
    python scripts/chaos_drill.py sever-litellm   # 断 LLM 网关网络，验熔断三态实弹
    python scripts/chaos_drill.py cpu-limit       # 给 api 加 1 CPU 限额，复跑压测看拐点

三个演练各自验证一个"以前只能嘴上说"的东西：
1. kill-api     → restart: unless-stopped 是不是真把它拉起来（RTO 有数字）；
2. sever-litellm → 熔断器对"上游不可达"真的打开、真的快速失败、真的自愈闭合；
3. cpu-limit    → 容量拐点在资源受限下怎么移动（压测基线的对照数据）。

设计决定：
- **演练要走 HTTP，不能 docker exec python**。熔断注册表在 uvicorn 进程内存里，
  exec 起的新进程看到的是另一个空注册表——只有走 API 才能验证"在线进程"的真实状态。
- **失败注入全部可逆**：kill 有 restart 策略兜底、断网必重连、限额必还原。演练脚本
  自己的 bug 不允许把生产环境留在损坏状态——finally 里做还原，比 try 里更靠得住。
- **断网演练零 LLM 成本**：litellm 不可达时请求在网络层就死了，到不了上游，
  不产生 token 计费；恢复后的探针是唯一的真实调用（几厘钱）。
- 判定标准写死在脚本里：PASS/FAIL 看退出码，CI / 手动都可用。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "job-prep-api-1"
LITELLM = "job-prep-litellm-1"


def load_token() -> str:
    tok = os.environ.get("APP_TOKEN", "")
    if tok:
        return tok
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def docker(*args: str, check: bool = True) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} 失败: {r.stderr.strip()[:500]}")
    return r.stdout.strip()


def client(token: str) -> httpx.Client:
    return httpx.Client(base_url="http://127.0.0.1:8765", timeout=35,
                        headers={"X-API-Token": token} if token else {})


def circuit_state(c: httpx.Client) -> dict:
    """在线进程的熔断状态（走 /api/obs/metrics 的 guards 段，不走 exec）。"""
    r = c.get("/api/obs/metrics", params={"days": 1})
    r.raise_for_status()
    return r.json().get("guards", {}).get("circuit", {})


def open_breaker(state: dict) -> dict | None:
    for b in state.get("breakers", []):
        if b.get("state") == "open":
            return b
    return None


def one_chat(c: httpx.Client, msg: str = "混沌演练探针，请只回复 ok") -> dict:
    """一次最小对话。返回 {ok, status, ms, error}。

    判定口径实测修正（2026-11-17）：console_service 把单段异常包进 200 响应体的
    error 字段（"一段挂掉不该丢掉其他段"），所以 status==200 不代表成功——
    必须看 body.error 是否为空。
    """
    t0 = time.perf_counter()
    try:
        r = c.post("/api/console/chat", json={"message": msg, "mode": "assist"})
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        err = str(body.get("error") or "")
        if r.status_code != 200 and not err:
            err = r.text[:150]
        return {"ok": r.status_code == 200 and not err, "status": r.status_code, "ms": ms,
                "error": err[:150]}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "ms": int((time.perf_counter() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:150]}


# ---------------------------------------------------------------------------
# 演练 1：kill api
# ---------------------------------------------------------------------------

def drill_kill_api() -> bool:
    print("=== 演练 1：api 进程崩溃，验证 restart 策略与 RTO ===")
    print("崩溃机制 = 应用内置 SIGUSR1 钩子（app/main.py）。三个实测结论支撑这个设计：")
    print("  1) 命名空间内 kill PID1 的 SIGKILL 被内核忽略——进程根本不会死；")
    print("  2) `docker kill` 被视为人为停止，restart 策略不触发（restarts=0 实锤）；")
    print("  3) 非 CLI 发起的自愿退出（trap 实测）会正常触发 restart 策略。")
    with client(load_token()) as c:
        pre = one_chat(c, "演练前基线探针")
        if not pre["ok"]:
            print(f"FAIL: 演练前服务就不健康（{pre}），先修服务再演练")
            return False
    print(f"基线探针 ok（{pre['ms']}ms）")

    started1 = docker("inspect", API, "--format", "{{.State.StartedAt}}")
    t0 = time.perf_counter()
    docker("exec", API, "python", "-c",
           "import os, signal; os.kill(1, signal.SIGUSR1)")
    print(f"已发送 SIGUSR1（{API} 硬崩溃，restart: unless-stopped 应自动拉起）")

    deadline = time.monotonic() + 180
    healthy = False
    while time.monotonic() < deadline:
        try:
            r = httpx.get("http://127.0.0.1:8765/health", timeout=3)
            if r.status_code == 200:
                healthy = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    if not healthy:
        print("FAIL: 180s 内 /health 没恢复")
        return False
    rto = time.perf_counter() - t0
    print(f"/health 恢复：RTO = {rto:.1f}s")

    # 防假阳性：/health 秒回不代表容器真重启过（钩子没生效时旧进程还活着）。
    started2 = docker("inspect", API, "--format", "{{.State.StartedAt}}")
    if started2 == started1:
        print(f"FAIL: StartedAt 未变化（{started1}），容器没有真正重启——RTO 是假数据")
        return False
    print(f"容器确认重启过（StartedAt: {started1} -> {started2}）")

    with client(load_token()) as c:
        post = one_chat(c, "演练后验证探针")
    ok = post["ok"] and rto < 120
    print(f"业务探针：{'ok' if post['ok'] else 'FAIL'}（{post['ms']}ms）；"
          f"RTO {'<' if rto < 120 else '>='} 120s 判定线")
    print("PASS" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# 演练 2：停 litellm 网关 → 熔断三态实弹
# ---------------------------------------------------------------------------


def drill_sever_litellm() -> bool:
    print("=== 演练 2：停掉 litellm 网关，验证熔断打开/快速失败/自愈闭合 ===")
    print("故障注入用 `docker stop` 而不是 network disconnect：实测（2026-11-17）后者会")
    print("把容器的内嵌 DNS 注册搞丢（disconnect 后 IP 通但服务名 NXDOMAIN，重启容器")
    print("都救不回来，只能 --force-recreate）。stop/start 的语义也更真实：网关进程死亡。")
    with client(load_token()) as c:
        base = circuit_state(c)
        if open_breaker(base) is not None:
            # 上次演练残留的 open 状态：三态机只在"有请求进来"时才从 open 转
            # 半开，干等永远不会闭合——冷却结束就主动发探针。
            print("熔断尚在 open（上次演练残留），等冷却并主动探针…")
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and open_breaker(circuit_state(c)):
                ob = open_breaker(circuit_state(c))
                if (ob.get("open_remaining_s") or 0) > 0:
                    time.sleep(min(ob["open_remaining_s"], 65) + 5)
                else:
                    p = one_chat(c, "演练前恢复探针")
                    print(f"  恢复探针: ok={p['ok']} ms={p['ms']} err={p['error'][:50]}")
                    if not p["ok"]:
                        time.sleep(10)
            if open_breaker(circuit_state(c)):
                print("FAIL: 5 分钟后熔断仍未闭合，结果不可信")
                return False
        base = circuit_state(c)
        print(f"基线：closed，breakers={len(base.get('breakers', []))} 个")

        docker("stop", LITELLM)   # 人为停止不触发 restart 策略，恢复由本脚本负责
        stopped = True
        print(f"已停止 {LITELLM}。开始锤：6 次对话调用（到不了上游，零 token 成本）")
        try:
            hammers = [one_chat(c) for _ in range(6)]
            for i, h in enumerate(hammers, 1):
                print(f"  锤#{i}: ok={h['ok']} status={h['status']} ms={h['ms']} "
                      f"err={h['error'][:60]}")
            if all(h["ok"] for h in hammers):
                print("FAIL: 锤子全部成功——故障注入没生效？")
                return False

            opened = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                opened = open_breaker(circuit_state(c))
                if opened:
                    break
                time.sleep(2)
            if not opened:
                print("FAIL: 60s 内熔断没有打开——threshold/统计口径有问题")
                return False
            print(f"熔断已打开: key={opened['key']} opens={opened['opens']} "
                  f"rejects={opened['rejects']} err={opened['last_error'][:60]}")

            quick = one_chat(c, "熔断打开期间应该被快速拒绝")
            print(f"快速失败验证: ok={quick['ok']} ms={quick['ms']} err={quick['error'][:80]}")
            quick_fail_ok = (not quick["ok"]) and quick["ms"] < 10000
            print(f"  {'PASS' if quick_fail_ok else 'FAIL'}: 打开期间调用 "
                  f"{'快速失败' if quick_fail_ok else '不符合快速失败预期'}（阈值 10s）")
        finally:
            # 还原永远执行：脚本自己挂了也不能把网关留在死亡状态
            if stopped:
                docker("start", LITELLM)
                print(f"已重新启动 {LITELLM}（finally 保护）")

        # 等网关就绪：litellm 启动要十几秒，宿主 127.0.0.1:4000 可探
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if httpx.get("http://127.0.0.1:4000/health/liveliness",
                             timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        print("litellm 已就绪，进入恢复循环（探针失败会退避加倍，跟着 open_remaining_s 走）")

        # 恢复循环：跟着熔断器的退避节奏探，最多 5 分钟
        deadline = time.monotonic() + 300
        probe_ok = False
        probe = None
        while time.monotonic() < deadline:
            ob = open_breaker(circuit_state(c))
            if ob and (ob.get("open_remaining_s") or 0) > 0:
                time.sleep(min(ob["open_remaining_s"], 65) + 5)
                continue
            probe = one_chat(c, "半开探针：真实调用，恢复后第一个请求")
            print(f"探针: ok={probe['ok']} ms={probe['ms']} err={probe['error'][:60]}")
            if probe["ok"]:
                probe_ok = True
                break
        if not probe_ok:
            print("FAIL: 5 分钟内探针没有成功")
            return False

        after = circuit_state(c)
        still_open = open_breaker(after) is not None
        print(f"熔断闭合: {'否（仍 open）' if still_open else '是'}；"
              f"现状: {[(b['key'][:40], b['state']) for b in after.get('breakers', [])]}")
        print("PASS" if (not still_open and quick_fail_ok) else "FAIL")
        return (not still_open) and quick_fail_ok


# ---------------------------------------------------------------------------
# 演练 3：CPU 限额 → 容量拐点移动
# ---------------------------------------------------------------------------

def drill_cpu_limit(cpus: float, stages: str, duration: float) -> bool:
    print(f"=== 演练 3：api 限 {cpus} CPU，复跑压测（对照 2026-11-16 基线："
          f"并发4 177rps/P95 33ms，并发8 112rps/P95 195ms）===")
    orig = docker("inspect", API, "--format", "{{.HostConfig.NanoCpus}}")
    print(f"原始 NanoCpus = {orig}（0 = 不限）")
    try:
        docker("update", f"--cpus={cpus}", API)
        print(f"已限 {cpus} CPU")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "load_test.py"),
                            "--stages", stages, "--duration", str(duration),
                            # 不带 token 的压测全打在 401 上（毫秒级失败），数据无效——
                            # 实测踩过：错误率 100% 但 rps 翻倍，其实是鉴权挡住了。
                            "--token", load_token()],
                           timeout=duration * (len(stages.split(",")) + 2) * 4)
        load_ok = r.returncode == 0
        print(f"压测脚本退出码: {r.returncode}（0=无拐点异常，1=P95 2 倍劣化或错误率超线）")
        print("说明：限额下 FAIL 不是环境坏了，恰恰是容量拐点左移的证据——"
              "记录数字，与不限额基线对照就是结论。")
        return load_ok
    finally:
        # 还原永远执行：演练脚本不允许把服务留在受限状态。
        # 实测：--cpus=0 不是合法撤销值（报 range 0.01-12），撤销限额要用
        # --cpu-quota=-1；原值非零则按 NanoCpus→CPU 数换算还原。
        if float(orig) > 0:
            docker("update", f"--cpus={float(orig) / 1e9:g}", API)
            print(f"已还原 CPU 限额 = {float(orig) / 1e9:g}")
        else:
            docker("update", "--cpu-quota=-1", API)
            print("已还原 CPU 限额 = 不限（--cpu-quota=-1）")


def main() -> int:
    ap = argparse.ArgumentParser(description="混沌演练（容器化后才做得到）")
    ap.add_argument("drill", choices=["kill-api", "sever-litellm", "cpu-limit", "all"])
    ap.add_argument("--cpus", type=float, default=1.0, help="cpu-limit 演练的限额值")
    ap.add_argument("--stages", default="1,4,8")
    ap.add_argument("--duration", type=float, default=8.0)
    args = ap.parse_args()

    drills = {"kill-api": drill_kill_api, "sever-litellm": drill_sever_litellm}.get(
        args.drill)
    if drills:
        return 0 if drills() else 1
    if args.drill == "cpu-limit":
        return 0 if drill_cpu_limit(args.cpus, args.stages, args.duration) else 1
    # all：顺序跑三个，任何一个失败都不阻断后面的（还原逻辑各自独立）
    results = {"kill-api": drill_kill_api(), "sever-litellm": drill_sever_litellm(),
               "cpu-limit": drill_cpu_limit(args.cpus, args.stages, args.duration)}
    print("\n=== 汇总 ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
