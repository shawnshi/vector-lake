---
id: "20260422_c006grag"
title: "GraphRAG"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "RAG"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence", "System_Architecture"]
tags: ["GraphRAG", "知识图谱", "检索增强", "消除幻觉", "临床路径"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/research/AI 原生时代医疗机构语义层 (Semantic Layer) 建设战略报告.md"]
---

# GraphRAG

## 定义
**GraphRAG** 是一种结合了结构化知识图谱与非结构化向量检索的增强生成技术。它通过在检索阶段引入图拓扑关系，确保 AI 的推理路径符合既定的逻辑约束[^1]。

## 医疗应用
- **消除幻觉**：在医疗问答中，GraphRAG 强制 AI 沿着标准的“症状-检查-诊断-治疗”路径进行推理，而非仅凭概率相关性产生输出[^1]。
- **语义层基石**：是 [[Concept_MSL_医疗语义层.md]] 实现高保真业务 Grounding 的核心技术手段。

## 优势
相比传统的向量 RAG，GraphRAG 具备更强的全局语义理解能力与逻辑一致性，是解决医疗 AI “黑盒黑产”问题的关键路径[^1]。

[^1]: [[Source_AI 原生时代医疗机构语义层 (Semantic Layer) 建设战略报告.md]]
