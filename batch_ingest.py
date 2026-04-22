import os
import datetime
from pathlib import Path

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def write_wiki_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Written: {filename}")

def append_wiki_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(content)
    print(f"Appended: {filename}")

# --- SOURCE PAGES ---

source_1 = r'''---
id: "20260422_src_mas_strat"
title: "医疗MAS架构策略_20260419_Full"
type: "source"
domain: "Medical_IT"
topic_cluster: "Architecture"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 95
categories: ["System_Architecture"]
tags: ["MAS", "多智能体", "架构策略", "异步计算"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗MAS架构策略_20260419_Full.md"]
---
# 医疗MAS架构策略_20260419_Full

## 核心主旨
深入探讨医疗多智能体系统 (MAS) 的工程化落地路径，提出应对大模型长上下文塌陷的物理隔离方案，并界定了异步计算与意图路由在医疗高频交互中的核心地位。

## 关键实体
- **ACE (智能体协调引擎)**：分布式多智能体调度的核心 [属于:: [[Concept_Agentic_Hospital]]]。
- **Intent-Driven Router (意图驱动路由器)**：毫秒级分流网关 [关联:: [[Concept_Intent_Driven_Router]]]。

## 关键概念
- **MAS (多智能体系统) 医疗架构**：将任务切割为专职 Agent 集群，解决单体模型记忆塌陷问题 [属于:: [[Concept_MAS_Medical_Architecture]]]。
- **异步计算架构**：通过异步消息队列 (Kafka) 解决 MAS 串行交互导致的延迟黑洞[^1]。

## 核心论点
1. **单体模型的终结**：Monolithic LLM 在临床复杂语境下必然遭遇注意力塌陷，MAS 是医疗 AI 的唯一工程化出路。
2. **三位一体防御**：通过意图路由拦截 80% 确定性流量，剩余 20% 走异步计算，确保毫秒级 SLA[^1]。

[^1]: [[Source_医疗MAS架构策略_20260419_Full.md]]
'
write_wiki_file("Source_医疗MAS架构策略_20260419_Full.md", source_1)

source_2 = r'''---
id: "20260422_src_rcm_auto"
title: "医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现"
type: "source"
domain: "Medical_IT"
topic_cluster: "RCM"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 92
categories: ["Strategy_and_Business"]
tags: ["RCM", "自动驾驶", "AI代理", "博弈论"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现.md"]
---
# 医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现

## 核心主旨
定义 RCM 从 Copilot 向 Autopilot 的跨越，描述医院如何利用 AI 智能体对抗支付方的算法审核。

## 关键实体
- **WellSpan Health**：验证了 AI 代理在行政/呼叫中心的完全自主权 [属于:: [[Entity_WellSpan_Health]]]。

## 关键概念
- **RCM 自动驾驶 (Autopilot RCM)**：收入循环管理从辅助驾驶转向 AI 自主完成编码与申诉 [属于:: [[Concept_Agentic_RCM]]]。
- **算法博弈**：医院与医保/商保之间的算法长矛对抗算法盾牌[^1]。

## 核心论点
1. **从副驾驶到主驾驶**：AI 不再仅提供建议，而是直接承担申诉律师的角色。
2. **责任闭环**：通过 [关联:: [[Concept_Identity_Threading]]] 将 AI 动作挂载至人类数字身份。

[^1]: [[Source_医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现.md]]
'
write_wiki_file("Source_医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现.md", source_2)

source_3 = r'''---
id: "20260422_src_155_plan"
title: "医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃"
type: "source"
domain: "Medical_IT"
topic_cluster: "Policy"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 98
categories: ["Healthcare_IT"]
tags: ["十五五", "数字化重塑", "数据要素", "算法治院"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃.md"]
---
# 医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃

## 核心主旨
解读“十五五”政策背景下的医疗数字化重塑，强调从简单的系统连接转向利用算法重构生产关系。

## 关键实体
- **国家卫健委 (NHC)**：数字化重塑的行政推手 [属于:: [[Entity_NHC]]]。
- **国家医保局 (NHSA)**：通过 DRG/DIP 改革强制推动数字化转型 [属于:: [[Entity_NHSA]]]。

## 关键概念
- **十五五“重塑”**：从“连接”转向利用算法重构生产关系 [属于:: [[Synthesis_十五五医疗数字化战略]]]。
- **逻辑湖 (Logic Lake)**：捕获顶级专家对 AI 建议的修正记录 [属于:: [[Concept_Logic_Lake]]]。

## 核心论点
1. **生产关系重构**：数字化不再是辅助工具，而是医院运营的底层算法。
2. **制度深潜**：利用行政手段（如检查互认）强制推行技术标准[^1]。

[^1]: [[Source_医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃.md]]
'
write_wiki_file("Source_医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃.md", source_3)

source_4 = r'''---
id: "20260422_src_himss2026"
title: "医疗数字化的重力迁徙_HIMSS2026战略观察"
type: "source"
domain: "Medical_IT"
topic_cluster: "Strategy"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 90
categories: ["Strategy_and_Business"]
tags: ["HIMSS2026", "重力迁徙", "Epic-first", "Sovereign AI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数字化的重力迁徙_HIMSS2026战略观察.md"]
---
# 医疗数字化的重力迁徙_HIMSS2026战略观察

## 核心主旨
通过 HIMSS2026 观察，揭示全球医疗 IT 市场的重力转移：从通用平台向主权 AI 与刚性生态收缩。

## 关键实体
- **Epic Systems**：通过智能体工厂实施物理级占领 [关联:: [[Entity_Epic_Systems]]]。
- **Mehmet Oz 博士**：CMS 局长，推动 AI 作为通缩工具[^1]。

## 关键概念
- **Sovereign AI (主权 AI)**：作为医疗机构生存底线的私有化部署。
- **Agent Factory (智能体工厂)**：Epic 的核心 AI 交付模式 [关联:: [[智能体工厂 (Agent Factory)]]]。

## 核心论点
1. **Epic-first 战略**：第三方 SaaS 正在被平台巨头挤压至边缘。
2. **通缩工具**：AI 代理被用于 Medicare 成本控制[^1]。

[^1]: [[Source_医疗数字化的重力迁徙_HIMSS2026战略观察.md]]
'
write_wiki_file("Source_医疗数字化的重力迁徙_HIMSS2026战略观察.md", source_4)

source_5 = r'''---
id: "20260422_src_mas_hospital"
title: "医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final"
type: "source"
domain: "Medical_IT"
topic_cluster: "Strategy"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 96
categories: ["Healthcare_IT"]
tags: ["智能体医院", "范式重构", "逻辑资产", "Evidence-Mesh"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md"]
---
# 医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final

## 核心主旨
宣告单体聊天机器人模式的失效，提出“智能体医院”作为下半场的核心范式。

## 关键实体
- **Shawn Shi**：提出了 MSL、ACE、逻辑湖等核心框架 [属于:: [[Entity_Shawn_Shi]]]。

## 关键概念
- **Agentic Hospital (智能体医院)**：放弃表单中心，转向意图驱动的系统架构 [关联:: [[Agentic Hospital (智能体医院)]]]。
- **Evidence-Mesh (证据网)**：为 AI 结论提供逻辑拓扑 [属于:: [[Concept_Evidence_Mesh]]]。

## 核心论点
1. **从套壳到重构**：简单的 LLM 接口无法承载临床业务。
2. **逻辑资产捕获**：专家修正数据是比物理病历更昂贵的资产[^1]。

[^1]: [[Source_医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md]]
'
write_wiki_file("Source_医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md", source_5)

mas_concept = r'''---
id: "20260422_concept_mas_arch"
title: "MAS (多智能体系统) 医疗架构"
type: "concept"
domain: "System_Architecture"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 95
categories: ["System_Architecture"]
tags: ["多智能体", "分布式系统", "临床架构"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗MAS架构策略_20260419_Full.md"]
---
# MAS (多智能体系统) 医疗架构

## 定义
为解决单体大模型（Monolithic LLM）在临床复杂语境下的“注意力塌陷”与“幻觉爆炸”，将复杂任务切割并分发给具备特定领域能力的专职智能体集群的分布式架构[^1]。

## 核心组件
- **ACE (智能体协调引擎)**：负责智能体间的任务编排与状态流转。
- **Intent-Driven Router**：[关联:: [[Concept_Intent_Driven_Router]]]。

## 医疗价值
通过物理层面的分工，实现了对复杂医学逻辑的精细化处理，同时通过异步计算架构解决了链式推理引发的延迟黑洞。

[^1]: [[Source_医疗MAS架构策略_20260419_Full.md]]
'
write_wiki_file("Concept_MAS_Medical_Architecture.md", mas_concept)

rcm_concept = r'''---
id: "20260422_concept_agent_rcm"
title: "Agentic RCM (自动驾驶收入循环管理)"
type: "concept"
domain: "Healthcare_IT"
topic_cluster: "RCM"
status: "Active"
alignment_score: 92
epistemic-status: "evergreen"
categories: ["Strategy_and_Business"]
tags: ["RCM", "自动驾驶", "算法博弈"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁 in 为医院的现.md"]
---
# Agentic RCM (自动驾驶收入循环管理)

## 定义
医疗收入循环管理从“Copilot (辅助驾驶)”向“Autopilot (自动驾驶)”的跨越。由 AI 智能体自主完成临床编码、拒付申诉和与支付方的动态博弈[^1]。

## 核心机制
- **自主申诉**：AI 代理作为非结构化数据的“辩护律师”。
- **身份挂载**：[依赖:: [[Concept_Identity_Threading]]]，实现法理合规。

[^1]: [[Source_医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现.md]]
'
write_wiki_file("Concept_Agentic_RCM.md", rcm_concept)

logic_lake = r'''---
id: "20260422_concept_logic_lake"
title: "逻辑湖 (Logic Lake)"
type: "concept"
domain: "Philosophy_and_Cognitive"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 94
categories: ["Philosophy_and_Cognitive"]
tags: ["隐性知识", "专家修正", "数字资产"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃.md", "raw/article/digitalhealthobserve/医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md"]
---
# 逻辑湖 (Logic Lake)

## 定义
专门用于捕获、存储和检索顶级医学专家对 AI 建议的“修正、驳回与补充”记录的数字资产层。

## 价值锚点
逻辑湖捕获的是比物理病历更昂贵的“诊疗思维资产”。它解决了 AI 泛化知识与临床专家个体智慧之间的断层，是构建 [关联:: [[Agentic Hospital (智能体医院)]]] 的核心护城河[^1]。

[^1]: [[Source_医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’范式坍塌与重构_20260304_final.md]]
'
write_wiki_file("Concept_Logic_Lake.md", logic_lake)

router_concept = r'''---
id: "20260422_concept_intent_router"
title: "意图驱动路由器 (Intent-Driven Router)"
type: "concept"
domain: "System_Architecture"
topic_cluster: "Architecture"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 93
categories: ["System_Architecture"]
tags: ["意图识别", "流量分发", "SLA控制"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗MAS架构策略_20260419_Full.md"]
---
# 意图驱动路由器 (Intent-Driven Router)

## 定义
部署在医疗系统边缘的毫秒级流量网关，通过识别用户意图，将请求分流至“确定性规则引擎”或“异步 MAS 推理链”[^1]。

## 工程策略
- **80/20原则**：拦截 80% 的高频确定性流量，防止其涌入昂贵且高延迟的 LLM 链路。
- **语义缓存**：执行记忆脱水，重用共识。

[^1]: [[Source_医疗MAS架构策略_20260419_Full.md]]
'
write_wiki_file("Concept_Intent_Driven_Router.md", router_concept)

id_threading = r'''---
id: "20260422_concept_id_threading"
title: "身份穿透 (Identity Threading)"
type: "concept"
domain: "Philosophy_and_Cognitive"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 91
categories: ["Philosophy_and_Cognitive"]
tags: ["数字身份", "法理闭环", "责任分担"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现.md"]
---
# 身份穿透 (Identity Threading)

## 定义
一种法理闭环机制，规定 AI 代理不具备独立的临床或财务执行权，其所有生成的建议和发起的动作必须实时挂载并穿透至特定人类医生的数字身份[^1]。

## 应用场景
在 [关联:: [[Concept_Agentic_RCM]]] 中，确保每一笔由 AI 发起的申诉都在人类专家的法理监管下完成。

[^1]: [[Source_医疗收入循环的“自动驾驶”时刻：当 AI 不再是副驾驶，谁在为医院的现.md]]
'
write_wiki_file("Concept_Identity_Threading.md", id_threading)

plan_synth = r'''---
id: "20260422_synth_155_strat"
title: "十五五医疗数字化战略"
type: "synthesis"
domain: "Healthcare_IT"
topic_cluster: "Strategy"
status: "Active"
epistemic-status: "sprouting"
alignment_score: 98
categories: ["Healthcare_IT"]
tags: ["十五五", "数据要素", "算法治院", "中试基地"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃.md", "Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md"]
---
# 十五五医疗数字化战略

## 战略跃迁
“十五五”标志着医疗数字化从“连接阶段”向“重塑阶段”的惊险一跃。其核心不再是建设孤立的系统，而是利用算法重构医疗生产关系[^1]。

## 落地支柱
1. **数据要素资产化**：将医疗数据从成本中心转变为可变现、可评估的资产。
2. **算法治院**：将 DRG/DIP、质控规则嵌入算法内核。
3. **中试基地**：建立 [关联:: [[Concept_国家人工智能应用中试基地]]] 进行合规沙箱验证。

[^1]: [[Source_医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃.md]]
'
write_wiki_file("Synthesis_十五五医疗数字化战略.md", plan_synth)

hospital_update = r'''---
id: "20260422_concept_agent_hosp_upd"
title: "Agentic Hospital (智能体医院)"
type: "concept"
domain: "Healthcare_IT"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 96
categories: ["Healthcare_IT"]
tags: ["智能体", "系统重构", "ACE"]
created: "2026-04-11"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md", "raw/article/digitalhealthobserve/医疗MAS架构策略_20260419_Full.md"]
---
# Agentic Hospital (智能体医院)

## 定义
放弃“以表单为中心”的旧 HIS 思维，转向以“意图与自主协同为中心”的新一代医疗系统架构。

## 架构演进 (2026-04-22 Update)
智能体医院的核心不仅是 Agent 的堆砌，而是引入了 **ACE (智能体协调引擎)** 作为中枢，并配合 **[包含:: [[Concept_MAS_Medical_Architecture]]]** 实现分布式逻辑处理。系统通过 [依赖:: [[Concept_Intent_Driven_Router]]] 实现了与临床流程的无感集成[^1]。

## 核心支柱
- **逻辑湖**：[关联:: [[Concept_Logic_Lake]]]。
- **证据网**：[关联:: [[Concept_Evidence_Mesh]]]。

[^1]: [[Source_医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’范式坍塌与重构_20260304_final.md]]
'
write_wiki_file("Agentic Hospital (智能体医院).md", hospital_update)

msl_update = r'''---
id: "20260422_concept_msl_upd"
title: "Medical Semantic Layer (MSL)"
type: "concept"
domain: "Healthcare_IT"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 97
categories: ["Healthcare_IT"]
tags: ["语义层", "数字防火墙", "实体哈希"]
created: "2026-04-12"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗MAS架构策略_20260419_Full.md"]
---
# Medical Semantic Layer (MSL)

## 定义
作为医疗系统的“数字防火墙”，强制 LLM 在医学本体和业务规则的约束下运行。

## 性能增强 (2026-04-22 Update)
为了应对 MAS 架构带来的高频 Token 消耗，MSL 引入了 **“实体感知哈希 (Entity-Aware Hashing)”** 技术，强制重用共识并执行记忆脱水[^1]。同时，通过 MCP (模型上下文协议) 联邦，实现了跨异构智能体的一致性语义约束。

[^1]: [[Source_医疗MAS架构策略_20260419_Full.md]]
'
write_wiki_file("Medical Semantic Layer (MSL).md", msl_update)

epic_update = r'''---
id: "20260322_entity_epic_upd"
title: "Entity_Epic_Systems"
type: "entity"
domain: "Healthcare_IT"
topic_cluster: "Strategy"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 90
categories: ["Healthcare_IT"]
tags: ["Epic", "行业霸主", "Agent Factory"]
created: "2026-03-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数字化的重力迁徙_HIMSS2026战略观察.md"]
---
# Entity_Epic_Systems

## 战略地位
医疗 IT 行业的绝对霸主。在 AI 时代，Epic 正在通过 **Agent Factory (智能体工厂)** 和 **Agentic EHR** 实施对医院生产流程的“物理级占领”[^1]。

## 最新动态 (2026-04-22)
HIMSS2026 观察显示，Epic 正在执行“Epic-first”战略，通过将第三方 AI SaaS 挤压至边缘，确立其作为医疗主权 AI 的核心承载平台。

[^1]: [[Source_医疗数字化的重力迁徙_HIMSS2026战略观察.md]]
'
write_wiki_file("Entity_Epic_Systems.md", epic_update)

shawn_shi = r'''---
id: "20260422_entity_shawn_shi"
title: "Shawn Shi"
type: "entity"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
alignment_score: 100
categories: ["Strategy_and_Business"]
tags: ["架构师", "战略专家", "MSL", "逻辑湖"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md"]
---
# Shawn Shi

## 核心定位
医疗数字化转型咨询专家，战略观察者。

## 理论贡献
主导提出了 **MSL (医疗语义层)**、**ACE (智能体协调引擎)**、**逻辑湖 (Logic Lake)** 及 **Evidence-Mesh (证据网)** 等核心理论框架，旨在构建具备物理隔离边界与法理闭环的 [关联:: [[Agentic Hospital (智能体医院)]]]。
'
write_wiki_file("Entity_Shawn_Shi.md", shawn_shi)

overview_content = r'''---
id: 20260422_ov001
updated: '2026-04-22'
---
# Vector Lake 知识全景概览 (Overview)

Vector Lake 当前锚定于解决医疗 IT 与系统架构领域的结构性拐点与抗熵增难题。通过构建纯 Markdown 的多智能体状态机，本知识库重点映射了由核心中枢 Mentat 所指挥的本地自主智能体集群架构。这套架构抛弃了单纯的单体大模型幻想，转而拥抱物理约束、隔离边界与自动化防腐管线，并主张从被动的 RAG 检索向具有物理边界的 Agentic 编译跃迁。

在最新的“十五五”战略重塑期，医疗数字化正经历从“连接”向“重塑”的惊险跨越。通过引入 **MAS (多智能体系统) 医疗架构**、**ACE (智能体协调引擎)** 以及 **逻辑湖 (Logic Lake)**，知识库构建了应对大模型上下文塌陷与注意力衰减的物理屏障。医疗语义层 (MSL) 进一步强化为带有“实体感知哈希”的数字防火墙，确保在高度复杂的 RCM 自动驾驶（Autopilot RCM）博弈中，AI 动作始终挂载于人类数字身份（Identity Threading），维持极高密度的逻辑信噪比。

行业格局上，Epic Systems 等巨头通过“智能体工厂”实施物理级占占领，迫使主权 AI 意识觉醒。Vector Lake 持续通过悲观执行与非对称审计策略，记录顶级专家对系统的修正先验，将“隐性智慧”转化为可增值的逻辑资产。这一演进标志着医疗 IT 已进入利用算法彻底重构生产关系的深水区，任何单点技术的演进都必须服从于整体的系统性重构与法理合规约束。

### 核心主题索引
- **架构重构**: [[Concept_MAS_Medical_Architecture]], [[Concept_Intent_Driven_Router]], [[Concept_Logic_Lake]]
- **业务跃迁**: [[Concept_Agent_RCM]], [[Synthesis_十五五医疗数字化战略]], [[Agentic Hospital (智能体医院)]]
- **防御与合规**: [[Medical Semantic Layer (MSL)]], [[Concept_Identity_Threading]], [[Concept_Evidence_Mesh]]
- **行业节点**: [[Entity_Epic_Systems]], [[Entity_NHC]], [[Entity_Shawn_Shi]]
'
write_wiki_file("overview.md", overview_content)

now_log = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
log_entry = f"## [{now_log}] Batch Ingest | Healthcare MAS & 15th Five-Year Plan Strategy (5 Sources, 10+ Nodes)\n"
append_wiki_file("log.md", log_entry)

print("Batch ingestion completed successfully.")
