---
id: kb-bigdata-sparkshu-004
domain: bigdata
topic: Spark Shuffle 实现
title: Spark shuffle 的几种实现与区别？
mastery: 0
srs_due: ""
tags: ["Spark", "Shuffle", "Sort Shuffle", "Hash Shuffle", "大数据"]
related: []
source: agent
created: 2026-10-31
updated: 2026-10-31
merged_into: kb-bigdata-sparkshu-001
status: archived
---

# Spark shuffle 的几种实现与区别？

**Q：** Spark shuffle 的几种实现与区别？

## 要点

- Spark shuffle 按实现分两类：Hash Shuffle（含优化版）和 Sort Shuffle；按输出形式分两类：Shuffle Write 与 Shuffle Read 的聚合方式。
- Hash Shuffle 每个 map task 为每个 reduce task 生成一个文件，文件数 = M×R，小文件多、随机写多，已被淘汰。
- Hash Shuffle 优化版（Consolidate）让同一 core 上多个 map task 共享输出文件，文件数降为 core 数×R，但仍有大量随机写。
- Sort Shuffle 每个 map task 只写一个数据文件加一个索引文件，文件数 = 2×M，reduce 按索引读取，大幅减少小文件。
- Sort Shuffle 内部有两种模式：普通模式先按 partitionId 排序再写；bypass 模式不排序、直接按 partition 写缓冲，适合 reduce 数少且无需 map 端聚合的场景。
- bypass 触发条件：reduce 数小于 spark.shuffle.sort.bypassMergeThreshold（默认 200）且非 map 端聚合算子，此时省去排序开销。
- Shuffle Write 阶段按 partitionId 做哈希分区，数据先写内存缓冲（默认 32KB），溢出时排序后落盘，最终 merge 成一个大文件。
- Shuffle Read 阶段 reduce task 通过 BlockManager 拉取自己分区的数据，可配置 spark.reducer.maxSizeInFlight 控制单次拉取量，避免 OOM。
- 工程上优先用 Sort Shuffle；调参关注 spark.shuffle.file.buffer、spark.reducer.maxSizeInFlight、spark.shuffle.memoryFraction，并开启 spark.shuffle.compress 压缩。

## 易错

- 别把 Hash Shuffle 和 Sort Shuffle 的文件数公式记混，M×R 是未优化 Hash 的。
- bypass 模式不是所有场景都更快，有 map 端聚合时仍会退化为普通 Sort。
- 面试官常追问 shuffle 与数据倾斜的关系，要能说出分区键哈希不均导致单 reduce 热点。
