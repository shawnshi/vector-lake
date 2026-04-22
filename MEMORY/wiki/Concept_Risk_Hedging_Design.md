---
id: "20260422_rhd001"
title: "Concept: Risk Hedging Design (风险对冲设计)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "AI_Philosophy"
status: "Active"
alignment_score: 95
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["人机协同", "安全性设计", "系统韧性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十一讲：设计哲学 —— “人机协同”的本质是风险对冲.md"]
---

# Risk Hedging Design (风险对冲设计)

## 定义
风险对冲设计是一种针对概率性 AI 系统（如大语言模型）的防御性设计范式。它不追求消除错误，而是通过**人机协同**的结构性安排，使人类的确定性经验与 AI 的高效率能力互为备份，从而降低系统整体的失效风险。

## 核心原则
1.  **能力不对称备份**：用人的直觉/伦理对冲 AI 的幻觉；用 AI 的穷举能力对冲人的记忆盲点[^1]。
2.  **强制性摩擦 (Meaningful Friction)**：在高风险决策节点故意降低交互平滑度，防止人类产生“自动化偏见” [支持:: [[Concept_Cognitive_Friction.md]]]。
3.  **责任锚定 (Responsibility Anchoring)**：界面设计必须明确谁在为当前结果签字。严禁出现“AI 建议”导致责任主体模糊的情况。

## 实战案例
*   **取消一键采纳**：要求医生必须修改或补充 AI 生成的内容后才能提交。
*   **多选对比界面**：同时展示 3 个可能的诊断路径供医生裁决，而非直接给出最优解。

[^1]: [[Source_第十一讲：设计哲学 —— “人机协同”的本质是风险对冲.md]]
