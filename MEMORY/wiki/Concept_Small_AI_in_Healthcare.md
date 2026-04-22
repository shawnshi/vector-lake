---
id: "20240410_sm01"
title: "Small AI (医疗端侧小模型)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["端侧AI", "下沉市场", "小脑化", "Small_AI"]
created: "2024-04-10"
updated: "2026-04-22"
sources: ["raw/DigitalHealthWeeklyBrief/DHWB-20260413.md", "raw/DigitalHealthWeeklyBrief/DHWB-20260419.md", "raw/HealthcareIndustryRadar/DHWB-Radar-20260308.md"]
---

# Small AI (医疗端侧小模型)

## 核心定义
Small AI 是指针对低算力、高响应、私有化部署场景优化的专用型医疗小参数大模型。它通过模型蒸馏、量化等技术，在极低显存（如 16G/24G）的传统服务器或专用硬件（如 [[System_WiNBOT.md]]）上实现单点任务的极致性能[^1]。

## 2026 年战略价值演进
1. **推理成本红利**: 麦肯锡报告指出，AI 推理成本两年内下降了 280 倍，使得 Small AI 的全院级部署在经济上变得完全可行[^1]。
2. **下沉市场清场**: 是卫宁、创业惠康等厂商进入县域医共体等预算有限场景的核心武器。通过“送算力 + 送 Small AI”的组合拳，实现对高价智算中心的降维打击[^1][^3]。
3. **小脑化 (Cerebellarization)**: Small AI 作为智能体的“小脑”，负责处理高频、实时的临床质控与文书生成，而将复杂的逻辑推演交给后方的“大脑”模型[^2]。

## 研发重点
- **极低延迟**: 在内网环境下实现 500ms 内的响应，确保不干扰医生的实时操作流[^1]。

## 关联页面
- [支持:: [[Concept_Cerebellarization.md]]]
- [物理支撑:: [[System_WiNBOT.md]]]
- [对手:: [[Entity_B-Soft.md]]]

[^1]: [[Source_DHWB-20260413.md]]
[^2]: [[Source_DHWB-20260419.md]]
[^3]: [[Source_DHWB-Radar-20260308.md]]
