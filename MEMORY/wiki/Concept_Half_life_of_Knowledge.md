---
id: "20260422_c002_hlk"
title: "Concept: Half-life of Knowledge (知识半衰期)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Risk_Management"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
categories: ["Artificial_Intelligence", "Biomedicine"]
tags: ["医学知识", "RAG", "时效性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md"]
---

# Concept: Half-life of Knowledge (知识半衰期)

## 定义
指某一领域的一半知识被证明是错误的或过时所经历的时间。在医学领域，由于科研进展飞速，知识的半衰期正不断缩短。

## 对 AI 的影响
- **数字古董化**: 静态预训练模型（LLM）的知识被冻结在训练截止日期，随着时间推移，其输出的准确性呈指数级衰减[^1]。
- **指南依赖性**: 临床决策必须基于最新的共识与指南，而非陈旧的统计概率。

## 解决方案
- **RAG (检索增强生成) [支持:: [[Concept_RAG.md]]]**: 通过外挂实时更新的医学知识库，确保 AI 输出基于最新证据而非过时权重。
- **定期微调**: 虽成本高昂，但可作为特定专病模型的补充。

[^1]: [[Source_第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md]]
