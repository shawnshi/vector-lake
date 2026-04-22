---
id: "20260422_c_hip"
title: "Concept_HIP_集成平台"
type: "concept"
domain: "Medical_IT"
topic_cluster: "System_Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 1825
categories: ["Healthcare_IT", "System_Architecture"]
tags: ["HIP", "集成平台", "ESB", "解耦", "互联互通"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md"]
---

# Concept | 医院信息集成平台 (HIP)

## 定义
**医院信息集成平台 (Hospital Integration Platform, HIP)** 是医疗机构内部系统的“中央神经系统”，旨在通过标准化的技术手段实现各子系统（HIS, EMR, PACS, LIS 等）之间的互联互通与业务协同。

## 核心价值：解耦
HIP 的本质是将系统间的“网状强耦合”转化为“星型松耦合”。
- **痛点**：意大利面式架构（[[Concept_Spaghetti_Architecture]]）导致牵一发而动全身[^1]。
- **解决方案**：引入 ESB (企业服务总线)，所有系统仅需与平台对接一次，由平台负责指令的分发与协议转换[^1]。

## 关键组成
1. **ESB (企业服务总线)**：物理底座，负责消息路由与转换。
2. **CDR (临床数据仓库)**：实现跨系统的临床数据聚合。
3. **标准适配器**：支持 HL7, DICOM, FHIR 等标准协议。
4. **主索引 (MPI)**：解决患者身份在不同系统间的唯一性识别。

## 与 MSL 的关系
HIP 解决了物理层的“通”，而 [[Concept_MSL_医疗语义层]] 旨在解决逻辑层的“懂”。HIP 是 MSL 落地的物理基础。

[^1]: [[Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md]]
