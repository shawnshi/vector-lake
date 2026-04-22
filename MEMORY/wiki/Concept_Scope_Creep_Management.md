---
id: "20260422_cscope"
title: "Concept_Scope_Creep_Management"
title_alias: ["范围蠕变管理"]
type: "concept"
domain: "Medical_IT"
topic_cluster: "Project_Management"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["Project_Management", "Scope_Creep", "Governance", "CCB"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十七讲：项目管理与交付：确保蓝图不只是“墙上的画”.md"]
---

# Scope Creep Management (范围蠕变管理)

## 定义
范围蠕变（Scope Creep）指在项目执行过程中，由于未经过适当的评估与授权，导致项目范围在预定义边界之外不断扩张的现象。在医疗 IT 领域，这通常表现为临床科室不断提出的“随口需求”和对系统功能的无限制索取[^1]。

## 核心危害
- **[支持:: [[Concept_Delivery_Debt.md]]] 的积累**：非标需求的堆砌导致系统架构腐烂。
- **资源透支**：导致项目延期（Time）与成本（Cost）超支，破坏“铁三角”平衡[^1]。
- **交付动力丧失**：团队长期陷入琐碎细节，无法达成阶段性价值目标。

## 管理策略
1. **基准设定**：在立项阶段通过 [属于:: [[Concept_一页纸立项书.md]]] 明确“不做什么”。
2. **刚性变更门控 (CCB)**：
   - 建立 **[[Concept_CCB_变更控制委员会.md]]**，由医院管理层、CIO 与厂商项目负责人组成。
   - 所有需求变更必须经过“影响评估 -> 成本核算 -> 审批签字”流程[^1]。
3. **分期迭代**：将非核心需求推入“二期规划”，确保一期核心价值的极速落地。

## 关联概念
- [属于:: [[Concept_Value_Driven_Delivery.md]]]
- [对抗:: [[Concept_Spaghetti_Architecture.md]]]

[^1]: [[Source_第二十七讲：项目管理与交付：确保蓝图不只是“墙上的画”.md]]
