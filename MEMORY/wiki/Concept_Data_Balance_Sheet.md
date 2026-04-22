---
id: "20260422_cdbs1"
title: "Concept_Data_Balance_Sheet"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Data_Strategy"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["数据资产", "数据负债", "ROI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第四讲：数据资产负债表 —— LLM的“燃料”与“负债”.md"]
---

# Concept: 数据资产负债表 (Data Balance Sheet)

## 定义
一种借用财务视角来评估医疗机构数据价值的框架。它认为数据并非天生就是资产，而是处于“资产”与“负债”的二元动态平衡中[^1]。

## 核心维度
1.  **数据资产 (Data Assets)**: 
    - 高质量、标注精准的临床路径数据。
    - 具备唯一性与稀缺性的专病数据集。
    - 能够直接驱动 AI 推理并产生临床/经济效益的活数据。
2.  **数据负债 (Data Liabilities)**: 
    - **合规风险**: 未经脱敏或授权的敏感数据。
    - **维护成本**: 存储大量“僵尸数据”带来的存储与运维开支。
    - **质量债务**: 字段缺失、逻辑冲突导致的“脏数据”，会在 AI 训练中产生误导性结果[^1]。

## 战略价值
引导医院管理者从“数字囤积癖”转向“资产运营者”，优先投资于能够“偿还债务”的数据治理工程 [支持:: [[Concept_数据治理.md]]]。

## 关联关系
- [属于:: [[Concept_Data_as_a_Product.md]]]
- [支持:: [[Concept_Value_Density.md]]]
- [衍生于:: [[Source_第四讲：数据资产负债表 —— LLM的“燃料”与“负债”.md]]]

[^1]: [[Source_第四讲：数据资产负债表 —— LLM的“燃料”与“负债”.md]]
