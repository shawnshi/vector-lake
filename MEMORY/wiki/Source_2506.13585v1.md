---
id: "20260422_s250613"
title: "Source: Reinforcement Pre-Training: Corpus as Reward Source"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 90
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["RPT", "Pre-training", "RL", "Qwen-2"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2506.13585v1.pdf"]
---

# Source: Reinforcement Pre-Training: Corpus as Reward Source (RPT)

## 核心内容摘要 (Abstract)
**[[Concept_Reinforcement_Pre-Training.md]]** (RPT) 提出了一种范式转移：将强化学习从微调阶段提前至预训练阶段。其核心思想是，原始文本语料库中的“下一个词”本身就是最客观、无穷的奖励信号。通过这种方式，模型不仅在学习“概率预测”，而是在预训练阶段就开始学习“逻辑推演”以达成该预测。

## 关键提取 (Key Extractions)

### 技术核心
*   **Corpus as Reward**: 将语料库文本视为 Ground Truth，通过强化学习目标（而非单纯的交叉熵损失）来优化模型。
*   **逻辑坍缩预防**: 相比于 SFT，RPT 在保持长程逻辑一致性方面表现更优，因为它强制模型在生成思维链时时刻对齐语料库中的真实结论。

### 路线斗争
*   **算力代价 vs. 智能收益**: RPT 的训练成本远高于传统预训练，因为预测单个 Token 需要生成数千 Token 的隐性推理。这引发了关于“暴力 Scaling”是否可持续的争议。

## 与 Wiki 的联系
*   **关联 [[Concept_Industrialization_of_Cognition.md]]**: RPT 是将人类语料作为逻辑原材料进行大规模工业化加工的终极体现。
*   **支持 [[Concept_Agentic_Tree_Search.md]]**: RPT 在预训练阶段就为模型植入了树状搜索与回溯的潜意识。

[^1]: [[Source_2506.13585v1.md]]
