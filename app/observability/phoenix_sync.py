"""评测结果双写 Phoenix（P2）：本地是执行与门禁的唯一事实源，Phoenix 是分析层。

职责边界（为什么不走 phoenix-client 的 run_experiment）：
- run_experiment 会让 Phoenix 的 task runner 重新执行业务逻辑——那是评测的第二条
  执行路径，和 REST/Agent/MCP 三条读路径一样迟早分叉（本项目坑 6 的教训）；
- 所以这里只做「搬运」：本地评测照常跑完，把数据集和逐用例结果作为
  dataset + experiment + runs + evaluations 推上去，供 UI 对照与后续归因（P3）。

实测踩坑（Phoenix 21.0.0）：
- POST /v1/datasets/upload?sync=true 返回 200 后，立即 GET /v1/datasets/{id}
  可能 404（异步写入的可见性窗口，毫秒到秒级不定）。官方 client 3.7.0 的
  create_dataset 内部就在这个 404 上翻车。因此所有 read-after-write 都带重试；
- 不存在的路由返回 SPA HTML（200 + text/html），不能拿 200 当命中，必须验 content-type；
- experiment 的 run 对 (experiment, example, repetition) 幂等冲突返回 409——
  重推同一份报告时捕获 409 跳过即可（成功 run 不可变）。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import time

import httpx

BASE = os.getenv("PHOENIX_BASE", "http://127.0.0.1:6006").rstrip("/")

# read-after-write 重试：可见性窗口实测毫秒~秒级，10s 上限足够
_RETRY_TRIES = 40
_RETRY_DELAY = 0.25


def _client() -> httpx.Client:
    # trust_env=False：别让本机代理（7890 等）劫持对 localhost 的请求
    return httpx.Client(base_url=BASE, trust_env=False, timeout=20)


def available(timeout: float = 3.0) -> bool:
    """Phoenix 是否可达。用 /v1/datasets（JSON 路由）而不是 /（SPA 也返回 200）。"""
    try:
        with _client() as c:
            r = c.get("/v1/datasets", timeout=timeout)
            return r.status_code == 200 and "application/json" in r.headers.get("content-type", "")
    except Exception:  # noqa: BLE001
        return False


def _get_json_with_retry(c: httpx.Client, path: str) -> dict:
    """GET JSON，404/HTML 都重试（写入可见性窗口）。"""
    last = None
    for _ in range(_RETRY_TRIES):
        r = c.get(path, headers={"accept": "application/json"})
        if r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
            return r.json()
        last = f"{r.status_code} {r.text[:80]}"
        time.sleep(_RETRY_DELAY)
    raise RuntimeError(f"GET {path} 重试 {_RETRY_TRIES} 次仍失败: {last}")


def _upload(c: httpx.Client, payload: dict) -> dict:
    r = c.post("/v1/datasets/upload", params={"sync": "true"},
               json=payload, headers={"accept": "application/json"})
    r.raise_for_status()
    return r.json()["data"]


def list_datasets() -> list[dict]:
    with _client() as c:
        r = c.get("/v1/datasets", headers={"accept": "application/json"})
        r.raise_for_status()
        return r.json()["data"]


def upsert_dataset(name: str, description: str,
                   inputs: list[dict], outputs: list[dict],
                   metadata: list[dict] | None = None) -> dict:
    """按名字 upsert 数据集。内容未变（描述里带内容 hash）则跳过上传。

    返回 {id, examples}，examples 按 input 内容建索引，供逐用例 join。
    """
    h = hashlib.sha1(json.dumps([inputs, outputs], ensure_ascii=False,
                                sort_keys=True).encode()).hexdigest()[:10]
    tag = f"[hash:{h}]"
    for d in list_datasets():
        if d["name"] == name and tag in (d.get("description") or ""):
            examples = _get_json_with_retry(
                _client(), f"/v1/datasets/{d['id']}/examples")["data"]["examples"]
            return {"id": d["id"], "examples": examples, "skipped": True}
    payload = {"action": "update", "name": name,
               "description": (description + " " + tag).strip(),
               "inputs": inputs, "outputs": outputs,
               "metadata": metadata or [{} for _ in inputs]}
    with _client() as c:
        info = _upload(c, payload)
        data = _get_json_with_retry(c, f"/v1/datasets/{info['dataset_id']}/examples")["data"]
    return {"id": info["dataset_id"], "examples": data["examples"], "skipped": False}


def create_experiment(dataset_id: str, name: str, description: str = "",
                      metadata: dict | None = None) -> str:
    """创建 experiment；服务端不查重，同名先查再建（重推同一报告保持幂等）。"""
    with _client() as c:
        r = c.get(f"/v1/datasets/{dataset_id}/experiments",
                  params={"limit": 100}, headers={"accept": "application/json"})
        if r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
            for e in r.json().get("data", []):
                if e.get("name") == name:
                    return e["id"]
        r = c.post(f"/v1/datasets/{dataset_id}/experiments",
                   json={"name": name, "description": description,
                         "repetitions": 1, "metadata": metadata or {}})
        r.raise_for_status()
        return r.json()["data"]["id"]


def log_run(experiment_id: str, example_id: str, output, start: datetime.datetime,
            end: datetime.datetime, error: str | None = None) -> str:
    payload = {"dataset_example_id": example_id, "output": output,
               "repetition_number": 1,
               "start_time": start.isoformat(), "end_time": end.isoformat()}
    if error:
        payload["error"] = error
    with _client() as c:
        r = c.post(f"/v1/experiments/{experiment_id}/runs", json=payload)
        if r.status_code == 409:  # 成功 run 不可变：重推同一份报告时跳过
            return ""
        r.raise_for_status()
        return r.json()["data"]["id"]


def log_evaluation(run_id: str, name: str, score: float | None = None,
                   label: str | None = None, explanation: str | None = None) -> None:
    result = {}
    if score is not None:
        result["score"] = score
    if label is not None:
        result["label"] = label
    if explanation:
        result["explanation"] = explanation[:500]
    payload = {"experiment_run_id": run_id, "name": name, "annotator_kind": "CODE",
               "result": result,
               "start_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "end_time": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with _client() as c:
        r = c.post("/v1/experiment_evaluations", json=payload)
        r.raise_for_status()


def experiment_url(experiment_id: str) -> str:
    return f"{BASE}/experiments/{_b64('Experiment', experiment_id)}"


def _b64(kind: str, num_or_id: str) -> str:
    """server 返回的 experiment id 可能是数字串，统一转 global id。"""
    if str(num_or_id).isdigit():
        return base64.b64encode(f"{kind}:{num_or_id}".encode()).decode()
    return str(num_or_id)


# ---------------------------------------------------------------- 双写主流程
def sync_eval_report(report: dict, evals_dir) -> dict:
    """把 run_evals 的报告双写到 Phoenix。返回 {datasets, experiments} 概要。

    供逐用例 join 的键是 input 内容本身，不依赖服务器返回顺序。"""
    from pathlib import Path

    edir = Path(evals_dir)

    def load(p: str) -> list[dict]:
        f = edir / p
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]

    out: dict = {"datasets": {}, "experiments": {}}
    stamp = report.get("timestamp", datetime.datetime.now().isoformat(timespec="seconds"))

    def push(kind: str, ds_name: str, cases: list[dict],
             inputs_of, outputs_of, meta_of,
             runs: list[dict]) -> None:
        """runs: [{example_idx, output, scores: {name: (score, label, expl)}, error}]"""
        ds = upsert_dataset(ds_name, f"job-prep {kind} golden set",
                            inputs_of(cases), outputs_of(cases), meta_of(cases))
        out["datasets"][ds_name] = {"id": ds["id"], "skipped": ds["skipped"],
                                    "cases": len(cases)}
        # input 内容 -> example_id（不依赖服务器返回顺序）
        by_input = {json.dumps(e["input"], ensure_ascii=False, sort_keys=True): e["id"]
                    for e in ds["examples"]}
        exp_id = create_experiment(
            ds["id"], f"{kind} {stamp}",
            description="由 run_evals 本地结果双写，非平台重跑",
            metadata={"source": "run_evals", "llm_calls": report.get("llm_calls")})
        ok_runs = 0
        for run in runs:
            key = json.dumps(inputs_of(cases)[run["example_idx"]],
                              ensure_ascii=False, sort_keys=True)
            eid = by_input.get(key)
            if not eid:
                continue
            rid = log_run(exp_id, eid, run.get("output"),
                          start=datetime.datetime.now(datetime.timezone.utc),
                          end=datetime.datetime.now(datetime.timezone.utc),
                          error=run.get("error"))
            if not rid:
                continue
            ok_runs += 1
            for name, (score, label, expl) in (run.get("scores") or {}).items():
                log_evaluation(rid, name, score=score, label=label, explanation=expl)
        out["experiments"][f"{kind} {stamp}"] = {"id": exp_id, "runs": ok_runs}

    def ts() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    # ---- 检索三档：details 与 cases 同序 ----
    ret_cases = load("retrieval_cases.jsonl")
    ret_report = report.get("retrieval") or {}
    for mode, r in ret_report.items():
        if r.get("skipped") or not ret_cases:
            continue
        runs = []
        for i, (c, d) in enumerate(zip(ret_cases, r.get("details", []))):
            rr = 1.0 / (d["top3"].index(c["expected_card_id"]) + 1) \
                if d.get("ok") and c["expected_card_id"] in d.get("top3", []) else 0.0
            runs.append({"example_idx": i, "output": {"top3": d.get("top3", [])},
                         "scores": {"hit5": (1.0 if d.get("ok") else 0.0, None, None),
                                    "reciprocal_rank": (rr, None, None),
                                    "kind": (None, c.get("kind"), None)}})
        push(f"retrieval/{mode}", "retrieval-golden", ret_cases,
             lambda cs: [{"query": c["query"]} for c in cs],
             lambda cs: [{"expected_card_id": c["expected_card_id"]} for c in cs],
             lambda cs: [{"kind": c.get("kind")} for c in cs],
             runs)

    # ---- search_kb 工具：covered + gap 两段 details ----
    st = report.get("search_tool")
    st_cases = load("retrieval_cases.jsonl")
    gap_cases = load("tool_gap_cases.jsonl")
    if st and st_cases:
        runs = []
        for i, (c, d) in enumerate(zip(st_cases, st.get("details", []))):
            runs.append({"example_idx": i,
                         "output": {"top3": d.get("top3", []),
                                    "confidence": d.get("confidence")},
                         "scores": {"hit3": (1.0 if d.get("ok") else 0.0, None, None),
                                    "top1": (1.0 if d.get("top1") else 0.0, None, None)}})
        n_cov = len(st_cases)
        for j, (c, d) in enumerate(zip(gap_cases, st.get("gap_details", []))):
            runs.append({"example_idx": n_cov + j, "output": {"confidence": d.get("confidence")},
                         "scores": {"not_false_confident":
                                    (0.0 if d.get("false_confident") else 1.0, None,
                                     f"sim={d.get('sim')}")}})
        all_cases = st_cases + gap_cases
        ds = upsert_dataset("search-tool-golden", "job-prep search_kb golden set",
                            [{"query": c["query"]} for c in all_cases],
                            [{"expected_card_id": c.get("expected_card_id", "")} for c in all_cases],
                            [{"kind": c.get("kind", "gap")} for c in all_cases])
        out["datasets"]["search-tool-golden"] = {"id": ds["id"], "skipped": ds["skipped"],
                                                 "cases": len(all_cases)}
        by_input = {json.dumps(e["input"], ensure_ascii=False, sort_keys=True): e["id"]
                    for e in ds["examples"]}
        exp_id = create_experiment(ds["id"], f"search-tool {stamp}",
                                   description="run_evals 双写")
        ok = 0
        for run in runs:
            eid = by_input.get(json.dumps(
                {"query": all_cases[run["example_idx"]]["query"]},
                ensure_ascii=False, sort_keys=True))
            if not eid:
                continue
            rid = log_run(exp_id, eid, run["output"], start=ts(), end=ts())
            if not rid:
                continue
            ok += 1
            for name, (score, label, expl) in run["scores"].items():
                log_evaluation(rid, name, score=score, label=label, explanation=expl)
        out["experiments"][f"search-tool {stamp}"] = {"id": exp_id, "runs": ok}

    # ---- 评分校准 ----
    sc = report.get("scoring")
    sc_cases = load("golden_answers.jsonl")
    if sc and sc_cases:
        runs = []
        for i, (c, d) in enumerate(zip(sc_cases, sc.get("details", []))):
            err = d.get("error")
            runs.append({"example_idx": i, "output": {"band": d.get("got"), "avg": d.get("avg")},
                         "error": err,
                         "scores": {"band_match": (1.0 if d.get("ok") else 0.0, None, None),
                                    "avg_score": (d.get("avg"), None, None)}})
        push("scoring", "scoring-golden", sc_cases,
             lambda cs: [{"question": c["question"], "topic": c.get("topic"),
                          "qtype": c.get("qtype"), "answer": c["answer"]} for c in cs],
             lambda cs: [{"expected_band": c["expected_band"]} for c in cs],
             lambda cs: [{} for _ in cs], runs)

    # ---- 提取判定 ----
    ex = report.get("extract_judge")
    ex_cases = load("extract_judge_cases.jsonl")
    if ex and ex_cases:
        runs = []
        for i, (c, d) in enumerate(zip(ex_cases, ex.get("details", []))):
            runs.append({"example_idx": i, "output": {"doc": d.get("doc"), "pred": d.get("pred")},
                         "error": d.get("error"),
                         "scores": {"doc_type_ok": (1.0 if d.get("doc_ok") else 0.0, None, None),
                                    "completeness_ok":
                                    (1.0 if not d.get("mismatch") else 0.0, None,
                                     json.dumps(d.get("mismatch", []), ensure_ascii=False))}})
        push("extract-judge", "extract-judge-golden", ex_cases,
             lambda cs: [{"raw": c["raw"]} for c in cs],
             lambda cs: [{"expect_doc_type": c["expect_doc_type"],
                          "expect_completeness": c["expect_completeness"]} for c in cs],
             lambda cs: [{} for _ in cs], runs)

    # ---- Agent 任务级 ----
    at = report.get("agent_tasks")
    at_cases = load("agent_tasks.jsonl")
    if at and at_cases:
        runs = []
        for i, (c, d) in enumerate(zip(at_cases, at.get("details", []))):
            runs.append({"example_idx": i, "output": {"tools": d.get("tools"),
                                                      "items": d.get("items"),
                                                      "minutes": d.get("minutes")},
                         "error": d.get("reason"),
                         "scores": {"task_ok": (1.0 if d.get("ok") else 0.0,
                                                "ok" if d.get("ok") else "fail",
                                                json.dumps(d.get("checks", {}),
                                                            ensure_ascii=False))}})
        push("agent-tasks", "agent-tasks-golden", at_cases,
             lambda cs: [{"task": c["task"], "direction": c["direction"]} for c in cs],
             lambda cs: [{"min_tools": c.get("min_tools", 2),
                          "max_minutes": c.get("max_minutes", 60)} for c in cs],
             lambda cs: [{} for _ in cs], runs)

    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path
    # 用法：python -m app.observability.phoenix_sync [results.json]
    #   不带参数 = 重推 evals/ 下最新一份报告（不重跑评测，零 LLM 调用）
    edir = Path(__file__).resolve().parent.parent.parent / "evals"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(
        edir.glob("results-*.json"), key=lambda p: p.stat().st_mtime)
    report = json.loads(path.read_text(encoding="utf-8"))
    if not available():
        print(f"Phoenix 不可达（{BASE}），先启动：PHOENIX_PORT=6006 python -m phoenix.server.main serve")
        sys.exit(1)
    info = sync_eval_report(report, edir)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    for name, e in info["experiments"].items():
        print(f"  {name}: {experiment_url(e['id'])}  ({e['runs']} runs)")
