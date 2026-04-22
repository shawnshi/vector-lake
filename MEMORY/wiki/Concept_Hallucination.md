---
id: "20260422_h01hac"
title: "Concept: 幻觉 (Hallucination)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Artificial_Intelligence"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["LLM", "Generative_AI", "Risk", "Safety"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md"]
---

# 幻觉 (Hallucination)

## 定义
在生成式 AI（GenAI）语境下，指大语言模型（LLM）生成看似合理但事实错误、无中生有或逻辑不通的信息的现象。在医疗领域，这被视为“一本正经地胡说八道”[^1]。

## 根源 [衍生于:: [[Concept_Probability_Machine.md]]]
- **文字接龙机制**: LLM 本质是基于概率预测下一个词，它并不具备对现实世界物理规则或医学真理的认知[^1]。
- **训练数据偏差**: 若语料库中存在错误或过时的医学信息，模型将习得这些偏差。

## 医疗风险
- **误诊建议**: 虚构虚假的治疗方案或药物剂量。
- **引用伪造**: 在生成临床报告时编造不存在的文献支持[^2]。 [属于:: [[Concept_Probability_Chasm.md]]]

## 治理策略
- **RAG (检索增强生成)**: 将模型锚定在真实的私有知识库上 [支持:: [[Concept_RAG.md]]]。
- **Prompt Engineering**: 使用“如果你不知道，请回答不知道”的约束性指令。
- **HITL (人机回环)**: 强制要求专业医生对生成内容进行终审 [关联:: [[Concept_HITL_2.0.md]]]。

[^1]: [[Source_第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md]]
[^2]: [[Source_第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md]]
