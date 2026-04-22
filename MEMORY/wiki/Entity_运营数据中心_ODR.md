---
id: "20260422_ent001"
title: "运营数据中心 (ODR)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Data_Strategy"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT"]
tags: ["ODR", "数据中心", "运营管理", "决策支持"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第十三讲：数据决策之脑：医院运营数据中心（ODR）与 BI 应用.md"]
---

# 运营数据中心 (ODR - Operation Data Repository)

## 定义
**ODR** 被定义为医院的“决策之脑”，是专门为医院运营管理设计的、具备高度集成与分析能力的数据资产中心。它区别于传统的临床数据中心 (CDR)，侧重于人、财、物、效率与质量的横向拉通。

## 核心职能
1. **决策支持**: 回答管理者的具体经营问题，如“DRG 盈亏原因”、“床位周转率瓶颈”等 [衍生于:: [[Source_第十三讲：数据决策之脑：医院运营数据中心（ODR）与 BI 应用.md]]]。
2. **数据治理锚点**: 作为主数据管理 (MDM) 的核心载体，解决跨系统的数据同名异物、异名同物问题 [支持:: [[Concept_数据治理.md]]]。
3. **管理闭环驱动**: 通过 BI 工具将洞察推送至执行层，实现从发现问题到闭环管理的逻辑连通。

## 技术特性
- **主题化建模**: 围绕人、财、物等管理维度构建数据集市。
- **高频 ETL**: 保持与业务系统（如 HIS, HRP, EMR）的高度同步。
- **多维钻取**: 支持从全院宏观指标下钻至科室、单病种、甚至单张处方的微观细节。

## 战略地位
在 **[[Concept_DRG_DIP.md]]** 与公立医院高质量发展的双重压力下，ODR 是医院实现“精算管理”不可或缺的认知基础设施。它将数据从“负资产（数据坟场）”转化为支撑决策的“金矿”。

## 相关链接
- [属于:: [[Entity_WiNEX.md]]] (WiNEX DnA 的核心组件)
- [对比:: [[Concept_CDR.md]]] (侧重临床 vs. 侧重运营)
- [支持:: [[Concept_SMART_价值指标.md]]]
