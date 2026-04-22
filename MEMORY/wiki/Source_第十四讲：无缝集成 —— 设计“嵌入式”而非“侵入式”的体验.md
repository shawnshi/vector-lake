---
id: "20260422_s10014"
title: "Source: 第十四讲：无缝集成 —— 设计“嵌入式”而非“侵入式”的体验"
type: "source"
domain: "Medical_IT"
topic_cluster: "Product_Design"
status: "Active"
alignment_score: 98
epistemic-status: "evergreen"
ttl: 365
categories: ["Healthcare_IT"]
tags: ["嵌入式体验", "认知负荷", "电子病历集成"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十四讲：无缝集成 —— 设计“嵌入式”而非“侵入式”的体验.md"]
---

# 第十四讲：无缝集成 —— 设计“嵌入式”而非“侵入式”的体验

## 核心内容摘要
本讲探讨了医疗 AI 产品的交互哲学，提出了“**嵌入式体验 (Embedded Experience)**”优于“侵入式交互”的核心观点[^1]。作者认为，医生对新系统的容忍度极低，任何需要跳出当前工作流（如切换窗口、重新登录）的 AI 都是失败的[^1]。

核心设计原则：
1.  **在场感**：AI 应作为 EMR/HIS 的原生组件或侧边栏存在[^1]。
2.  **结论先行，证据在后**：直接给出建议，仅在用户需要时展示推理链[^1] [关联:: [[Concept_Responsibility_Black_Hole.md]]]。
3.  **微反馈设计**：在不中断操作的前提下捕获用户对 AI 的修正[^1] [支持:: [[Concept_Flow_Process_Data.md]]]。

## 关键提取
- **认知负荷最小化**：通过减少点击次数和信息噪音来降低医生的抗拒感 [支持:: [[Concept_Cognitive_Friction.md]]]。
- **交互即数据**：每一次“采纳”或“修正”动作都是高质量的 **[[Concept_Flow_Process_Data.md]]**[^1]。

[^1]: [[raw/article/医疗大语言模型应用二十讲/第十四讲：无缝集成 —— 设计“嵌入式”而非“侵入式”的体验.md]]
