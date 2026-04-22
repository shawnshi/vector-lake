---
id: "20260422_e001icu"
title: "Entity_ICU-IS (重症监护信息系统)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Clinical_System"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT"]
tags: ["ICU-IS", "重症", "临床决策支持", "数据分析"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md"]
---

# Entity: 重症监护信息系统 (ICU-IS)

## 定义
ICU-IS 是专门针对重症病房高频、高维、高压数据环境设计的临床系统。它充当了 ICU 医师的“第二大脑”，通过对海量生理参数的实时聚合，提供早期预警与决策支持[^1]。

## 核心能力
- **趋势分析**: 识别病人生命体征的细微波动，预防系统性崩溃。
- **临床集成**: 深度整合实验室数据（LIS）与医嘱记录，提供全景视图。
- **文书自动生成**: 极大降低重症护理人员在繁重书写上的负担。

## 关联节点
- [属于:: [[Entity_CIS.md]]]
- [支持:: [[Entity_HIP.md]]]

[^1]: [[Source_第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md]]
