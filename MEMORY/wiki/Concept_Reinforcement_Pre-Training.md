---
id: "20260422_c_rpt"
title: "Concept: Reinforcement Pre-Training (RPT)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["RL", "Pre-training", "Corpus_as_Reward", "Thinking_Pre-training"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2506.13585v1.pdf"]
---

# Concept: Reinforcement Pre-Training (RPT)

## 定义
**强化学习预训练 (Reinforcement Pre-Training, RPT)** 是一种将强化学习机制前移至模型预训练阶段的新范式。与传统的“预测下一个词”的交叉熵损失不同，RPT 将语料库视为无穷的奖励信号源（Ground Truth），要求模型通过生成中间思维链（CoT）来对齐这些信号。

## 核心原理
1.  **语料即奖励 (Corpus as Reward)**: 将语料库中真实存在的后续文本作为 RL 的奖励目标。
2.  **推理换智能**: 牺牲训练时的生成长度（预测一个词需思考数千个词）来换取模型对语料背后逻辑的深度理解[^1]。
3.  **对冲“死记硬背”**: 强制模型在学习知识的同时学习逻辑推演，从而在根本上缓解幻觉问题。

## 行业影响
RPT 的出现预示着 **[[Concept_Industrialization_of_Cognition.md]]** 进入了“深加工”阶段。它不再满足于对人类知识的表面统计模仿，而是试图在预训练阶段就固化逻辑结构。

## 关联关系
*   **支持:: [[Concept_Agentic_Tree_Search.md]]**: 为预训练模型注入了天然的搜索与验证基因。
*   **挑战:: [[Concept_Scaling_Laws.md]]**: RPT 的算力成本远超传统方法，对“算力房东”模式提出了更高的资本要求。

[^1]: [[Source_2506.13585v1.md]]
