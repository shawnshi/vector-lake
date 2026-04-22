---
id: "20260422_e001aim"
title: "Entity_AIMS (手术麻醉信息系统)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Clinical_System"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT"]
tags: ["AIMS", "手术室", "麻醉", "数字驾驶舱", "黑匣子"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md"]
---

# Entity: 手术麻醉信息系统 (AIMS)

## 定义
AIMS (Anesthesia Information Management System) 是专门服务于手术室环境的数字化系统。它被定义为手术室的**“数字驾驶舱”**与**“黑匣子”**，负责实时捕获麻醉过程中的生理参数、用药记录及事件反馈[^1]。

## 核心职能
- **自动采集**: 实时对接麻醉机、监护仪，确保数据的客观性与连续性。
- **质控审计**: 记录手术过程中的每一个关键动作，是解决 **[[Concept_Responsibility_Black_Hole.md]]** 的物理证据来源。
- **闭环节点**: 与医嘱系统（CPOE）对接，确保术中用药的准确执行。

## 关联节点
- [属于:: [[Entity_CIS.md]]]
- [对抗:: [[Concept_Responsibility_Black_Hole.md]]]

[^1]: [[Source_第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md]]
