---
id: "20260422_e001esb"
title: "Entity_ESB (企业服务总线)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Integration"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["System_Architecture"]
tags: ["ESB", "消息队列", "中间件", "高速公路"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md"]
---

# Entity: 企业服务总线 (ESB)

## 定义
企业服务总线（Enterprise Service Bus）是 **[[Entity_HIP.md]]** 的核心技术底座。它在各个系统之间建立了一条“数据高速公路”，负责消息的路由、转换、安全性审计和协议适配[^1]。

## 核心能力
- **松耦合**: 系统间不再需要点对点直接连接，降低了系统变更的爆炸半径。
- **协议转换**: 将不同系统使用的异构协议进行同态映射。
- **负载均衡与重试机制**: 确保医疗核心数据流的最终一致性。

## 在医疗中的应用
ESB 终结了医疗 IT 的 **[[Concept_Spaghetti_Architecture.md]]**，使得“拔插式”的系统集成成为可能[^1]。

## 关联节点
- [属于:: [[Entity_HIP.md]]]
- [对比:: [[Concept_Spaghetti_Architecture.md]]]

[^1]: [[Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md]]
