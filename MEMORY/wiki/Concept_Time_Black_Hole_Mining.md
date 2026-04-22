---
id: "20260422_ctimebh"
title: "Concept_Time_Black_Hole_Mining"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Requirement_Engineering"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["时间黑洞", "需求挖掘", "临床人类学"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十二讲：需求挖掘 —— 深入工作流，定位“时间黑洞”.md"]
---

# 时间黑洞挖掘 (Time Black Hole Mining)

## 定义
通过临床人类学的实地观察方法，识别并量化医疗工作流中那些因系统割裂、流程冗余而导致的隐形效率损耗点的过程。

## 核心方法：秒表式蹲点
1. **物理介入**: 调研人员进入医生真实办公环境。
2. **动作拆解**: 记录医生完成一项任务（如书写首志）中，花费在不同系统（HIS, EMR, PACS）间切换的时间。
3. **数据发现**: 医生往往无法通过访谈说出这些痛点，因为他们已将其视为“环境的一部分”。只有通过第三方观察才能发现那些高频、机械、无意义的录入动作[^1]。

## 时间黑洞的典型特征
- **复制粘贴重灾区**: 结构化数据无法流转，被迫手动转抄。
- **多窗口跳动**: 为获取一个指标需要点击超过 5 次以上。
- **表单监狱**: 典型的过度行政合规导致的数据录入浪费 [关联:: [[Concept_表单监狱.md]]]。

## 价值翻译
挖掘出的时间黑洞是 AI 替代的最佳场景。将节省的“分钟”乘以医生的“单位工时成本”，即可得出 AI 的基础 ROI[^1]。

## 关联节点
- [方法论基础:: [[Concept_Clinical_Anthropology.md]]]
- [微观表现:: [[Concept_Paper_Cuts.md]]]

[^1]: [[Source_第十二讲：需求挖掘 —— 深入工作流，定位“时间黑洞”.md]]
