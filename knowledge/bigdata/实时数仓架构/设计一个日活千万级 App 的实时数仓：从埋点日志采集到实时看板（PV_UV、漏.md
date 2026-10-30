---
id: kb-bigdata-gen-001
domain: bigdata
topic: 实时数仓架构
title: 设计一个日活千万级 App 的实时数仓：从埋点日志采集到实时看板（PV/UV、漏
mastery: 0.0
srs_due: ""
tags: ["实时数仓", "Flink", "UV去重", "端到端一致性", "漏斗留存", "OLAP选型"]
related: []
source: mock
created: 2026-10-27
updated: 2026-10-30
---

# 设计一个日活千万级 App 的实时数仓：从埋点日志采集到实时看板（PV/UV、漏

**Q：** 设计一个日活千万级 App 的实时数仓：从埋点日志采集到实时看板（PV/UV、漏斗、留存），请说明整体链路、存储选型、UV 去重方案，以及如何保证端到端数据一致性和延迟指标。

## 要点

- 整体链路我按五段讲：客户端埋点 SDK 上报 → 接入层 Kafka（按业务/事件分 topic）→ Flink 实时清洗与聚合 → 存储层（明细+聚合分层）→ 看板查询服务，中间用维表关联补齐用户/设备属性。
- 采集端我会做批量+重试+本地缓存：SDK 攒批（如 10~50 条或 5s）上报，失败落本地队列重发，服务端用 Nginx/网关做限流与鉴权，日志统一成 JSON 落到 Kafka，保证削峰和解耦。
- 存储选型分三层：明细/去重状态用 ClickHouse 或 Doris 存明细与聚合结果，UV 去重中间态用 Redis（HyperLogLog/Bitmap）或 Flink 状态后端，看板查询走 OLAP 引擎的物化视图/预聚合表，避免直接扫原始日志。
- UV 去重我按精度分档：日活千万级用 Flink 状态存 userId 的 Bitmap/RoaringBitmap 按天分桶，或 Redis HyperLogLog（误差约 0.81%）做近似；要求精确时用 Bitmap 或精确去重表，按维度（渠道/版本）预聚合。
- 漏斗和留存依赖明细：漏斗用 Flink 按 userId 分组、按事件时间排序做窗口内步骤匹配（如 5 步漏斗用 CEP 或状态机），留存用「首日活跃集合 ∩ 第 N 日活跃集合」，集合用 Bitmap 存，按天滚动计算。
- 端到端一致性我分两段保证：Kafka→Flink 用 checkpoint + 两阶段提交（或幂等写）保证 exactly-once；Flink→OLAP 用主键 upsert/幂等写 + 版本号去重，配合 Flink 的 checkpoint 做故障恢复，避免重复计数。
- 延迟指标我按链路埋点：Kafka 消费 lag、Flink 算子处理延迟（用 metrics 的 latency/watermark 滞后）、写入 OLAP 耗时、看板查询 P99，端到端延迟目标通常定在秒级到分钟级（如 1~5 分钟），用监控告警兜底。
- 工程上我会做分层与资源隔离：ODS（原始）→ DWD（清洗明细）→ DWS（轻度聚合）→ ADS（看板结果），实时与离线共用一套口径，Flink 任务按业务分队列、设并行度与反压监控，大促前做压测和扩容预案。
- 权衡与边界：UV 精确去重内存随 DAU 线性增长，千万级要分桶+TTL；漏斗窗口越长状态越大，用 RocksDB 状态后端+增量 checkpoint；延迟与一致性冲突时，优先保证不重不丢，允许看板分钟级延迟。

## 易错

- 只讲组件堆叠不讲数据流和口径，面试官会追问漏斗/留存的具体计算逻辑。
- UV 去重直接说用 count distinct，没提 Bitmap/HLL 的内存与精度权衡。
- 一致性只提 Flink checkpoint，漏掉写入 OLAP 的幂等/两阶段提交。
- 延迟指标没有量化目标和监控手段，答成「尽量低」。
