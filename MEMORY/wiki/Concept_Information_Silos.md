---
id: "20260422_c001sil"
title: "Concept: Information Silos (信息孤岛)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Integration"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT", "System_Architecture"]
tags: ["信息孤岛", "数据烟囱", "割裂", "互联互通"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md"]
---

# Concept: Information Silos (信息孤岛)

## 定义
医疗信息化的“原罪”。指由于部门专业化需求、系统采购时间差以及缺乏统一标准，导致医院内各 IT 系统之间无法直接通信、数据格式互不兼容、业务流程在系统边界断裂的现象[^1]。

### 隐喻：缸中之脑 (Brain in a Vat)
在缺乏集成平台（[[Concept_HIP_集成平台]]）的场景下，即便 EMR 拥有极其聪明的数据处理能力，但因无法感知外部系统的状态变化（如无法接收实时检验危急值），呈现出一种被“禁锢”在单一系统物理边界内的“缸中之脑”状态，无法产生全链路的临床协同价值[^1]。

## 根源分析
1. **部门利益主导**: 每个科室都希望拥有一套完全拟合其专业流程的“自留地”。
2. **厂商封闭性**: 部分厂商出于商业保护，人为设置数据壁垒。
3. **标准缺失**: 早期建设缺乏顶层规划。

## 解决路径
- **强制互联互通**: 建设 **[[Entity_HIP.md]]** 作为物理桥梁。
- **语义归一化**: 引入 **[[Medical Semantic Layer (MSL)]]** 消除理解偏差。

## 关联节点
- [支持:: [[Concept_Spaghetti_Architecture.md]]]
- [对抗:: [[Entity_HIP.md]]]

[^1]: [[Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md]]
