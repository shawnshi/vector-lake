import os
import datetime

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def write_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.strip() + '\n')

def append_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n' + content.strip() + '\n')

def read_file(filename):
    path = os.path.join(wiki_dir, filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
date_str = "2026-04-19"

source_1 = f"""---
id: \"20260419_src1\"
title: \"研究：1-bit LLM 与边缘计算小脑化\"
type: \"source\"
domain: \"Artificial_Intelligence\"
topic_cluster: \"Edge_Computing\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Artificial_Intelligence\", \"System_Architecture\"]
tags: [\"1-bit LLM\", \"小脑化\", \"端侧部署\", \"数据主权\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_1bit_llm_cerebellarization_20260419.md\"]
---
# 研究：1-bit LLM 与边缘计算小脑化

## 核心主旨
探讨 1-bit LLM (如 BitNet b1.58) 如何通过极低能耗实现医疗 AI 的端侧部署，进而提出“边缘计算小脑化”概念。

## 关键实体与概念
- **[支持:: [[Concept_1_bit_LLM]]]**
- **[衍生于:: [[Concept_Cerebellarization]]]**

## 核心论点
1-bit LLM 的出现不仅是降低计算成本，其核心战略意义是赋予基层医院与医疗设备“小脑化”的本地计算能力，构筑了对抗云端算力霸权与数据剥夺的“物理避险策略”。
"""

source_2 = f"""---
id: \"20260419_src2\"
title: \"研究：双模态认知架构\"
type: \"source\"
domain: \"System_Architecture\"
topic_cluster: \"AI_Safety\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\", \"Philosophy_and_Cognitive\"]
tags: [\"双模态\", \"System 1\", \"System 2\", \"防幻觉\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_bimodal_cognition_architecture_20260419.md\"]
---
# 研究：双模态认知架构

## 核心主旨
提出模仿人类 System 1（感知/模式识别）和 System 2（推理/规则校验）的双模态认知架构，用于对抗大模型幻觉。

## 关键实体与概念
- **[衍生于:: [[Concept_Bimodal_Cognition_Architecture]]]**

## 核心论点
为了合法穿透监管壁垒并解决幻觉，现代医疗 AI 必须重构底层。通过双模态/双轨制架构建立证据网 (Evidence-Mesh)，强行提供白盒化的临床逻辑推演路径，物理隔离生成与验证。
"""

source_3 = f"""---
id: \"20260419_src3\"
title: \"研究：CPIC 与药代动力学 AI\"
type: \"source\"
domain: \"Biomedicine\"
topic_cluster: \"Pharmacogenomics\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Biomedicine\", \"Healthcare_IT\"]
tags: [\"CPIC\", \"Sherpa Rx\", \"RAG\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_cpic_pharmacogenomics_ai_20260419.md\"]
---
# 研究：CPIC 与药代动力学 AI

## 核心主旨
分析基于 RAG 架构的药代动力学 AI 助手 Sherpa Rx 如何利用 CPIC 指南实现防幻觉校验。

## 关键实体与概念
- **[支持:: [[Entity_CPIC]]]**
- **[涉及:: [[System_Sherpa_Rx]]]**

## 核心论点
通过 RAG 架构锚定真实指南（如 CPIC），将 AI 退位为“支持检索与辅助”，最终解释权归属人类，以此符合高风险临床决策的严苛要求。
"""

source_4 = f"""---
id: \"20260419_src4\"
title: \"研究：双轨制架构与语义损失\"
type: \"source\"
domain: \"System_Architecture\"
topic_cluster: \"AI_Safety\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\", \"Healthcare_IT\"]
tags: [\"双轨制\", \"语义损失\", \"MSL\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_dual_track_architecture_semantic_loss_20260419.md\"]
---
# 研究：双轨制架构与语义损失

## 核心主旨
探讨大模型在处理复杂医疗上下文时发生的“语义损失”现象，以及双轨制架构如何通过 Medical Semantic Layer (MSL) 确保语义同态守恒。

## 关键实体与概念
- **[探讨:: [[Concept_Semantic_Loss]]]**
- **[解决方案:: [[Concept_Bimodal_Cognition_Architecture]]]**

## 核心论点
大模型处理复杂上下文或多模态时极易发生“标识符丢失”或“结构化坍塌”（语义损失），在医疗场景下这可能导致致命误诊。双轨制架构利用“快速轨”与“慢速轨”并行，确保转化过程中的绝对同态。
"""

source_5 = f"""---
id: \"20260419_src5\"
title: \"研究：FDA CDS 监管与 Epic 的解耦策略\"
type: \"source\"
domain: \"Strategy_and_Business\"
topic_cluster: \"Compliance\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\", \"Healthcare_IT\"]
tags: [\"FDA\", \"CDS\", \"Epic Systems\", \"责任漂移\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_fda_cds_regulation_epic_systems_20260419.md\"]
---
# 研究：FDA CDS 监管与 Epic 的解耦策略

## 核心主旨
分析 FDA 的“四点测试”对临床决策支持系统 (CDS) 的监管边界，以及 Epic Systems 如何通过“框架与内容解耦”策略规避医疗器械监管，导致医院面临责任漂移。

## 关键实体与概念
- **[涉及:: [[Entity_FDA]]]**
- **[涉及:: [[Concept_FDA_Non_Device_CDS_Test]]]**
- **[探讨:: [[Entity_Epic_Systems]]]**
- **[导致:: [[Concept_Liability_Shift_in_Healthcare_AI]]]**

## 核心论点
FDA 的“四点测试”实质上是对算法透明度的硬性强制，否决了纯黑盒 LLM 在高风险临床决策中的直接应用。EHR 巨头（如 Epic）的“引擎与内容解耦”策略将高风险的临床逻辑重担抛给了医疗机构，引发“责任漂移”。
"""

concept_cer = f"""---
id: \"20260419_c_cer\"
title: \"边缘计算小脑化 (Cerebellarization)\" 
type: \"concept\"
domain: \"System_Architecture\"
topic_cluster: \"Edge_Computing\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\", \"Artificial_Intelligence\"]
tags: [\"边缘计算\", \"端侧智能\", \"物理避险\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_1bit_llm_cerebellarization_20260419.md\"]
---
# 边缘计算小脑化 (Cerebellarization)

## 概念定义
将复杂逻辑下沉为高频、低延迟的端侧反射弧的系统架构模式。

## 战略价值
通过将智能部署在边缘设备上，实现断网可用的物理避险，保障临床安全与医疗语义主权。这是对抗云端算力霸权的核心武器，[支持:: [[Concept_1_bit_LLM]]] 的发展为其提供了重要的底层技术支撑。
"""

concept_bimodal = f"""---
id: \"20260419_c_bimodal\"
title: \"双模态认知架构 (Bimodal Cognition Architecture)\" 
type: \"concept\"
domain: \"System_Architecture\"
topic_cluster: \"AI_Safety\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\", \"Philosophy_and_Cognitive\"]
tags: [\"双模态\", \"双轨制\", \"System 1\", \"System 2\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_bimodal_cognition_architecture_20260419.md\", \"raw/research/research_dual_track_architecture_semantic_loss_20260419.md\"]
---
# 双模态认知架构 (Bimodal Cognition Architecture)

## 概念定义
模仿人类 System 1（感知/模式识别，快速轨）和 System 2（推理/规则校验，慢速轨）的双路径认知与执行架构。

## 战略价值
物理隔离生成与验证，利用 Medical Semantic Layer (MSL) 确保转化过程中语义的绝对同态与守恒。该架构能够有效对抗大模型幻觉与 [防御:: [[Concept_Semantic_Loss]]]，是穿透严格监管壁垒（如 FDA CDS 标准）的核心工程底座。
"""

concept_sem_loss = f"""---
id: \"20260419_c_sem_loss\"
title: \"语义损失 (Semantic Loss)\" 
type: \"concept\"
domain: \"Philosophy_and_Cognitive\"
topic_cluster: \"AI_Safety\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Philosophy_and_Cognitive\", \"Artificial_Intelligence\"]
tags: [\"幻觉\", \"信息坍塌\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_dual_track_architecture_semantic_loss_20260419.md\"]
---
# 语义损失 (Semantic Loss)

## 概念定义
大模型在处理复杂上下文或跨模态转换时发生的“标识符丢失”或“结构化坍塌”现象。

## 医疗场景风险
大模型所追求的“液态、概率性生成”与临床医学要求的“固态、确定性因果”存在天然张力。在医疗场景下，语义损失极易导致致命误诊，必须通过 [解决方案:: [[Concept_Bimodal_Cognition_Architecture]]] 等机制予以防范。
"""

concept_liability = f"""---
id: \"20260419_c_liability\"
title: \"医疗 AI 中的责任漂移 (Liability Shift)\" 
type: \"concept\"
domain: \"Strategy_and_Business\"
topic_cluster: \"Compliance\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\", \"Healthcare_IT\"]
tags: [\"责任漂移\", \"合规风险\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_fda_cds_regulation_epic_systems_20260419.md\"]
---
# 医疗 AI 中的责任漂移 (Liability Shift)

## 概念定义
指底层系统供应商或平台（如 EHR 厂商）通过架构解耦，将高风险的业务逻辑配置权与终极合规责任转移给终端使用机构（如医院）的现象。

## 典型案例与影响
[实践者:: [[Entity_Epic_Systems]]] 通过“框架与内容解耦”来规避 FDA 的器械监管。这导致医院若盲目使用黑盒 AI 功能，将被迫承担类似于“医疗器械制造商”的终极合规责任。医院在享受 AI 便利与承担器械合规风险间进退维谷。
"""

concept_fda = f"""---
id: \"20260419_c_fda_cds\"
title: \"FDA CDS 四点测试 (FDA Non-Device CDS Test)\" 
type: \"concept\"
domain: \"Strategy_and_Business\"
topic_cluster: \"Compliance\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\", \"Healthcare_IT\"]
tags: [\"FDA\", \"CDS\", \"合规监管\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_fda_cds_regulation_epic_systems_20260419.md\"]
---
# FDA CDS 四点测试 (FDA Non-Device CDS Test)

## 概念定义
[制定者:: [[Entity_FDA]]] 用于界定非医疗器械类临床决策支持 (Non-Device CDS) 边界的法定测试标准。

## 核心内涵
其中最关键的是“独立审查（Transparency）”法则，在法理上直接否决了纯黑盒 AI 在高风险临床决策中的应用。这要求算法黑箱必须向透明度让步，AI 只能退位为“支持检索与辅助”，最终解释权归属人类（Human-in-the-Loop）。
"""

entity_sherpa = f"""---
id: \"20260419_e_sherpa\"
title: \"Sherpa Rx\"
type: \"system\"
domain: \"Healthcare_IT\"
topic_cluster: \"AI_Systems\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Healthcare_IT\", \"Biomedicine\"]
tags: [\"RAG\", \"药代动力学\", \"CDS\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_cpic_pharmacogenomics_ai_20260419.md\"]
---
# Sherpa Rx

## 核心定位
采用 RAG 架构的药代动力学 AI 助手代表系统。

## 核心功能
主要通过实时查询 [依赖:: [[Entity_CPIC]]] 等标准指南，构建防御性证据网。通过锚定真实指南进行防幻觉校验，体现了高风险临床决策对确定性因果的要求。
"""

entity_fda = f"""---
id: \"20260419_e_fda\"
title: \"FDA (美国食品药品监督管理局)\" 
type: \"entity\"
domain: \"Strategy_and_Business\"
topic_cluster: \"Regulation\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\", \"Biomedicine\"]
tags: [\"监管机构\", \"CDS\", \"SaMD\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_fda_cds_regulation_epic_systems_20260419.md\"]
---
# FDA (美国食品药品监督管理局)

## 核心定位
制定医疗器械（含 SaMD）与临床决策支持 (CDS) 系统监管边界的核心监管机构。

## 影响与政策
其发布的 [颁布:: [[Concept_FDA_Non_Device_CDS_Test]]] 对行业内 AI 产品的架构设计与合规策略产生了决定性影响，强制要求算法透明度。
"""

llm_content = read_file("Concept_1_bit_LLM.md")
if llm_content:
    if "边缘计算小脑化" not in llm_content:
        llm_content += f"""
## 物理避险与边缘小脑化 (2026-04-19 增补)
1-bit LLM (如 BitNet b1.58) 的实战价值不仅在于降低能耗，其核心战略意义是赋予基层医院与医疗设备“小脑化”的本地计算能力（[关联:: [[Concept_Cerebellarization]]]）。这为构建对抗云端算力霸权与数据剥夺的端侧防线提供了物理避险策略。
"""
        write_file("Concept_1_bit_LLM.md", llm_content)
else:
    llm_new = f"""---
id: \"20260419_c_1bit\"
title: \"1-bit LLM\"
type: \"concept\"
domain: \"Artificial_Intelligence\"
topic_cluster: \"Edge_Computing\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Artificial_Intelligence\"]
tags: [\"LLM\", \"端侧部署\", \"BitNet\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_1bit_llm_cerebellarization_20260419.md\"]
---
# 1-bit LLM

## 核心定位
采用极低比特（如三值权重 {{ -1, 0, 1 }}）量化的超轻量级大语言模型。

## 战略意义
极大地降低了模型的内存足迹与推理能耗。其核心战略意义是赋予基层医院与医疗设备本地计算能力（[衍生于:: [[Concept_Cerebellarization]]]），构筑物理避险防线。
"""
    write_file("Concept_1_bit_LLM.md", llm_new)

cpic_content = read_file("Entity_CPIC.md")
if cpic_content:
    if "Sherpa Rx" not in cpic_content:
        cpic_content += f"""
## AI 合规与防幻觉校验 (2026-04-19 增补)
在现代基于 RAG 架构的药代动力学 AI 助手（如 [关联:: [[System_Sherpa_Rx]]]）中，CPIC 指南作为外围实体扮演了关键的 Ground Truth 校验角色。锚定 CPIC 数据是系统实现防幻觉、符合高风险临床决策要求的重要保障。
"""
        write_file("Entity_CPIC.md", cpic_content)
else:
    cpic_new = f"""---
id: \"20260419_e_cpic\"
title: \"CPIC (临床药物遗传学实施联盟)\" 
type: \"entity\"
domain: \"Biomedicine\"
topic_cluster: \"Pharmacogenomics\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"Biomedicine\"]
tags: [\"指南\", \"药物代谢\", \"Ground Truth\"]
created: \"{date_str}\" 
updated: \"{date_str}\" 
sources: [\"raw/research/research_cpic_pharmacogenomics_ai_20260419.md\"]
---
# CPIC (临床药物遗传学实施联盟)

## 核心定位
提供基因与药物代谢关联规则的权威 Ground Truth 知识库。

## AI 合规与防幻觉校验
在基于 RAG 架构的药代动力学 AI 助手（如 [关联:: [[System_Sherpa_Rx]]]）中扮演关键校验角色。
"""
    write_file("Entity_CPIC.md", cpic_new)

epic_systems_content = read_file("Entity_Epic_Systems.md")
if epic_systems_content:
    if "责任漂移" not in epic_systems_content:
        epic_systems_content += f"""
## 架构合规与责任漂移 (2026-04-19 增补)
面对 [关联:: [[Entity_FDA]]] 的 CDS 严苛监管，Epic 采取了“框架与内容解耦”的避险策略。这种策略虽然保全了厂商自身，但将高风险的临床逻辑重担抛给了医疗机构，引发了系统性的 [导致:: [[Concept_Liability_Shift_in_Healthcare_AI]]] 问题。
"""
        write_file("Entity_Epic_Systems.md", epic_systems_content)

overview_new = f"""---
id: \"20260419_overview\"
title: \"Vector Lake Overview\"
type: \"synthesis\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\"]
tags: [\"全局概览\", \"医疗IT\"]
created: \"2026-04-19\"
updated: \"{date_str}\" 
sources: []
---
# Vector Lake Overview

Vector Lake (V7.2) 是一座以医疗信息化 (HIT) 为主轴、兼顾战略架构与 AI 基础设施的深层认知网络。本知识库旨在通过结构化压缩行业变量，揭示医疗 IT 市场的结构性拐点（如 DRG/DIP 控费驱动下的系统重构），并为 AI Agent 在医疗场景的临床决策与院内运营落地提供高维推演支撑。

当前，医疗 IT 正经历底层的物理重构与监管合规的碰撞。国际市场上，以 Epic Systems 为代表的巨头正利用数据壁垒构建“生态霸权与语义垄断”；同时面对 FDA 严苛的 CDS 四点测试标准，其采取了“框架与内容解耦”的策略，直接导致了向医疗机构的“责任漂移”。而在对抗黑盒算法带来的语义损失问题上，行业加速摒弃纯粹的端到端生成范式，转向探索 [包含:: [[Concept_Bimodal_Cognition_Architecture]]] 与严格的双轨制防护，利用 RAG 架构锚定 CPIC 等权威指南，将大模型的角色退位为辅助推理引擎。

与此同时，算力的下沉正孕育新的防线。1-bit LLM 的崛起不仅破解了能耗瓶颈，更催生了“边缘计算小脑化”的战略构想。这使得将高频、低延迟的反射弧沉淀到端侧设备成为可能，构筑了对抗云端算力霸权与捍卫医疗数据主权的坚固避险策略。国内市场在经历资本塌缩与交付债务的残酷洗牌后，传统的宏大叙事已让位于极简的“脱水架构”，这要求厂商必须提供具备极短 ROI 的高确定性组件。

伴随技术的演进，知识库持续关注组织、人才与涌现性冲突。从医疗大模型、智能体编排 (Agentic Workflows) 的普及，到它在真实医疗质量管理下引发的“审计反转”，全能诊断神话已经破灭，行业正全面向“防御型风控智能体”转型。Vector Lake 坚持悲观执行与非对称审计策略，致力于为头部企业构筑牢不可破的逻辑底座。
"""
write_file("overview.md", overview_new)

write_file("Source_research_1bit_llm_cerebellarization_20260419.md", source_1)
write_file("Source_research_bimodal_cognition_architecture_20260419.md", source_2)
write_file("Source_research_cpic_pharmacogenomics_ai_20260419.md", source_3)
write_file("Source_research_dual_track_architecture_semantic_loss_20260419.md", source_4)
write_file("Source_research_fda_cds_regulation_epic_systems_20260419.md", source_5)
write_file("Concept_Cerebellarization.md", concept_cer)
write_file("Concept_Bimodal_Cognition_Architecture.md", concept_bimodal)
write_file("Concept_Semantic_Loss.md", concept_sem_loss)
write_file("Concept_Liability_Shift_in_Healthcare_AI.md", concept_liability)
write_file("Concept_FDA_Non_Device_CDS_Test.md", concept_fda)
write_file("System_Sherpa_Rx.md", entity_sherpa)
write_file("Entity_FDA.md", entity_fda)

append_file("log.md", f"## [{now}] Ingest | 5 Research Documents (1-bit LLM, Bimodal, CPIC, Semantic Loss, FDA/Epic)")

print("Write complete.")
