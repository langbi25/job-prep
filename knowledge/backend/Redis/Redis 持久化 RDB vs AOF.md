---
id: kb-backend-redis-001
domain: backend
topic: Redis
title: Redis 持久化 RDB vs AOF
mastery: 0
srs_due: ""
tags: ["高频"]
related: []
source: manual
created: 2026-10-25
updated: 2026-10-25
---

# Redis 持久化 RDB vs AOF

**Q：** Redis 的 RDB 和 AOF 各自原理与取舍？

## 要点

- RDB：快照，fork 子进程
- AOF：追加命令日志，everysec 折中

## 易错

- 别把全量/增量说反
