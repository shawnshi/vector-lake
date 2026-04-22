---
id: "20260422_s004_m3"
title: "Source: 第三讲：风险根源 —— 从技术缺陷到系统性脆弱"
type: "source"
domain: "Medical_IT"
topic_cluster: "Risk_Management"
status: "Active"
alignment_score: 96
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence", "System_Architecture"]
tags: ["风险管理", "幻觉", "知识半衰期"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md"]
---

# 第三讲：风险根源 —— 从技术缺陷到系统性脆弱

## 核心内容概述
深入剖析了医疗 AI 面临的三大技术根源性风险：幻觉的必然性、知识的半衰期以及模型的黑盒属性。作者强调，风险不应被掩盖，而应被作为资产进行系统性管理。

## 关键提取
- **幻觉的必然性**: 幻觉是生成式 AI 的底层特性，无法通过单纯增加参数量根除[^1]。
- **知识半衰期 [属于:: [[Concept_Half_life_of_Knowledge]]]**: 医学知识的快速更新导致静态权重模型迅速过时，成为“数字古董”[^1]。
- **黑盒审计 [关联:: [[Concept_Algorithm_Audit]]]**: 缺乏解释性使得临床回溯变得困难[^1]。

## 论点与发现
- **RAG 的必要性 [支持:: [[Concept_RAG]]]**: 通过检索增强生成来对抗知识陈旧和减少幻觉。
- **证据链前置**: 在输出结果的同时，必须强制附带原始医学依据，构建“证据网”[^1]。

[^1]: [[Source_第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md]]
