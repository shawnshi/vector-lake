import os
import datetime
import re

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def read_file(filename):
    path = os.path.join(wiki_dir, filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

def write_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.strip() + '\n')

def append_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n' + content.strip() + '\n')

def update_or_create_source(filename, title, yaml_sources, insights, category):
    content = read_file(filename)
    date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    if content:
        # Append insights
        content += f"\n\n## {date_str} 战略审计增补\n" + insights.strip()
        write_file(filename, content)
    else:
        # Create new
        id_str = datetime.datetime.now().strftime('%Y%m%d') + "_srcnew"
        new_content = f"""
---
id: "{id_str}"
title: "{title}"
type: "source"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
categories: ["{category}"]
tags: ["2026", "Audit"]
created: "{date_str}"
updated: "{date_str}"
sources: {yaml_sources}
---
# {title}

## 核心主旨
{insights.strip()}
"""
        write_file(filename, new_content)

def update_or_create_concept(filename, title, insights, category):
    content = read_file(filename)
    date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    if content:
        # Append insights
        content += f"\n\n## {date_str} 审计增补\n" + insights.strip()
        write_file(filename, content)
    else:
        # Create new
        id_str = datetime.datetime.now().strftime('%Y%m%d') + "_connew"
        new_content = f"""
---
id: "{id_str}"
title: "{title}"
type: "concept"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
categories: ["{category}"]
tags: ["2026", "Architecture"]
created: "{date_str}"
updated: "{date_str}"
sources: []
---
# {title}

## 核心内涵
{insights.strip()}
"""
        write_file(filename, new_content)

# Source: 2026医疗数字化：一场剥离幻觉的冷酷清算_20260419.md
update_or_create_source(
    "Source_2026医疗数字化：一场剥离幻觉的冷酷清算_20260419.md",
    "2026医疗数字化：一场剥离幻觉的冷酷清算",
    '["raw/article/digitalhealthobserve/2026医疗数字化：一场剥离幻觉的冷酷清算_20260419.md"]',
    "医疗数字化的冷酷清算：2026 年医疗 IT 从“颠覆性创新”全面退防至“物理合规围栏”。技术不再是创造增量的造梦机，而是以降低成本和满足 DRG/DIP 控费限制为核心的“止血工具”。\n临床解释权的转移与算力寡头游戏：99% 的 AI 沦为打杂苦力，而 1% 掌握真实数据与顶级算力的寡头通过“数字孪生”垄断了临床生命科学的试错权。\n关联实体：[反面案例:: [[Entity_Pear_Therapeutics]]]，[支持:: [[Concept_1-bit_LLM]]]，[应用:: [[Concept_Ambient_AI_Scribes]]]。",
    "Strategy_and_Business"
)

# Source: 2026-Q2.md
update_or_create_source(
    "Source_2026-Q2.md",
    "个人日志 2026-Q2",
    '["raw/privacy/Diary/2026-Q2.md"]',
    "Mentat 高强度执行中发现，系统的鲁棒性不取决于大模型自身的智能，而是取决于“底层物理硬锁”、“Win32 Payload 降维”和剥离幻觉的四层壳架构。\n记录了 Vector Lake V7.2 的架构升级（解决并发锁、引入 Louvain 拓扑）。",
    "Philosophy_and_Cognitive"
)

# Source: 2026-Q2_Audit.md
update_or_create_source(
    "Source_2026-Q2_Audit.md",
    "Mentat 审计日志 2026-Q2",
    '["raw/privacy/Diary/mentat_audit/2026-Q2_Audit.md"]',
    "静默降级掩盖失败 (Silent Degradation Masking Failure)：代码中过度“优雅”的后备机制导致底层致命断层（如文件丢失）被掩盖。要求全面转向“Fail-Fast（快速失败）”与绝对物理路径硬锁。\n关联概念：[防范:: [[Concept_Silent_Degradation_Masking_Failure]]]",
    "System_Architecture"
)

# Source: 20260419_Strategic_Audit_7d.md
update_or_create_source(
    "Source_20260419_Strategic_Audit_7d.md",
    "7天战略审计报告",
    '["raw/personal-insights/20260419_Strategic_Audit_7d.md"]',
    "暗负载 / 认知摩擦 (Shadow Load)：纯精神或认知上的高强度消耗，即使物理消耗极低，也会导致系统性皮质醇淤积与神经疲劳。通过读取 [关联:: [[System_Garmin]]] 的底层 JSON 数据对抗“Shadow Load”，实现在指挥官处于睡眠负债与高压时，强制阻断高维系统重构指令。\n关联概念：[监控:: [[Concept_Shadow_Load]]]",
    "Philosophy_and_Cognitive"
)

# Concept_1-bit_LLM.md
update_or_create_concept(
    "Concept_1-bit_LLM.md",
    "1-bit LLM (1-bit 小脑化)",
    "在面临芯片禁令与数据合规出境双重封锁下，医疗 AI 唯一的物理突围路径。放弃云端庞大算力，将大模型压缩至 1-bit 级并硬核植入边缘设备（如移动查房车、超声终端），兼顾绝对合规与超低延迟。\n来源：[[Source_2026医疗数字化：一场剥离幻觉的冷酷清算_20260419.md]]",
    "Artificial_Intelligence"
)

# Concept_Ambient_AI_Scribes.md
update_or_create_concept(
    "Concept_Ambient_AI_Scribes.md",
    "环境智能与 HUD 隐喻 (Ambient AI Scribes)",
    "隐式运行在诊室后台的守护进程，自动监听医患对话并清洗生成结构化病历。这是医疗 Agentic AI 落地的最现实路径——放弃干预临床决策，主动承担最繁重的“行政/合规苦力工作”。\n来源：[[Source_2026医疗数字化：一场剥离幻觉的冷酷清算_20260419.md]]",
    "Healthcare_IT"
)

# Concept_Digital_Twins_in_Healthcare.md
update_or_create_concept(
    "Concept_Digital_Twins_in_Healthcare.md",
    "数字孪生与多组学沙盘 (Digital Twins in Healthcare)",
    "结合基因、蛋白质等真实世界数据，在虚拟环境中为患者建立高精度副本。全球跨国药企绕开临床活体测试的高维玩法，通过纯算力推演药物毒性，拉开医疗数字化绝对的“贫富差距”。",
    "Biomedicine"
)

# Concept_Silent_Degradation_Masking_Failure.md
update_or_create_concept(
    "Concept_Silent_Degradation_Masking_Failure.md",
    "静默降级掩盖失败 (Silent Degradation Masking Failure)",
    "代码中过度“优雅”的后备机制（Fallback）导致底层致命断层（如文件丢失）被掩盖。在系统审计中被发现为核心架构漏洞，要求全面转向“Fail-Fast（快速失败）”与绝对物理路径硬锁。",
    "System_Architecture"
)

# Concept_Shadow_Load.md
update_or_create_concept(
    "Concept_Shadow_Load.md",
    "暗负载 / 认知摩擦 (Shadow Load)",
    "纯精神或认知上的高强度消耗，即使物理消耗极低，也会导致系统性皮质醇淤积与神经疲劳。揭示了过度投入高密度架构推演对生理缓冲垫的快速击穿，需作为阻断工作流的生理预警。通过底层传感器如 [支持:: [[System_Garmin]]] 监控。",
    "Philosophy_and_Cognitive"
)

# Entity_Pear_Therapeutics.md
entity_content = """
---
id: "20260419_ent_pear"
title: "Pear Therapeutics"
type: "entity"
domain: "Strategy_and_Business"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
categories: ["Strategy_and_Business"]
tags: ["数字疗法", "DTx", "反面案例", "破产"]
created: "2026-04-19"
updated: "2026-04-19"
sources: []
---
# Pear Therapeutics

## 核心定位
曾经的“处方数字疗法第一股”。

## 案例分析
作为核心反面案例。因未能向医保支付方证明其“节省医疗成本”的价值，最终破产，标志着单纯追求用户参与度（Engagement）的互联网医疗模式的消亡。
"""
write_file("Entity_Pear_Therapeutics.md", entity_content)

# Entity_北大医疗.md (check if exists, if not create, if exists append)
beida_content = read_file("Entity_北大医疗.md")
date_str = datetime.datetime.now().strftime('%Y-%m-%d')
if beida_content:
    beida_content += f"\n\n## {date_str} 战略审计增补\n业务交涉对象，日志中提到进行业务交流与合作推演。\n"
    write_file("Entity_北大医疗.md", beida_content)
else:
    new_beida = f"""
---
id: "20260419_ent_beida"
title: "北大医疗"
type: "entity"
domain: "Healthcare_IT"
topic_cluster: "General"
status: "Active"
epistemic-status: "evergreen"
categories: ["Healthcare_IT"]
tags: ["合作伙伴", "业务交流"]
created: "{date_str}"
updated: "{date_str}"
sources: []
---
# 北大医疗

## 核心定位
业务交涉对象，日志中提到进行业务交流与合作推演。
"""
    write_file("Entity_北大医疗.md", new_beida)

# Update overview.md
overview = """
---
id: 20260419_overview
title: Vector Lake Overview
type: synthesis
domain: Medical_IT
topic_cluster: General
status: Active
epistemic-status: evergreen
categories:
- System_Architecture
tags:
- 全局概览
- 医疗IT
created: '2026-04-19'
updated: '2026-04-19'
sources: []
---
# Vector Lake Overview

Vector Lake (V7.2) 是一座以医疗信息化 (HIT) 为主轴、兼顾战略架构与 AI 基础设施的深层认知网络。本知识库旨在通过结构化压缩行业变量，揭示医疗 IT 市场的结构性拐点（如 DRG/DIP 控费驱动下的系统重构），并为 AI Agent 在医疗场景的临床决策与院内运营落地提供高维推演支撑。

当前，全球医疗 IT 正面临代际跃迁的重大关口。从依赖“if-then-else”规则与字典表匹配的传统 HIS 巨石架构，全面迈向 Software 3.0 范式的 Agentic Runtime（智能体运行时）。在极高的医疗容错压力下，这一演进摒弃了单纯的涌现智能，确立了“非对称架构”为物理防线。通过建立固态的医疗语义层 (MSL) 充当“翻译黑盒”，并将核心合规逻辑经“洁净室重构”原子化，系统得以防范 LLM 经验毒性并掌握医院财务咽喉。大模型不再仅仅是效率“副驾驶”，而是化身为冷酷的审计权实体，将合规审查从“事后追溯”前置到“事中生成”。为保障医生“敢于签字”的临床确定性，底层架构强制挂载 Evidence-Mesh (证据网)，提供加密因果的推演链路与责任确权。

医疗数字化的商业逻辑已发生根本性反转。数字疗法（DTx）过往的流量叙事彻底破产，被迫转向“结果导向前瞻性支付”，以 [反面案例:: [[Entity_Pear_Therapeutics]]] 为代表的破产宣告了缺乏降本 ROI 逻辑的终结。医院逐步演化为向商业健康险输送高确定性结构化数据的算力工厂。与此同时，在算力受限与数据出境阻断的边缘节点（如超声、移动查房车），AI 架构正向“1-bit 小脑化”（[关联:: [[Concept_1-bit_LLM]]]）物理降维，确立了端侧零依赖的自主生存法则。为了降低合规风险，基于 [应用:: [[Concept_Ambient_AI_Scribes]]] 的隐式环境监听成为 Agentic AI 最现实的落地路径。同时，顶尖药企正利用 [应用:: [[Concept_Digital_Twins_in_Healthcare]]] 的算力垄断拉开贫富差距。

在纯数字与逻辑层面的架构演进中，一个被频繁忽视的物理陷阱是“纯认知摩擦 ([关联:: [[Concept_Shadow_Load]]])”。指挥官在进行无休止的逻辑审计与架构反熵时，往往遭受着严重的睡眠负债与内分泌失调（由 Garmin 等系统监控证实）。这警示我们在拥抱全维度数字化时，必须建立基于生理态势感知的决策阻断机制，以防止肉体在系统走向完美的途中坍塌。此外，面对 [防范:: [[Concept_Silent_Degradation_Masking_Failure]]] 等系统漏洞，架构必须转向 Fail-Fast 和绝对物理路径硬锁。
"""
write_file("overview.md", overview)

# Update log.md
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
append_file("log.md", f"## [{now}] Ingest | 2026医疗数字化冷酷清算与 Q2 Mentat 战略审计")
print("Write complete.")
