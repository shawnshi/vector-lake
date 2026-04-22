---
id: "20260412_p001_pm"
title: "Concept: Probability Machine (概率机器)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "AI_Logic"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence", "Philosophy_and_Cognitive"]
tags: ["LLM原理", "随机性", "幻觉根源"]
created: "2026-04-12"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”.md"]
---

# Concept: Probability Machine (概率机器)

## 定义
指大语言模型（LLM）的物理内核本质。其核心运作机制是基于海量语料库统计出的概率分布，通过上文预测下一个 Token 的出现概率，而非基于因果推断或逻辑演绎[^1]。

## 生成机制：文字接龙 (Next Token Prediction)
- **本质**: LLM 并不理解背后的科学原理，而是根据上下文预测概率最高的下一个词[^2]。
- **后果**: 这种概率本质导致了模型无法 100% 保证输出的确定性，必然产生 [[Concept_Hallucination.md]]（幻觉）[^2]。

## 核心特征
1. **统计关联而非因果**: 模型理解的是词与词之间的共现频率。
2. **随机性（Temperature）**: 随机性赋予了模型创造力，但也直接导致了**幻觉的必然性**。
3. **不可预测性**: 同样的输入在不同时刻可能产生细微差异。

## 医疗含义
在严肃的医疗场景中，“概率”与“确定性”之间存在天然鸿沟 [属于:: [[Concept_Probability_Chasm.md]]]。
- **幻觉资产化**: 将幻觉视为创造力副产品，通过外部约束（如 RAG）进行纠偏。
- **HITL**: 将概率机器限制在“建议”层级，必须通过人工审核 [关联:: [[Concept_HITL_2.0.md]]]。
- **祛魅**: 明确 LLM 不具备真实的“临床逻辑”，仅是高维度的文本模拟器[^1]。

## 关联概念
- [对比:: [[Concept_Responsibility_Black_Hole.md]]]
- [支持:: [[Concept_Half_life_of_Knowledge.md]]]

[^1]: [[Source_第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”.md]]
[^2]: [[Source_第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md]]
