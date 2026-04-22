---
id: "20260422_c001spa"
title: "Concept: Spaghetti Architecture (意大利面条式架构)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Integration"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["System_Architecture"]
tags: ["P2P集成", "点对点", "脆弱性", "不可扩展"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md"]
---

# Concept: Spaghetti Architecture (意大利面条式架构)

## 定义
描述一种由于过度使用点对点集成（P2P）而导致的混乱、脆弱且不可扩展的网状连接架构。在这种架构中，每增加一个新系统，都需要与现有系统分别建立接口，导致接口数量呈几何级数增长（$N(N-1)/2$）[^1]。

## 特征
- **牵一发而动全身**: 任何一个系统的微小变动都可能导致多个关联接口失效。
- **维护成本极高**: 文档缺失，全靠“活字典”式的人工经验维系。
- **排查困难**: 数据流向不透明。

## 破局
引入 **[[Entity_ESB.md]]** 实现中心化管理，将网状连接降维为放射状连接。

## 关联节点
- [属于:: [[Concept_Information_Silos.md]]]
- [对比:: [[Entity_ESB.md]]]

[^1]: [[Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md]]
