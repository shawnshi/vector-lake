import os
import datetime

# Strict directory lock
wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def write_node(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.strip())
    print(f"Written: {filename}")

# --- 1. Source Pages ---

source_1 = r"""
---
id: "20260422_s001"
title: "Source: 医疗机构可信数据空间建设策略与实施路径"
type: "source"
domain: "Medical_IT"
topic_cluster: "Data_Governance"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 365
categories: ["Healthcare_IT"]
tags: ["可信数据空间", "TDS", "数据要素", "隐私计算"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医疗机构可信数据空间建设策略与实施路径.md"]
---

# 医疗机构可信数据空间建设策略与实施路径

## 核心摘要
本文详细探讨了医疗机构构建**可信数据空间 (TDS)** 的战略意义与技术路径。在数据要素化的大背景下，TDS 通过融合隐私计算、区块链、存证溯源等技术，解决医疗数据在流转过程中的信任、安全与确权问题，实现“数据可用不可见、可控可计量”。

## 关键提取
- **核心定义**: 可信数据空间是支撑医疗数据要素安全流转的基础设施，旨在打破数据孤岛，实现合规下的价值释放。
- **技术栈**: 强调了 [支持:: [[Concept_Privacy_Computing]]]、[支持:: [[Concept_Blockchain]]] 以及基于 [支持:: [[Concept_OMOP_CDM]]] 的通用数据模型标准。
- **实施路径**: 提出从内部治理到区域协作，再到跨行业价值发现的三阶段模型。
- **关键概念**: [衍生于:: [[Concept_Data_Product]]] (数据产品) 概念，主张将原始数据加工为标准化的、可交易的服务单元。

## 与 Wiki 联系
- 为 [[Concept_可信数据空间]] 提供了具体的建设策略支撑。
- 强化了 [[Concept_Privacy_Computing]] 在医疗场景下的落地逻辑。
- 补充了关于数据要素市场化背景下医疗机构的防御性响应。
"""

source_2 = r"""
---
id: "20260422_s002"
title: "Source: 医院高质量数字化转型实践"
type: "source"
domain: "Medical_IT"
topic_cluster: "Digital_Transformation"
status: "Active"
alignment_score: 90
epistemic-status: "evergreen"
ttl: 365
categories: ["Healthcare_IT"]
tags: ["高质量发展", "数字化转型", "医院运营", "精细化管理"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/医院高质量数字化转型实践.md"]
---

# 医院高质量数字化转型实践

## 核心摘要
本文聚焦于公立医院在“高质量发展”要求下的数字化转型实务。强调数字化不再是简单的 IT 系统建设，而是管理模式的深度重塑，重点在于运营效率的压榨与临床质量的闭环管理。

## 关键提取
- **转型重心**: 从“信息化建设”转向“数智化运营”。
- **核心矛盾**: 解决业务流程非标与系统标准化之间的冲突 [对比:: [[Concept_Delivery_Debt]]]。
- **管理闭环**: 强调通过 [支持:: [[Concept_Digital_Nervous_System]]] 实现对临床行为的实时感知与干预。
- **案例支撑**: 提及了复星健康等集团化医院在一体化平台建设上的实践。

## 与 Wiki 联系
- 支撑了 [[Concept_Digital_Nervous_System]] 的落地案例。
- 为 [[Concept_Agentic_Hospital]] 的初级阶段（精细化运营）提供了背景说明。
"""

source_3 = r"""
---
id: "20260422_s003"
title: "Source: 十五五期间大型公立医院数字化建设的重点及策略20251119"
type: "source"
domain: "Medical_IT"
topic_cluster: "Strategy"
status: "Active"
alignment_score: 98
epistemic-status: "evergreen"
ttl: 365
categories: ["Strategy_and_Business"]
tags: ["十五五规划", "公立医院", "双轨制", "医保博弈"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/十五五期间大型公立医院数字化建设的重点及策略20251119.md"]
---

# 十五五期间大型公立医院数字化建设的重点及策略20251119

## 核心摘要
本文是对“十五五”期间公立医院数字化战略的深度前瞻。核心论点是医院将面临“双轨制生存”压力：一方面是存量业务的极致精算与控费，另一方面是增量健康资产的运营与转化。

## 关键提取
- **十五五双轨制**:
    1. **轨 A (存量压榨)**: 基于 DRG/DIP 的成本管控，需要 [支持:: [[Concept_Medical_P&L_Engine]]]。
    2. **轨 B (增量运营)**: 区域健康运营、慢病管理与数据要素分红。
- **数字外骨骼**: [衍生于:: [[Concept_AI_Exoskeleton]]], 认为 AI 应辅助医生处理低价值劳动，而非替代决策。
- **数据征用**: 预测政府将加大对医院数据的统筹力度，医院需建立防御性数据主权架构。

## 与 Wiki 联系
- 确立了 [[Strategy_十五五公立医院数字化双轨制生存]] 的战略基调。
- 补充了 [[Concept_AI_Exoskeleton]] 在公立医院语境下的具体定义。
- 提出了对 [[Concept_Medical_P&L_Engine]] (医疗损益引擎) 的紧迫需求。
"""

source_4 = r"""
---
id: "20260422_s004"
title: "Source: 卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12"
type: "source"
domain: "Medical_IT"
topic_cluster: "Winning_Health_Strategy"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 365
categories: ["Strategy_and_Business"]
tags: ["卫宁健康", "Agentic AI", "T2A", "订阅模式"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md"]
---

# 卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12

## 核心摘要
本文分析了卫宁健康如何利用 Agentic AI 重新定义其竞争壁垒。核心逻辑是从“功能堆砌”转向“逻辑调度”，通过 **ACE (智能体协作引擎)** 实现从临床意图到系统动作的 **T2A (Text-to-Action)** 跃迁。

## 关键提取
- **ACE 引擎**: 负责管理多智能体协作，是卫宁 [支持:: [[Concept_Agentic_Hospital]]] 的核心组件。
- **T2A 跃迁**: [支持:: [[Concept_T2A_Text-to-Action]]], 标志着医疗系统从“被动记录”转向“主动执行”。
- **商业模式重构**: 提出 [支持:: [[Concept_Skill_Subscription_Model]]] (Skill 订阅模式)，按 AI 技能的实际产出收费。
- **壁垒定义**: 认为卫宁的终极壁垒是 [支持:: [[Concept_MSL_医疗语义层]]] 的解释权，它是 AI 理解临床逻辑的唯一物理通道。

## 与 Wiki 联系
- 极大地丰富了 [[Entity_Winning_Health]] 的战略节点。
- 为 [[Concept_MSL_医疗语义层]] 提供了“逻辑翻译机”的物理定义。
- 正式引入了 [[Concept_Skill_Subscription_Model]]。
"""

source_5 = r"""
---
id: "20260422_s005"
title: "Source: 卫宁健康：在“云端”窒息，还是在“泥泞”中进化"
type: "source"
domain: "Medical_IT"
topic_cluster: "Winning_Health_Strategy"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 365
categories: ["Strategy_and_Business"]
tags: ["卫宁健康", "WiNEX", "交付债务", "J型曲线"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md"]
---

# 卫宁健康：在“云端”窒息，还是在“泥泞”中进化

## 核心摘要
本文对卫宁健康及 WiNEX 的现状进行了冷酷审计。指出 WiNEX 的云原生架构虽然先进，但在中国公立医院的非标环境中面临剧烈的 **交付债务 (Delivery Debt)** 和 **J型曲线窒息**。

## 关键提取
- **交付债务**: [支持:: [[Concept_Delivery_Debt]]], 指复杂系统在实施过程中的非标成本吞噬利润。
- **J型曲线窒息**: 描述在从旧架构转向新架构（如 HIS 转向 WiNEX）的过程中，医院和厂商由于投入巨大但产出滞后而进入的“窒息期”。
- **进化路径**: 认为卫宁必须通过 [支持:: [[Concept_Digital_Labor]]] (数字劳动力) 来标准化非标交付，而非依赖人力堆砌。

## 与 Wiki 联系
- 为 [[System_WiNEX]] 提供了负先验审计视角。
- 强化了 [[Concept_Delivery_Debt]] 的行业案例。
- 提出了 [[Concept_J_Curve_Suffocation]] 这一关键风险模型。
"""

# --- 2. Concept Pages ---

concept_1 = r"""
---
id: "20260422_c001"
title: "Concept: Skill 订阅模式 (Skill Subscription Model)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Business_Model"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["卫宁健康", "商业模式", "AI 技能", "SaaS"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md"]
---

# Concept: Skill 订阅模式

## 定义
Skill 订阅模式是卫宁健康提出的、面向 Agentic AI 时代的新型商业模式。它不再以软件 License（授权）为核心收费单元，而是将 AI 的具体能力（如：自动病历生成、智能审方、DRG 预测等）打包为独立的 **Skill (技能)**，根据其在医院实际运行的活跃度、有效性或带来的增量收益进行按需订阅。

## 核心逻辑
- **从“拥有”到“服务”**: 医院无需一次性买断系统，而是订阅解决特定问题的“数字能力”。
- **价值对齐**: 收费与 AI 的临床/管理产出挂钩，解决传统医疗 IT “买而不活”的问题。
- **交付解耦**: 基于云原生架构（如 [[System_WiNEX]]），实现技能的分钟级分发与热更新。

## 战略意义
- [支持:: [[Entity_Winning_Health]]] 的收入结构转型，从一次性销售向经常性收入 (ARR) 迁移。
- 降低医院的初始试错成本，提高 AI 落地渗透率。
- 迫使厂商从“写代码”转向“运营效果”，形成质量复利。

## 关联
- [属于:: [[Concept_Agentic_Hospital]]]
- [支持:: [[Concept_Digital_Labor]]]
- [对比:: [[Concept_License_Model]]]
"""

concept_2 = r"""
---
id: "20260422_c002"
title: "Concept: T2A (Text-to-Action)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Artificial_Intelligence"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Agentic AI", "自动化", "临床闭环", "卫宁健康"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md"]
---

# Concept: T2A (Text-to-Action)

## 定义
T2A 指医疗系统从理解**自然语言意图 (Text)** 直接跃迁到**执行系统动作 (Action)** 的过程。它是 Agentic AI 在医疗领域落地的核心能力，标志着系统从“记录器”向“执行器”的进化。

## 物理实现路径
1. **意图捕获**: 医生通过语音或文字输入临床意图。
2. **语义对齐**: 通过 [[Concept_MSL_医疗语义层]] 将自然语言翻译为结构化的医疗逻辑。
3. **协作调度**: 由 [[Concept_ACE_智能体协作引擎]] 分配任务给不同的功能 Agent。
4. **动作闭环**: 调用 API 执行开立医嘱、修改病历、预约检查等真实系统操作。

## 核心挑战
- **安全红线**: 必须具备 99.9% 的确定性，防止 AI 产生误动作。通常需要 [支持:: [[Concept_Human-on-the-Loop]]] 的强制核对机制。
- **孤岛突破**: 需要底层系统（如 HIS/EMR）具备深度的 API 开放性。

## 战略价值
- 彻底消除 [支持:: [[Concept_表单监狱]]], 让医生回归临床。
- 实现真正的“临床闭环”，将意图转化为可审计的系统行为。

## 关联
- [属于:: [[Concept_Agentic_Hospital]]]
- [衍生于:: [[Concept_MSL_医疗语义层]]]
- [支持:: [[Concept_ACE_智能体协作引擎]]]
"""

concept_3 = r"""
---
id: "20260422_c003"
title: "Concept: 医疗损益精算引擎 (Medical P&L Engine)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Hospital_Management"
status: "Active"
alignment_score: 90
epistemic-status: "seed"
ttl: 1825
categories: ["Healthcare_IT"]
tags: ["DRG", "DIP", "成本管控", "十五五规划"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/十五五期间大型公立医院数字化建设的重点及策略20251119.md"]
---

# Concept: 医疗损益精算引擎

## 定义
一种集成于临床与运营流程中的实时计算模型，旨在应对 DRG/DIP 控费铁幕。它通过对每一张医嘱、每一个诊疗路径进行实时的成本-收益测算，实现“在治疗过程中控费”，而非事后分析。

## 核心逻辑
- **实时性**: 在医生开立医嘱时，立即反馈当前的 DRG 额度占用与预估损益。
- **路径优化**: 推荐既符合临床质控要求又具备成本效益的诊疗路径。
- **利益对齐**: 将科室绩效、医生收益与精算结果挂钩，实现 [支持:: [[Strategy_十五五公立医院数字化双轨制生存]]] 中的存量压榨。

## 战略背景
在“十五五”期间，公立医院从“营收中心”转为“成本中心”。该引擎是医院在极度紧缩环境下维持财务健康的物理防线。

## 关联
- [支持:: [[Strategy_十五五公立医院数字化双轨制生存]]]
- [属于:: [[Concept_数字化神经系统]]]
- [对抗:: [[Concept_Defensive_Medicine]]] (防御性医疗带来的成本溢出)
"""

concept_4 = r"""
---
id: "20260422_c004"
title: "Concept: J型曲线窒息 (J-Curve Suffocation)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Digital_Transformation"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["数字化风险", "卫宁健康", "架构转型", "交付危机"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md"]
---

# Concept: J型曲线窒息

## 定义
描述医疗 IT 厂商或医院在推行跨代架构转型（如从单体 HIS 转向云原生平台）时，面临的一段高投入、低产出且伴随剧烈摩擦的“死亡谷”阶段。因其在时间轴上的表现形似 J 字母的底部弯曲部分，故名。

## 产生特征
1. **投入激增**: 研发成本（新旧并行）与交付成本（学习曲线）达到峰值。
2. **产出停滞**: 由于新系统复杂度高，初期交付速度反而慢于旧系统。
3. **组织排异**: 一线实施人员和客户由于习惯路径被打破，产生剧烈的认知冲突与抵触。

## 典型案例
- [支持:: [[Entity_Winning_Health]]] 推行 [[System_WiNEX]] 过程中，由于非标场景过多，导致交付周期被极度拉长，毛利大幅下降，即为“在泥泞中进化”的典型。

## 防御策略
- [支持:: [[Concept_Clean_Room_Refactoring]]] 减少历史包袱。
- 引入 [[Concept_Digital_Labor]] 自动化替代重复性配置人力。

## 关联
- [衍生于:: [[Concept_Delivery_Debt]]]
- [对比:: [[Concept_Strangler_Fig_Pattern]]] (用于规避窒息的渐进模式)
"""

concept_6 = r"""
---
id: "20260422_c006"
title: "Concept: ACE (智能体协作引擎 / Agentic Collaboration Engine)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Artificial_Intelligence"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["多智能体", "调度引擎", "卫宁健康", "T2A"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md"]
---

# Concept: ACE (智能体协作引擎)

## 定义
ACE 是卫宁健康 Agentic AI 架构中的核心指挥中枢。它负责协调多个专病、专岗智能体（Agents）之间的通讯、任务分配与逻辑校验，是实现 [支持:: [[Concept_T2A_Text-to-Action]]] 的关键执行底座。

## 核心职责
- **任务分解**: 将复杂的临床意图分解为可执行的子任务。
- **冲突裁决**: 当不同智能体给出的建议冲突时，依据医学权威指南执行逻辑仲裁。
- **状态维持**: 跟踪长链路临床任务的执行进度与安全状态。

## 地位
它是 [支持:: [[Concept_Agentic_Hospital]]] 能够像“医院大脑”一样运作的技术前提。

## 关联
- [属于:: [[Concept_Agentic_Hospital]]]
- [支持:: [[Concept_T2A_Text-to-Action]]]
- [调用:: [[Concept_MSL_医疗语义层]]]
"""

# --- 3. Update Existing Entities/Systems/Concepts ---

entity_winning = r"""
---
id: "20260422_e001"
title: "Entity: 卫宁健康 (Winning Health)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Industry_Leaders"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1095
categories: ["Strategy_and_Business", "Healthcare_IT"]
tags: ["上市公司", "300253", "Agentic AI", "WiNEX"]
created: "2026-04-10"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md", "raw/article/digitalhealthobserve/卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md"]
---

# Entity: 卫宁健康 (Winning Health)

## 战略定位
卫宁健康正处于从传统的“医疗软件开发商”向“**医疗智能操作系统服务商**”转型的关键期。其核心战略是利用 **Agentic AI** 与 **云原生架构** 重新定义医疗 IT 的竞争壁垒。

## 核心资产与能力
- **底座系统**: [[System_WiNEX]] (云原生平台)。
- **AI 引擎**: [[System_WiNGPT]] (医疗大模型) 与 [[Concept_ACE_智能体协作引擎]]。
- **语义主权**: 掌握 [[Concept_MSL_医疗语义层]]，作为临床意图的“逻辑翻译机” [^1]。

## 竞争挑战
- **交付债务**: 在非标交付中面临剧烈的 [支持:: [[Concept_Delivery_Debt]]], 导致利润承压 [^2]。
- **J型曲线**: 处于架构转型带来的 [支持:: [[Concept_J_Curve_Suffocation]]] 期。

## 商业模式演进
- 积极推行 [支持:: [[Concept_Skill_Subscription_Model]]] (Skill 订阅制)，试图突破 License 模式的增长瓶颈。

[^1]: [[Source_卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md]]
[^2]: [[Source_卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md]]
"""

system_winex = r"""
---
id: "20260422_sys01"
title: "System: WiNEX"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Product_Platforms"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1095
categories: ["Healthcare_IT", "System_Architecture"]
tags: ["云原生", "微服务", "卫宁健康", "中台"]
created: "2026-04-10"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md"]
---

# System: WiNEX

## 系统定位
卫宁健康推出的新一代云原生医疗数字化平台。采用微服务、中台化架构，旨在实现医疗业务的快速迭代与弹性伸缩。

## 架构演进与风险
- **先进性**: 物理架构领先，但在公立医院非标流程中产生 [支持:: [[Concept_Delivery_Debt]]]。
- **窒息风险**: 处于转型期的 [支持:: [[Concept_J_Curve_Suffocation]]], 实施周期长、成本高是当前核心痛点 [^1]。
- **进化点**: 开始整合 [[Concept_ACE_智能体协作引擎]]，推动从“云端中台”向“智能体底座”的二次跳跃。

## 核心矛盾
WiNEX 的“高贵血统（标准化、模块化）”与中国医院“泥泞现实（高非标、强耦合）”之间的冲突。

[^1]: [[Source_卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md]]
"""

concept_msl = r"""
---
id: "20260422_c005"
title: "Concept: MSL (医疗语义层 / Medical Semantic Layer)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence", "Healthcare_IT"]
tags: ["语义主权", "卫宁健康", "临床逻辑", "T2A"]
created: "2026-04-12"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md"]
---

# Concept: MSL (医疗语义层)

## 定义
MSL 是位于原始医疗数据与 AI 应用之间的**逻辑解释层**。它通过对临床场景、医学术语、诊疗规范的深层建模，为 AI 提供了一套统一的“医疗母语”，从而实现从统计幻觉向物理逻辑的对齐。

## 战略主权
卫宁健康认为，MSL 是医疗 IT 厂商在 Agentic AI 时代的**终极壁垒**。谁掌握了 MSL 的定义权，谁就掌握了医疗逻辑的“最终解释权”和 [支持:: [[Concept_T2A_Text-to-Action]]] 的物理通道 [^1]。

## 功能定位
- **逻辑翻译机**: 将模糊的自然语言指令翻译为精确的系统动作。
- **事实获取权**: [支持:: [[Concept_事实获取权]]], 确保 AI 能够从底层复杂的数据库中提取到正确的临床语义。

[^1]: [[Source_卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md]]
"""

# --- 4. Global Overlays ---

overview_content = r"""
---
id: "20260422_ov012" 
updated: "2026-04-22" 
---
# Vector Lake 知识全景概览 (Overview)

Vector Lake 正在经历从“生成式 AI”向“代理式 AI (Agentic AI)”与**认知工业化 (Cognitive Industrialization)** 的双重范式重组。我们的核心战略已演进为**架构包围算法**：通过构建具备刚性约束力的 **[[Concept_MSL_医疗语义层.md]]**、**[[Concept_Evidence_Mesh.md]]** 及 Teacher-Student 蒸馏架构，来对冲 AI 幻觉并保卫智力主权。

近期，**[[Entity_Winning_Health.md]]** 与 **[[Entity_WiNEX.md]]** 的深度演进显示，医疗 IT 的核心壁垒已转向 **[[Concept_T2A_Text-to-Action.md]]** 能力与 **[[Concept_Skill_Subscription_Model.md]]** 商业模式。在“十五五”规划的高压下，公立医院正被迫执行 **[[Strategy_十五五公立医院数字化双轨制生存.md]]**，将数字化从信息化升级转变为生存基因工程，旨在建立以 **[[Concept_Medical_P&L_Engine.md]]** 为核心的极致精算体系。

在基础设施层面，**[[Concept_可信数据空间.md]]** (TDS) 结合 **[[Entity_OHDSI.md]]** 的 **[[Concept_OMOP_CDM.md]]** 标准，正在破除数据流转的信任瓶颈。面对转型中的 **[[Concept_J_Curve_Suffocation.md]]** (J型曲线窒息)，引入 **[[Concept_Digital_Labor.md]]** (数字劳动力) 已成为降低非标场景交付成本、实现从软件商向“收税官”转型的关键路径。

在行业大势上，中国医疗 IT 正处于从 HIS/EMR 时代的“扫盲式数字化”向以价值医疗（VBH）为导向的“制度深潜”跨越。这一转变催生了“数字化神经系统”这一核心概念，旨在通过重构激励罗盘来实现临床与运营的实时闭环管控。
"""

# Execute Writes
write_node("Source_医疗机构可信数据空间建设策略与实施路径.md", source_1)
write_node("Source_医院高质量数字化转型实践.md", source_2)
write_node("Source_十五五期间大型公立医院数字化建设的重点及策略20251119.md", source_3)
write_node("Source_卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md", source_4)
write_node("Source_卫宁健康：在“云端”窒息，还是在“泥泞”中进化.md", source_5)
write_node("Concept_Skill_Subscription_Model.md", concept_1)
write_node("Concept_T2A_Text-to-Action.md", concept_2)
write_node("Concept_Medical_P&L_Engine.md", concept_3)
write_node("Concept_J_Curve_Suffocation.md", concept_4)
write_node("Concept_ACE_智能体协作引擎.md", concept_6)
write_node("Entity_Winning_Health.md", entity_winning)
write_node("System_WiNEX.md", system_winex)
write_node("Concept_MSL_医疗语义层.md", concept_msl)
write_node("overview.md", overview_content)

# Append to log.md
log_path = os.path.join(wiki_dir, "log.md")
with open(log_path, 'a', encoding='utf-8') as f:
    f.write(f"## [{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}] Ingest Batch | 5 Sources on Winning Health & Medical IT Strategy\n")

print("All nodes written successfully.")
