---
id: "20260422_cmcloud"
title: "Concept_混合云医疗策略"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Infrastructure"
status: "Active"
alignment_score: 94
epistemic-status: "evergreen"
ttl: 1825
categories: ["System_Architecture"]
tags: ["混合云", "私有云", "公有云", "数据主权", "业务弹性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第十八讲：云计算与 5G：医疗数字化的“水、电、煤”.md"]
---

# 混合云医疗策略 (Hybrid Cloud Strategy for Healthcare)

## 定义
混合云医疗策略是指医疗机构在数字化转型中采取的一种务实的基础设施布局模式：核心临床数据与核心业务系统保留在院内（私有云/本地化部署），而非核心的交互性业务、低密级的互联网医院业务、大算力消耗的 AI 训练业务等则部署在公有云上[^1]。

## 核心逻辑
- **安全性 (私有云)**: 响应 [[Concept_Sovereign_AI.md]] 的要求，保卫核心临床数据的解释权与物理控制权。
- **弹性 (公有云)**: 利用公有云的弹性扩展能力处理突发的互联网问诊流量，或按需租用昂贵的 GPU 算力。
- **无感化切换**: 通过云管平台实现不同环境下的资源统一调度与管理。

## 适用场景
- **双轨制运行**: HIS 系统在院内运行，挂号/支付/报告查询在公有云运行。
- **灾备冗余**: 在公有云建立实时热备份，防止物理机房故障导致的业务中断。

[^1]: [[Source_第十八讲：云计算与 5G：医疗数字化的“水、电、煤”.md]]
