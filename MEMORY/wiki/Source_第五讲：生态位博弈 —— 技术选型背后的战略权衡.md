---
id: "20260422_s005"
title: "第五讲：生态位博弈 —— 技术选型背后的战略权衡"
type: "source"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Strategy_and_Business"]
tags: ["Strategic Triangle", "Privatization", "RAG vs Fine-tuning"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第五讲：生态位博弈 —— 技术选型背后的战略权衡.md"]
---

# 第五讲：生态位博弈 —— 技术选型背后的战略权衡

## 核心观点
本讲探讨了医疗机构在 AI 技术选型中的生态位博弈，强调大中型医院在复杂权衡中对“控制权”的绝对优先级选择[^1]。

## 关键内容
- **战略三角博弈**：医疗机构必须在 **效果 (Performance)**、**成本 (Cost)** 与 **控制权 (Control)** 之间做出三难权衡。大中型医院通常会牺牲一定的灵活性以换取确定性的控制权[^1]。
- **业主模式 (Homeowner Model)**：由于医疗对隐私、合规及自主可控的极高要求，中大型医院必然选择私有化部署。这一选择被形象地比喻为“房主”相对于“租客”的权力保障[^1]。
- **技术路线：RAG vs Fine-tuning**：确立了“RAG 是基石（开卷考试），微调是利刃（考前辅导）”的协同逻辑。RAG 确保准确性，微调提升机构化的特定语调[^1]。

## 战略对齐
- **关联 [[Concept_Tenant_vs_Homeowner_Model.md]]**：为部署路径的选择提供了战略分类法。
- **关联 [[Concept_可信数据空间.md]]**：私有化部署是构建可信数据空间的物理基座。
- **关联 [[Concept_MSL_医疗语义层.md]]**：在私有化环境下，语义层是确保控制权的最终防线。

[^1]: [[Source_第五讲：生态位博弈 —— 技术选型背后的战略权衡.md]]
