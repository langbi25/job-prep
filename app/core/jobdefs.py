"""默认任务注册表：api 与 worker 两个进程都要 import。

任务注册表（jobs._REGISTRY）活在进程内存里，所以"哪些任务存在"必须每个
进程各自注册一遍：

- **worker 进程**：调度循环按注册表决定跑什么——不注册就永远不跑；
- **api 进程**：`GET /api/obs/jobs` 的 snapshot 和手动触发的 run_job 都查
  注册表——不注册的话面板显示为空、按钮按下去 KeyError。

注册是幂等的，且**逐个任务判重**：每个 job 各自检查自己是否已注册，而不是
"发现有一个就整体 return"。后者在新增任务时是个静默 bug——第二次调用看到
prune_spans 已存在就直接返回，新加的采样任务永远注册不上（api 面板里没有、
手动触发按钮 KeyError，且没有任何报错）。
"""
from __future__ import annotations

import os


def register_default_jobs() -> None:
    from . import jobs as _jobs

    if "prune_spans" not in _jobs.registry():
        from ..observability import tracing as _tr

        # span 分级清理：每 6h 一轮。以前只在 api 启动时清一次，长驻进程几天
        # 不清、span 表悄悄膨胀；进 job runner 后靠"到期判断 + 执行历史"保证
        # 既不欠账也看得见每次清了多少。
        @_jobs.job("prune_spans", interval_s=6 * 3600, timeout_s=300)
        def _prune_spans_task() -> int:
            return _tr.prune_spans()

    if "capacity_sample" not in _jobs.registry():
        from ..services import capacity_service as _cap

        interval = int(os.environ.get("CAPACITY_SAMPLE_S", "") or _cap.SAMPLE_INTERVAL_S)

        # 容量快照：写锁探测 + 库体积 + 写入量代理。放 worker 是因为它是
        # 定时进程；放 api 会让请求面背一个定时器，且重启就断档。
        @_jobs.job("capacity_sample", interval_s=interval, timeout_s=60)
        def _capacity_sample_task() -> int:
            return int(_cap.sample()["db_bytes"])
