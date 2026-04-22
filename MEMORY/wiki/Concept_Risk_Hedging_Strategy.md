---
id: "20260422_crisk01"
title: "Concept_Risk_Hedging_Strategy"
type: "concept"
domain: "Medical_IT"
topic_cluster: "AI_Design_Philosophy"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence", "Philosophy_and_Cognitive"]
tags: ["风险对冲", "设计哲学", "医疗AI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十一讲：设计哲学 —— “人机协同”的本质是风险对冲.md"]
---

# 风险对冲战略 (Risk Hedging Strategy)

## 定义
在医疗 AI 系统设计中，通过引入人类的经验、直觉与责任机制，来抵消大模型由于概率性本质产生的幻觉风险、逻辑断层及不可解释性错误的战略框架。

## 核心逻辑
医疗场景是典型的“低容错、高不确定”环境。AI 作为一个概率机器（Probability Machine），其本质与医疗所需的绝对确定性存在冲突。
- **AI 概率性**: 擅长大规模模式识别，但存在长尾分布下的幻觉。
- **人类确定性**: 擅长基于小样本、多模态上下文的终审与确权。
- **对冲路径**: 将 AI 定位为“证据生成器”，将人类定位为“决策终审官”[^1]。

## 实践原则
1. **三层协同对冲**: 监督、引导与深度协作。
2. **设计意图摩擦力**: 在高风险操作中强制人类介入，防止“自动化偏见”导致的无意识确认 [支持:: [[Concept_Cognitive_Friction.md]]]。
3. **责任闭环**: 系统设计必须确保在发生错误时，能够追溯到人类的审核节点，实现责任确权[^1]。

## 关联节点
- [对冲对象:: [[Concept_Hallucination.md]]]
- [设计手段:: [[Concept_HITL_2.0.md]]]
- [底层支持:: [[Concept_RAG.md]]]

[^1]: [[Source_第十一讲：设计哲学 —— “人机协同”的本质是风险对冲.md]]
