---
id: "20260422_cf001"
title: "Concept: 认知摩擦 (Cognitive Friction)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "AI_Philosophy"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
ttl: 1825
categories: ["Philosophy_and_Cognitive", "Artificial_Intelligence"]
tags: ["慢思考", "安全性设计", "自动化偏见", "认知摩擦网闸", "Nature_Medicine"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十一讲：设计哲学 —— “人机协同”的本质是风险对冲.md", "raw/article/医疗数字化三十讲/第四部分：顾问素养篇——成为卓越的医疗数字化咨询专家.md", "raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260308.md", "raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260322.md"]
---

# Concept: 认知摩擦 (Cognitive Friction)

## 核心定位
通过增加交互“阻力”来强迫人类进入系统 2 思维。在医疗 AI 场景下，它是确保人类主权的核心防御机制。

## 认知摩擦网闸 (Cognitive Friction Gateway)
2026 年 Nature Medicine 研究表明，分诊失败的主因在于 UI/UX 导致的意图解析断层（[[Concept_Intent_Gap.md]]）[^2]。
- **思维模式融合 (Thinking Mode Fusion)**: Qwen3 等模型通过显式的 `/think` 标签实现了这一理念的代码化。这种“强制思考”过程在输出前制造了必要的逻辑停顿，迫使系统执行自检，本质上是一种数字化的认知摩擦 [^4]。
- **设计范式**: 在交互界面中故意设计“逻辑减震器”，引导人类进行二次核验，以对抗通用大模型在分诊中 **52% 的致死性漏诊风险**[^2][^3]。

## 顾问作为“摩擦引入者”
在数字化转型中，顾问的 **[[Concept_顾问三重角色.md]]** 之一——“教练”，本质上是在组织内部引入必要的认知摩擦。通过质疑现有流程与算法决策，防止系统被 AI 盲目接管，实现真正的风险对冲[^1]。

[^1]: [[Source_第四部分：顾问素养篇——成为卓越的医疗数字化咨询专家.md]]
[^2]: [[Source_Weekly_DigitalHealth_20260308.md]]
[^3]: [[Source_Weekly_DigitalHealth_20260322.md]]
[^4]: [[Source_2505.09388v1.md]]
