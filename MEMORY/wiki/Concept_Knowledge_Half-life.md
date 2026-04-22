---
id: "20260422_ckhl01"
title: "Concept_Knowledge_Half-life"
type: "concept"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 1825
categories: ["Biomedicine", "Artificial_Intelligence"]
tags: ["医学知识更新", "动态RAG", "模型过时"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md"]
---

# Concept: 知识半衰期 (Knowledge Half-life)

## 定义
医学知识库中一半内容失效或被新发现取代所需的时间。在数字化与 AI 时代，医学知识的增长与迭代呈指数级加速。

## 对医疗 AI 的影响
1. **静态模型即“古董”**: 模型训练完成之日，即其医学知识开始过时之时[^1]。
2. **RAG 的强制性**: 论证了医疗场景下，必须采用动态检索架构（RAG）以确保 AI 能访问到最新的临床指南和研究成果 [支持:: [[Concept_RAG.md]]]。
3. **持续学习挑战**: 如何在不导致灾难性遗忘的前提下，让模型快速吸收新知识。

## 战略对策
- **外挂知识库策略**: 重点投资于可随时更新的语义向量库，而非频繁微调基座模型。

[^1]: [[Source_第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md]]
