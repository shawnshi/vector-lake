---
id: "20260422_s17894"
title: "Source: xray-mutarjim: A 1.5B Specialized Model for Ar-En Translation"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Translation_Models"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["xray-mutarjim", "Specialized_AI", "Tarjama-25", "Arabic-English"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.17894v2.md", "raw/Huggingface-Daily-Papers/2505.17894v2.pdf"]
---

# xray-mutarjim: A 1.5B Specialized Model for Ar-En Translation

## 核心论点
该论文证明了在特定领域（如阿拉伯语-英语翻译），极高质量的专有数据与精准的架构优化，可以使 1.5B 的小参数模型在性能上超越 GPT-4o mini 等大参数通用模型。这有力地支持了“专才模型”在特定任务上的优越性。

## 关键技术与基准
1. **Tarjama-25**: 论文提出的一套涵盖 25 个子领域的专家级翻译评估基准，旨在解决现有翻译评估中严重的英语中心化偏差问题 [^1]。
2. **Causal Masking on Source (源端因果掩码)**: 通过在训练中对源语言端应用因果掩码，强制模型更聚焦于目标语言的生成质量，显著提升了翻译的流利度 [^1]。
3. **性能超越**: xray-mutarjim 在多个阿-英翻译指标上击败了参数量大其 20 倍的通用模型，证明了特定领域数据的“杠杆作用” [^1]。

## 战略启示
在医疗垂直领域，我们不应盲目追求大参数模型。针对特定临床场景（如病案翻译、专科问诊）微调的高质量小模型，可能是更经济、更高效的路径。这进一步验证了 **[[Concept_Small_AI_in_Healthcare.md]]** 的战略价值。

[^1]: [[Source_2505.17894v2.md]]
