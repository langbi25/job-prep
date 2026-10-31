---
id: kb-bigdata-gen-003
domain: bigdata
topic: 概念辨析与审题
title: 面试审题与概念边界：Flink 反压 vs Spark Shuffle vs 数据倾斜
mastery: 0
srs_due: ""
tags: ["Agent建议"]
related: []
source: agent
created: 2026-10-31
updated: 2026-10-31
---

# 面试审题与概念边界：Flink 反压 vs Spark Shuffle vs 数据倾斜

**Q：** 面试审题与概念边界：Flink 反压 vs Spark Shuffle vs 数据倾斜

## 要点

- 反压（backpressure）：下游消费速率跟不上上游生产速率，压力沿算子链向上游传导；Flink 中表现为上游 subtask 的 outPoolUsage 高、下游 inPoolUsage 高，排查手段包括 Web UI 反压监控、TaskManager 线程栈采样（jstack）、checkpoint 对齐时间过长、buffer 超时等
- Spark Shuffle：宽依赖触发的跨节点数据重分布，涉及 map 端写 shuffle 文件、reduce 端拉取，关注点是 shuffle 读写量、spill、分区数；与 Flink 反压不是同一层面的问题
- 数据倾斜：某个 key 或分区数据量远大于其他，导致个别 subtask 处理量畸高、反压与长尾；解决手段包括加盐打散、两阶段聚合（local+global）、join 侧 broadcast/拆分热点 key、调整并行度与分区策略
- 三者关系：数据倾斜是反压的常见根因之一，反压是现象，shuffle 是 Spark 的机制概念；答题先复述题目关键词并确认问题边界，再按“现象—定位—根因—解决”结构作答
- 审题方法：听到问题先用自己的话复述一遍并确认（如“您问的是 Flink 反压排查，我从监控指标、线程栈、checkpoint 三个层面回答”），避免答成相邻但不同的主题

## 易错

- （无）
