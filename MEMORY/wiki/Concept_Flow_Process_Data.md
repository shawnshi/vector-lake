---
id: "20260422_cfpd"
title: "Concept_Flow_Process_Data"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Data_Strategy"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence", "Strategy_and_Business"]
tags: ["过程数据", "隐性知识", "核心资产", "护城河"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十五讲：构建真护城河 ——驱动“过程数据”的进化飞轮.md"]
---

# 过程数据 (Flow Process Data)

## 定义
过程数据是指在 AI 介入工作流后，捕获的用户与 AI 之间交互的动态轨迹。它不同于传统的“存量事实数据”（如化验单、出院小结），更多地表现为用户对 AI 建议的采纳、修改或驳回的决策过程[^1]。

## 核心价值
- **隐性知识显性化**：通过捕获专家对 AI 建议的微调，系统得以“学习”那些无法写进教科书的临床直觉与经验[^1]。
- **排他性壁垒**：过程数据是在特定的工作流、特定的医院语境下生成的，竞争对手即便拥有相同的算法，也无法获取这些反映“临床潜规则”的数据资产[^1]。
- **飞轮燃料**：是驱动 [[Concept_Evolution_Flywheel.md]] 的核心动力，直接用于 SFT（监督微调）和 RLHF（强化学习）。

## 与存量数据的对比
| 维度 | 存量事实数据 (Stock Fact Data) | 过程数据 (Flow Process Data) |
| :--- | :--- | :--- |
| **本质** | 历史结果的静态记录 | 决策意图的动态轨迹 |
| **可及性** | 易被多方调取/清洗 | 仅在深度嵌入的闭环中生成 |
| **价值趋势** | 随着基础模型增强而稀释 | 随着场景深入而指数级增值 |

## 现实意义
在 [[Entity_Winning_Health.md]] 的战略中，通过 [[Entity_WiNEX.md]] 深度嵌入临床，本质上是在进行大规模的过程数据“采矿”[^1]。

[^1]: [[Source_第十五讲：构建真护城河 ——驱动“过程数据”的进化飞轮.md]]
