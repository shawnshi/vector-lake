---
id: "20260422_intent_gap"
title: "Intent Gap (交互意图断层)"
type: "concept"
domain: "Philosophy_and_Cognitive"
topic_cluster: "Human-AI_Interaction"
status: "Active"
alignment_score: 97
epistemic-status: "sprouting"
categories: ["Artificial_Intelligence", "Philosophy_and_Cognitive"]
tags: ["UI/UX", "Triage_Failure", "Communication_Gap"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260308.md", "raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260322.md"]
---

# Intent Gap (交互意图断层)

## 定义
意图断层是指在人机交互过程中，人类模糊的语言表述与 AI 逻辑解析之间存在的鸿沟。尤其在医疗场景下，患者往往无法准确描述症状，导致 AI 获取的有效输入严重不足。

## 量化表现
研究显示，LLM 在直接处理结构化病历时的准确率为 **94.9%**，但在通过对话自行采集病史时，准确率下降至 **34.5%**[^1]。

## 根因分析
1. **开放式对话陷阱**: 缺乏结构化引导导致关键阴性症状缺失[^2]。
2. **自动化偏见反向作用**: AI 倾向于顺应人类的引导，而非进行批判性追问。

## 对抗策略
- **[[Concept_Cognitive_Friction.md]]**: 通过界面设计强制执行结构化录入，减少意图损失。
- **意图减震器 (Intent Damper)**: 在 MSL 层中对高频变动的意图进行平滑处理。

[^1]: [[Source_Weekly_DigitalHealth_20260308.md]]
[^2]: [[Source_Weekly_DigitalHealth_20260322.md]]
