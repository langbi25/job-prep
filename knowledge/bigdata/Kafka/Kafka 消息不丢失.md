---
id: kb-bigdata-kafka-002
domain: bigdata
topic: Kafka
title: Kafka 消息不丢失
mastery: 0
srs_due: ""
tags: []
related: []
source: manual
created: 2026-10-27
updated: 2026-10-27
---

# Kafka 消息不丢失

**Q：** Kafka 怎么保证消息不丢？

## 要点

- producer acks=all
- broker 副本+min.insync.replicas
- consumer 手动提交

## 易错

- （无）
