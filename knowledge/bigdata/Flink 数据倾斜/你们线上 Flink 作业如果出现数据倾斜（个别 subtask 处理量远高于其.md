---
id: kb-bigdata-flink-002
domain: bigdata
topic: Flink 数据倾斜
title: 你们线上 Flink 作业如果出现数据倾斜（个别 subtask 处理量远高于其
mastery: 0.0
srs_due: ""
tags: ["Flink", "数据倾斜", "keyBy", "两阶段聚合", "join优化", "热点key"]
related: []
source: mock
created: 2026-10-27
updated: 2026-10-30
---

# 你们线上 Flink 作业如果出现数据倾斜（个别 subtask 处理量远高于其

**Q：** 你们线上 Flink 作业如果出现数据倾斜（个别 subtask 处理量远高于其他），你会怎么定位和解决？请结合 keyBy 后的聚合、join 等场景说明具体手段及其代价。

## 要点

- 先定位：在 Flink Web UI 看各 subtask 的 recordsIn/out、busy 率与 backpressure，倾斜时个别 subtask 的 records 是其他的数倍且 busy 接近 100%，其余空闲。
- 再看指标：通过 Metrics 的 numRecordsInPerSecond、idleTimeMsPerSecond 对比 subtask 间差异，结合 checkpoint 对齐时间变长、反压向上游蔓延判断是倾斜而非纯反压。
- 确认 key 分布：在 keyBy 前加临时算子采样，把 key 与计数打到日志或侧输出，找出热点 key（如 null、默认值、某个大卖家 ID），这是定位根因最直接的一步。
- 聚合场景手段一：热点 key 加随机前缀做两阶段聚合，先 localKey=key+salt 预聚合，再去掉 salt 做全局聚合，代价是多一次 shuffle 和状态翻倍。
- 聚合场景手段二：对少量热点 key 用 local/global 分离，热点 key 走单独逻辑或广播到下游合并，避免全量数据重分布，代价是代码复杂度上升。
- join 场景：大表 join 大表倾斜时，把热点 key 单独抽出来做广播或加盐打散后 join 再 union；小表 join 大表直接 BroadcastHashJoin 广播小表，避免 shuffle。
- 参数与资源层：调大 sink/上游并行度、开启 Object Reuse 减少拷贝、给倾斜 subtask 更多 slot 或单独设置并行度，但并行度受 key 基数限制，治标不治本。
- 根治方向：从数据源治理热点 key（如 null 转随机、拆分大 key），或改用两阶段聚合 + 本地预聚合（MiniBatch/预聚合算子）降低单 key 状态与计算压力。
- 权衡：加盐两阶段聚合增加一次网络 shuffle 和状态存储，延迟上升；广播方案受小表大小限制（默认 100MB 左右），需评估内存与 GC。

## 易错

- 只调并行度不解决 key 基数少的问题，热点 key 仍落同一 subtask。
- 加盐后忘记二次聚合或 salt 范围选太大，导致 shuffle 数据量反而膨胀。
- 把倾斜和反压混为一谈，未先看 subtask 级 records 分布就下结论。
- 广播 join 未评估小表大小，超过 broadcast 阈值会退化为 shuffle join。
