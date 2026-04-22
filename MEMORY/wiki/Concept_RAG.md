---
id: "20260422_rag001"
title: "Concept: RAG (Retrieval-Augmented Generation)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence", "System_Architecture"]
tags: ["架构", "责任分离", "真实性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十三讲：架构之道 —— “RAG优先”与内置“安全阀”.md"]
---

# Concept: RAG (Retrieval-Augmented Generation)

## 核心定位
RAG 是医疗 AI 系统的**默认架构项**。它通过将“推理能力”（由 LLM 提供）与“知识存储”（由向量数据库/MSL 提供）解耦，解决了医疗知识半衰期短、幻觉不可接受以及数据更新成本高的核心痛点。

## 战略意义：责任分离
在医疗严肃场景中，RAG 不仅是技术手段，更是一种**治理手段**：
1. **知识确权**：知识库（外部数据）负责提供“真实性”证据，LLM 仅负责执行“组织表达”与“逻辑转换”。
2. **审计闭环**：通过强制溯源 [支持:: [[Concept_Evidence_Mesh.md]]]，确保每一句 AI 输出都有据可查，从而在架构层面剥离了 LLM 的黑箱属性[^1]。

## 架构准则：RAG-First
主张所有临床决策支持系统（CDSS）应采用 RAG 优先架构，内置“安全阀”机制，包括敏感词脱敏与强制证据引用。

## 医疗场景应用
在医疗领域，RAG 扮演着“物理围栏”的角色。由于基础模型无法保证 100% 事实准确（存在 **[[Concept_Hallucination.md]]**），通过 RAG 挂载医院私有知识库或权威临床指南，可以强制 LLM 仅基于已知事实进行回答[^1]。

### 核心价值
- **对冲幻觉**：提供可溯源的证据链，满足医疗严肃性要求。
- **知识实时化**：无需重训练即可引入最新的诊疗共识。
- **[支持:: [[Concept_MSL_医疗语义层.md]]]**：作为语义层的工程实现，确保输出符合临床逻辑。

[^1]: [[Source_第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md]]
