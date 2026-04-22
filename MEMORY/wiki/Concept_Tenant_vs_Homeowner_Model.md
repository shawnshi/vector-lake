---
id: "20260422_c003"
title: "租客与业主模型 (Tenant vs Homeowner Model)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Strategy_and_Business"]
tags: ["Deployment Model", "SaaS vs On-premise", "Strategic Triangle", "Control"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第五讲：生态位博弈 —— 技术选型背后的战略权衡.md"]
---

# 租客与业主模型 (Tenant vs Homeowner Model)

## 定义
这是一个形象化的战略比喻，用于描述医疗机构在 AI 部署路径上的三种核心选择：**API 调用（租客）**、**私有化部署（业主）**、**开源自建（自建房）**[^1]。

## 三种模式对比
1.  **租客模式 (Tenant/API)**：
    - **特征**：直接调用云端大模型 API。
    - **优点**：启动快、初始成本极低、模型能力最强（如 GPT-4）。
    - **缺点**：隐私风险大、控制权完全丧失、长期成本不透明。
2.  **业主模式 (Homeowner/Privatization)**：
    - **特征**：购买算力服务器，将模型私有化部署在院内。
    - **优点**：数据主权完全掌控、响应速度稳定、符合合规要求。
    - **缺点**：初始投资（Capex）高、需自主运维。
3.  **自建房模式 (Self-build/Open Source)**：
    - **特征**：基于开源模型（如 Llama）进行大规模微调和二次开发。
    - **优点**：极致的灵活性与垂直领域深度。
    - **缺点**：人力成本极高、容易陷入技术陷阱。

## 战略选择：战略三角
在 **效果 (Performance)**、**成本 (Cost)**、**控制权 (Control)** 的“战略三角”中，中大型公立医院在 AI 时代会坚定地选择**业主模式**，以保卫其核心的数据主权和临床解释权[^1]。

## 关联概念
- [属于:: [[Concept_OS_Strategy.md]]]
- [关联:: [[Concept_可信数据空间.md]]]
- [对比:: [[Concept_Data_Feudalism.md]]]

[^1]: [[Source_第五讲：生态位博弈 —— 技术选型背后的战略权衡.md]]
