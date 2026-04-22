---
id: "20260422_atd01"
title: "AI 文本检测 (AI Text Detection)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "AI_Safety"
status: "Active"
alignment_score: 92
epistemic-status: "seed"
ttl: 730
categories: ["Artificial_Intelligence"]
tags: ["内容鉴别", "合规性", "SAE", "水印技术"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2503.03601v1.md"]
---

# AI 文本检测 (AI Text Detection)

## 核心定义
识别一段文本是由人类编写还是由人工智能生成的流程与技术体系。

## 技术路线演进
1. **统计特征法**: 依赖困惑度（Perplexity）和突发性（Burstiness）指标。
2. **端到端分类器**: 利用神经网络进行黑盒鉴别。
3. **可解释特征法**: 利用 **[[Concept_SAE_Sparse_Autoencoders.md]]** 提取机器特有的风格指纹（如句法过度复杂、过度冗长）进行精准判定 [^1]。

## 医疗合规性意义
- **科研诚信**: 防止 AI 伪造医学实验数据与论文。
- **医疗文书审计**: 识别由 AI 自动生成但未经人工校对的潜在高风险诊断报告。

[^1]: [[Source_2503.03601v1.md]]
