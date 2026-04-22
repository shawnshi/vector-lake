---
id: "20260422_c_causal_mask"
title: "Concept: 源端因果掩码 (Causal Masking on Source)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 92
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["NLP", "Masking_Strategy", "Translation_Optimization"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.17894v2.md"]
---

# Concept: 源端因果掩码 (Causal Masking on Source)

## 核心定义
在 Seq2Seq 或解码器架构的训练中，除了对目标语言端应用因果掩码外，也对源语言输入序列应用因果掩码，强制模型按顺序处理源信息。

## 技术价值
- **压力测试**: 通过限制模型对源端信息的“未来视角”，迫使其在生成翻译时必须更紧密地跟随输入逻辑，减少跳译或漏译。
- **提升流利度**: 在 **[[Entity_xray-mutarjim.md]]** 的实验中，该技术被证明能有效提升 1.5B 小参数模型在生成长句时的逻辑连贯性 [^1]。

## 临床翻译隐喻
在高度严谨的医疗文书翻译中，这种“强顺序约束”有助于防止模型为了句式美化而导致关键医学术语或诊断结论的语义漂移。

[^1]: [[Source_2505.17894v2.md]]
