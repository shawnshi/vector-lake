---
id: "20260422_bctm"
title: "Concept: 区块链信任机器 (Blockchain as Trust Machine)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Infrastructure"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 1825
categories: ["System_Architecture"]
tags: ["Blockchain", "Trust", "Security", "Decentralization"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十三讲：区块链与数据安全：构建医疗信任体系的基石.md"]
---

# 区块链信任机器 (Blockchain as Trust Machine)

## 定义
指利用区块链技术的去中心化、不可篡改、共识机制等特性，在无需第三方中介（如政府、大企业）背书的情况下，建立多方协作信任的底层架构[^1]。

## 核心机制
- **分布式存证**: 数据在多个节点同步，单点失效不影响全局。
- **非对称加密**: 确保数据所有权与访问权的分离。
- **共识协议**: 解决“谁说了算”的问题。

## 医疗落地场景 [支持:: [[Concept_可信数据空间.md]]]
- **处方流转**: 解决电子处方在医院外的“防伪”与“防二次使用”问题[^1]。
- **器官捐献/移植**: 确保排队序列与匹配过程的绝对透明与公正。
- **药品追踪**: 从药厂到病人的全生命周期溯源，打击非法仿冒。

## 局限性
- **吞吐量瓶颈**: 强一致性通常伴随着低并发性能。
- **垃圾进，垃圾出 (GIGO)**: 区块链能保证“上链后不可篡改”，但不能保证“上链前数据真实” [关联:: [[Concept_Data_as_a_Product.md]]]。

## 战略评估
区块链在医疗行业不是数据库的替代品，而是针对**高价值、多方博弈、强监管**场景下的“信任手术刀”[^1]。

[^1]: [[Source_第二十三讲：区块链与数据安全：构建医疗信任体系的基石.md]]
