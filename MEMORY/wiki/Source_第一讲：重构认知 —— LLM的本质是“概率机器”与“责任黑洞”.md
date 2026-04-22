---
id: "20260422_s002_m1"
title: "Source: 第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”"
type: "source"
domain: "Medical_IT"
topic_cluster: "First_Principles"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence", "Philosophy_and_Cognitive"]
tags: ["第一性原理", "概率机器", "责任黑洞"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”.md"]
---

# 第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”

## 核心内容概述
作为系列课程的开篇，本讲从第一性原理出发，对大语言模型（LLM）在医疗场景下的本质进行了深刻祛魅。提出 LLM 是基于关联性的统计工具，其带来的效率红利与法律/医学要求的确定性之间存在天然张力。

## 关键提取
- **概率机器 [属于:: [[Concept_Probability_Machine]]]**: LLM 的核心运行逻辑是预测下一个 Token，这种随机性是其创造力的源泉，也是其不可消除幻觉的根源[^1]。
- **责任黑洞 [属于:: [[Concept_Responsibility_Black_Hole]]]**: 当 AI 辅助决策出现失误时，法律责任在医生、医院与厂商之间形成的模糊地带[^1]。
- **成本收益非对称性**: 医疗 AI 面临 99% 的效率提升与 1% 无限大风险的极端博弈[^1]。

## 论点与发现
- **定位辅助**: 由于归责困难，LLM 在现阶段的法律定位只能是“辅助工具”，其产出必须经过人类签署确权。
- **反脆弱架构**: 解决幻觉的方法不是追求 100% 准确（物理上不可能），而是通过架构（如 RAG）建立验证闭环。

[^1]: [[Source_第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”.md]]
