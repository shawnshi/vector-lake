---
id: "20260422_c026"
title: "Concept: 四大架构 (Four Architectures)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["System_Architecture", "Strategy_and_Business"]
tags: ["业务架构", "应用架构", "数据架构", "技术架构", "蓝图规划"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十六讲：蓝图规划与方案设计：从“看病” to “开方”.md"]
---

# Concept: 四大架构 (Four Architectures)

## 定义
在医疗数字化顶层设计中，四大架构构成了蓝图规划的骨架体系。它们相互依存，形成从价值逻辑到底层技术的垂直贯通[^1]。

## 核心组成
1. **业务架构 (Business Architecture)**: 
   - **核心**: 描述医院的价值愿景、流程逻辑与组织结构。
   - **作用**: 回答“数字化是为了解决什么业务问题”。
2. **应用架构 (Application Architecture)**: 
   - **核心**: 定义支撑业务运行的软件系统集合及其交互关系。
   - **作用**: 明确系统边界（HIS, EMR, PACS 等）与功能布局。
3. **数据架构 (Data Architecture)**: 
   - **核心**: 规定数据的标准、流转方向与存储策略。
   - **作用**: 确保数据的“血液”在系统中顺畅流动 [关联:: [[Concept_MSL_医疗语义层.md]]]。
4. **技术架构 (Technology Architecture)**: 
   - **核心**: 定义底层的 IT 基础设施（服务器、网络、安全、云计算、容器化等）。
   - **作用**: 为上层架构提供物理稳定性与扩展性。

## 联动关系
- **由上而下**: 业务驱动应用，应用驱动数据，数据决定技术选型。
- **由下而上**: 技术创新（如云原生）赋能应用敏捷，应用沉淀数据价值，最终反哺业务创新。

## 关联节点
- [属于:: [[Source_第二十六讲：蓝图规划与方案设计：从“看病”到“开方”.md]]]
- [支持:: [[Concept_MSL_医疗语义层.md]]]

[^1]: [[Source_第二十六讲：蓝图规划与方案设计：从“看病”到“开方”.md]]
