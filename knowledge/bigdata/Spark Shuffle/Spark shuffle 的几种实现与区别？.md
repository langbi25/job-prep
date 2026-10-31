---
id: kb-bigdata-sparkshu-001
domain: bigdata
topic: Spark Shuffle
title: Spark shuffle 的几种实现与区别？
status: active
mastery: 0.0
srs_due: ""
tags: ["Spark", "Shuffle", "SortShuffle", "HashShuffle", "性能调优"]
related: []
source: agent
created: 2026-10-31
updated: 2026-10-31
---

# Spark shuffle 的几种实现与区别？

**Q：** Spark shuffle 的几种实现与区别？

## 要点

- Spark shuffle 是宽依赖（如 groupByKey、join）触发的跨节点数据重分布，核心是把 map 端按分区器（HashPartitioner/RangePartitioner）写出的数据，在 reduce 端拉取并聚合，中间涉及磁盘落盘、网络传输和序列化。
- ShuffleManager 是可插拔的，Spark 1.2 前默认 HashShuffleManager，1.2 后默认 SortShuffleManager；1.6 起 HashShuffle 被标记为 deprecated，2.0 后基本只用 SortShuffle。
- HashShuffle 的机制是每个 map task 为每个 reduce task 生成一个文件，文件数 = map 数 × reduce 数，小文件多、句柄和缓冲区压力大；优化版通过 consolidate 机制让多个 map task 复用同一批文件，把文件数降到 core 数 × reduce 数。
- SortShuffle 的机制是每个 map task 只写一个数据文件加一个索引文件，数据先按 partitionId 排序（或使用 HashMap 聚合），reduce 端根据索引文件定位拉取，文件数 = 2 × map 数，大幅减少小文件。
- SortShuffle 有三种写模式：普通模式先排序再溢写；bypass 模式在 reduce 数 ≤ spark.shuffle.sort.bypassMergeThreshold（默认 200）且非聚合算子时，不排序直接按分区写多个小文件再合并，省去排序开销；tungsten-sort 用堆外内存和二进制排序，适合字段少的场景。
- 工程上要控制 shuffle 分区数：spark.sql.shuffle.partitions 默认 200，大作业常调到 core 数的 2~3 倍；分区太小会 OOM 或拉取慢，太大会产生大量小文件和调度开销。
- shuffle 数据落盘路径由 spark.local.dir 指定，要配多块盘并避免和日志盘混用；shuffle service（如 YARN 的 ExternalShuffleService）负责在 executor 释放后仍能提供数据，开启动态资源分配时必须开启。
- 调优手段包括：用 reduceByKey 替代 groupByKey 减少网络传输、map 端预聚合（combine）、开启 spark.shuffle.compress 压缩、适当增大 spark.shuffle.file.buffer 和 spark.reducer.maxSizeInFlight 提升 IO 与拉取效率。
- 权衡点：SortShuffle 通用但排序有 CPU 开销，bypass 模式省排序但不支持聚合且 reduce 数大时退化为普通模式；HashShuffle 写快但文件多，只适合 map/reduce 数都很小的场景。
- Sort Shuffle 在 map 端只做 partition 级排序，不是全局有序；要全局有序得另加 sortByKey 或 RangePartitioner。
- Tungsten Sort Shuffle 用 UnsafeRow 二进制格式 + 堆外内存，省掉序列化与对象开销，是 Sort Shuffle 的性能优化版。

## 易错

- 容易把 HashShuffle 的文件数说成 map 数 + reduce 数，实际是乘积，consolidate 后才按 core 数算。
- bypass 模式的条件记错：要求 reduce 数不超过阈值且不能有 map 端聚合，join 这类带聚合的算子不会走 bypass。
- 忽略 ExternalShuffleService，动态资源分配下 executor 被回收会导致 reduce 拉不到数据而重算。
- 分区数只背默认 200，说不出按 core 数 2~3 倍调整和 AQE 自动合并小分区的做法。
