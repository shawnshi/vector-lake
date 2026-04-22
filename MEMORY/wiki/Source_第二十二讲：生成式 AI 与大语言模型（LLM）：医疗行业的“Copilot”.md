---
id: "20260422_s22genai"
title: "Source_第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”"
type: "source"
domain: "Medical_IT"
topic_cluster: "Artificial_Intelligence"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["LLM", "Generative_AI", "Copilot", "RAG"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md"]
---

# 第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”

## 核心概述
本讲聚焦于从“判别式 AI”向“生成式 AI”的范式转移，定义了 LLM 作为医疗行业“副驾驶”（Copilot）的新定位。分析了 LLM 在病历自动生成、对话式问诊及科研辅助中的潜力，同时严厉警示了其“幻觉”风险，并提出了 [支持:: [[Concept_RAG.md]]] 作为工程化治理的核心路径[^1]。

## 关键知识点

### 1. 范式转移：从“找茬”到“创作”
- **判别式 AI**：擅长分类与预测（如：这是不是肿瘤？）。
- **生成式 AI**：擅长基于概率接龙创造新内容（如：写一份出院小结）[^1]。

### 2. 医疗 Copilot 的典型场景
- **[[Concept_Ambient_Scribing.md]]**：环境感知病历生成，让医生回归临床，而非录入员。
- **对话式 BI**：让非技术人员通过自然语言查询医院运营数据[^1]。
- **智慧导诊**：基于语义理解的精准分诊[^1]。

### 3. 核心阻力：幻觉与知识更新
- **[[Concept_Hallucination.md]]**：LLM 编造病历或用药建议的致命风险[^1]。
- **知识断层**：基础模型无法实时更新最新的临床指南。

## 关键论点
- **LLM 不是决策者，是助手**：在医疗这种高冗余、高责任场景下，必须保持 [属于:: [[Concept_HITL_2.0.md]]][^1]。
- **RAG 是医疗 LLM 的“物理围栏”**：通过挂载 [属于:: [[Concept_MSL_医疗语义层.md]]]，强制模型在事实轨道上运行[^1]。

[^1]: [[raw/article/医疗数字化三十讲/第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md]]
