import os
import datetime

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

# 1. Source Pages
sources = [
    {
        "file": "Source_从数字化到智能化：中国医疗信息化的过去、现在与未来.md",
        "title": "从数字化到智能化：中国医疗信息化的过去、现在与未来",
        "content": """---
id: \"20260421_s1a2b3\"
title: \"Source_从数字化到智能化：中国医疗信息化的过去、现在与未来\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"evergreen\"
categories: [\"Healthcare_IT\"]
tags: [\"中国医疗信息化\", \"发展阶段\", \"集成困局\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/从数字化到智能化：中国医疗信息化的过去、现在与未来.md\"]
---

# 从数字化到智能化：中国医疗信息化的过去、现在与未来

## 核心摘要
本文梳理了中国医疗信息化发展的四个阶段：以行政管理为核心的 HIS 阶段、以临床应用为核心的 CIS/EMR 阶段、以互联互通为核心的平台阶段，以及正在迈入的以价值驱动为核心的智能化阶段。文章指出，当前中国医疗 IT 正处于由系统碎片化和标准不统一导致的“集成困局”中，迫切需要通过重构架构实现向智能化的跃迁。

## 关键观点
- **集成困局 (Integration Abyss)**：由于早期建设缺乏顶层规划，导致医院内部形成了大量“数据孤岛”，系统间的互操作性极差[^1]。
- **范式转移**：从“流程驱动”转向“价值驱动”，IT 系统必须从记录工具进化为智能反馈系统[^1]。

## 关联实体
- [属于:: [[中国医疗信息化市场战略分析]]]
- [支持:: [[Concept_Medical_Semantic_Layer]]] (作为解决集成困局的技术方案)

[^1]: [[Source_从数字化到智能化：中国医疗信息化的过去、现在与未来.md]]"""
    },
    {
        "file": "Source_价值医疗的本质：重塑激励罗盘与构建数字化神经系统.md",
        "title": "价值医疗的本质：重塑激励罗盘与构建数字化神经系统",
        "content": """---
id: \"20260421_s2c3d4\"
title: \"Source_价值医疗的本质：重塑激励罗盘与构建数字化神经系统\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
alignment_score: 98
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\"]
tags: [\"价值医疗\", \"数字化神经系统\", \"激励机制\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/价值医疗的本质：重塑激励罗盘与构建数字化神经系统.md\"]
---

# 价值医疗的本质：重塑激励罗盘与构建数字化神经系统

## 核心摘要
本文深入探讨了价值医疗（Value-Based Healthcare, VBH）的本质及其实现路径。核心逻辑在于通过重构“激励罗盘”，即从按量付费（FFS）向按价值付费（DRG/DIP）转型，迫使医疗机构构建“数字化神经系统”以实现成本与质量的实时精准管控。

## 关键观点
- **数字化神经系统 (Digital Nervous System)**：指能够实时感知临床和运营数据，并通过闭环反馈机制做出响应的 IT 系统架构[^1]。
- **激励罗盘 (Incentive Compass)**：底层激励机制的改变是推动医疗数字化的根本动力[^1]。

## 关联概念
- [衍生于:: [[Concept_数字化神经系统]]]
- [关联:: [[Concept_Agentic_Hospital]]]

[^1]: [[Source_价值医疗的本质：重塑激励罗盘与构建数字化神经系统.md]]"""
    },
    {
        "file": "Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md",
        "title": "全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告",
        "content": """---
id: \"20260421_s3e4f5\"
title: \"Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
alignment_score: 92
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\"]
tags: [\"医疗AI独角兽\", \"投资逻辑\", \"工作流集成\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md\"]
---

# 全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告

## 核心摘要
该报告分析了全球医疗 AI 独角兽（如 Tempus, Xaira, Abridge）的竞争策略。文章提出，在 AI 2.0 时代，算法优势正被快速平庸化，真正的防御性来自于对专有数据的掌控、对核心工作流的深度集成以及对监管门控的专业应对。

## 关键观点
- **防御三位一体 (Defensive Triad)**：专有数据资产、深度集成入口（工作流）、监管合规门控[^1]。
- **工作流为王**：成功的医疗 AI 必须嵌入医生的现有操作流中，而不是作为一个独立的工具[^1]。

## 关联实体
- [提及:: [[Entity_Tempus]]]
- [提及:: [[Entity_Xaira_Therapeutics]]]
- [提及:: [[Entity_Abridge]]]
- [提及:: [[Entity_Insilico_Medicine]]]

[^1]: [[Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md]]"""
    },
    {
        "file": "Source_冰与火之歌：一份医疗AI“新基建”的工业革命蓝图.md",
        "title": "冰与火之歌：一份医疗AI“新基建”的工业革命蓝图",
        "content": """---
id: \"20260421_s4g5h6\"
title: \"Source_冰与火之歌：一份医疗AI“新基建”的工业革命蓝图\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
alignment_score: 94
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\"]
tags: [\"医疗AI新基建\", \"认知工业化\", \"可信数据空间\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/冰与火之歌：一份医疗AI“新基建”的工业革命蓝图.md\"]
---

# 冰与火之歌：一份医疗AI“新基建”的工业革命蓝图

## 核心摘要
本文将医疗 AI 的落地比作工业革命，提出需要建立支撑“认知工业化”的新型基础设施。重点讨论了“可信数据空间”在解决医疗数据隐私与流转矛盾中的核心作用。

## 关键观点
- **认知工业化 (Industrialized Cognition)**：通过 AI 将顶级专家的诊疗逻辑标准化并大规模复制[^1]。
- **可信数据空间 (Trusted Data Space)**：基于“数据可用不可见”的物理与法律架构，保障数据要素的合规流通[^1]。

## 关联概念
- [支持:: [[Concept_可信数据空间]]]
- [关联:: [[Concept_认知工业化]]]

[^1]: [[Source_冰与火之歌：一份医疗AI“新基建”的工业革命蓝图.md]]"""
    },
    {
        "file": "Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md",
        "title": "医疗AI的“十五五”：从技术狂欢到制度深潜",
        "content": """---
id: \"20260421_s5i6j7\"
title: \"Source_医疗AI的“十五五”：从技术狂欢到制度深潜\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
alignment_score: 96
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\"]
tags: [\"十五五规划\", \"制度深潜\", \"医疗AI落地\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/医疗AI的“十五五”：从技术狂欢到制度深潜.md\"]
---

# 医疗AI的“十五五”：从技术狂欢到制度深潜

## 核心摘要
针对“十五五”规划的前瞻性分析。文章认为，医疗 AI 将告别概念炒作阶段，进入深度嵌入医疗制度（医保支付、责任认定、临床路径）的“制度深潜”期。

## 关键观点
- **制度深潜 (Institutional Deep Diving)**：技术落地已非首要障碍，制度性的配套（如 AI 诊疗收费、法律责任判定）将成为核心变量[^1]。
- **中试基地模式**：通过设立国家级人工智能应用中试基地，在受控沙盒中加速产品化[^1]。

## 关联概念
- [属于:: [[医疗数字化的“十五五”：从“连接”到“重塑”的惊险一跃]]]
- [支持:: [[Concept_制度深潜]]]

[^1]: [[Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md]]"""
    }
]

# 2. Concept Pages
concepts = [
    {
        "file": "Concept_数字化神经系统.md",
        "content": """---
id: \"20260421_c1n2o3\"
title: \"Concept_数字化神经系统\"
type: \"concept\"
domain: \"Medical_IT\"
topic_cluster: \"Architecture\"
status: \"Active\"
alignment_score: 98
epistemic-status: \"sprouting\"
categories: [\"System_Architecture\"]
tags: [\"闭环反馈\", \"价值医疗\", \"实时感知\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/价值医疗的本质：重塑激励罗盘与构建数字化神经系统.md\"]
---

# 数字化神经系统 (Digital Nervous System)

## 定义
一种能够实时感知医疗机构临床与运营数据，并根据预设逻辑（如临床路径、控费规则）自动做出响应、干预或决策反馈的闭环系统架构。它是实现“价值医疗”的技术底座。

## 核心特征
1. **实时感知**：不再依赖事后报表，而是实时监控临床关键指标。
2. **自动反馈**：在工作流中实时嵌入提醒或阻断逻辑。
3. **闭环演进**：通过结果数据不断优化前端决策规则。

## 战略价值
解决中国 HIT 长期存在的“重记录、轻管控”问题[^1]。

## 关联
- [支持:: [[Source_价值医疗的本质：重塑激励罗盘与构建数字化神经系统.md]]]
- [属于:: [[Concept_Agentic_Hospital]]]

[^1]: [[Source_价值医疗的本质：重塑激励罗盘与构建数字化神经系统.md]]"""
    },
    {
        "file": "Concept_制度深潜.md",
        "content": """---
id: \"20260421_c2p3q4\"
title: \"Concept_制度深潜\"
type: \"concept\"
domain: \"Medical_IT\"
topic_cluster: \"Strategy\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"sprouting\"
categories: [\"Strategy_and_Business\"]
tags: [\"十五五\", \"医疗改革\", \"AI落地\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/医疗AI的“十五五”：从技术狂欢到制度深潜.md\"]
---

# 制度深潜 (Institutional Deep Diving)

## 定义
指医疗新技术（尤其是 AI）从单纯的技术实验阶段，进入到与医疗行业的支付制度、责任体系、临床准入路径以及绩效考核深度融合的阶段。

## 特征
- **从“加法”到“乘法”**：AI 不再是医疗流程外的额外插件，而是重构流程的内生要素。
- **利益博弈显性化**：涉及医保基金分配、医生责任分担等深层利益调整[^1]。

## 关联
- [支持:: [[Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md]]]
- [关联:: [[“十五五”时期大型公立医院数字化建设迈向高质量发展的重点与路径战略分析]]]

[^1]: [[Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md]]"""
    },
    {
        "file": "Concept_可信数据空间.md",
        "content": """---
id: \"20260421_c3r4s5\"
title: \"Concept_可信数据空间\"
type: \"concept\"
domain: \"Medical_IT\"
topic_cluster: \"Architecture\"
status: \"Active\"
alignment_score: 97
epistemic-status: \"sprouting\"
categories: [\"System_Architecture\"]
tags: [\"数据要素\", \"隐私计算\", \"数据可用不可见\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/冰与火之歌：一份医疗AI“新基建”的工业革命蓝图.md\"]
---

# 可信数据空间 (Trusted Data Space)

## 定义
一种基于物理隔离、法律合规与隐私计算技术（如联邦学习、TEE）构建的医疗数据流通架构。其核心准则是“数据可用不可见，用途可控可计量”。

## 解决的矛盾
解决医疗机构对数据主权的保护诉求与外部（如 AI 开发、跨院协作）对数据利用诉求之间的冲突[^1]。

## 关联
- [支持:: [[Source_冰与火之歌：一份医疗AI“新基建”的工业革命蓝图.md]]]
- [关联:: [[医疗机构可信数据空间建设建设策略与实施路径]]]
- [关联:: [[在AI与可信数据空间驱动下的新一代医院数据平台：定位、框架与建设策略]]]

[^1]: [[Source_冰与火之歌：一份医疗AI“新基建”的工业革命蓝图.md]]"""
    }
]

# 3. Entity Pages
entities = [
    {
        "file": "Entity_Xaira_Therapeutics.md",
        "content": """---
id: \"20260421_e1u2v3\"
title: \"Entity_Xaira_Therapeutics\"
type: \"entity\"
domain: \"Biomedicine\"
topic_cluster: \"Investment\"
status: \"Active\"
alignment_score: 85
epistemic-status: \"seed\"
categories: [\"Strategy_and_Business\"]
tags: [\"AI新药研发\", \"TechBio\", \"独角兽\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md\"]
---

# Xaira Therapeutics

## 基本信息
2024 年成立的顶级 AI 新药研发（TechBio）公司，融资额巨大。其核心竞争力在于利用生成式 AI 设计全新的蛋白质药物。

## 战略地位
代表了医疗 AI 投资的“杠铃式”一端：极端的基础科学前沿突破。与 Abridge 这种解决现有流程效率的公司形成对比[^1]。

## 关联
- [属于:: [[Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md]]]
- [对比:: [[Entity_Insilico_Medicine]]]

[^1]: [[Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md]]"""
    },
    {
        "file": "Entity_Abridge.md",
        "content": """---
id: \"20260421_e2w3x4\"
title: \"Entity_Abridge\"
type: \"entity\"
domain: \"Medical_IT\"
topic_cluster: \"Product\"
status: \"Active\"
alignment_score: 92
epistemic-status: \"sprouting\"
categories: [\"Healthcare_IT\"]
tags: [\"Ambient_AI\", \"临床文档\", \"Epic集成\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md\"]
---

# Abridge

## 基本信息
环境式 AI 临床文档（Ambient AI Scribes）领域的领先者。通过监听医患对话并自动生成结构化病历，极大地缓解了医生的书写负担。

## 核心策略
**深度集成工作流**：Abridge 的成功很大程度上归功于其与 Epic 系统的深度垂直集成，使其直接进入医生的核心工作界面，从而建立了极高的防御性[^1]。

## 关联
- [属于:: [[Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md]]]
- [关联:: [[Concept_Ambient_AI_Scribes]]]
- [关联:: [[Entity_Epic_Systems]]]

[^1]: [[Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md]]"""
    },
    {
        "file": "Entity_数坤科技.md",
        "content": """---
id: \"20260421_e3y4z5\"
title: \"Entity_数坤科技\"
type: \"entity\"
domain: \"Medical_IT\"
topic_cluster: \"China_Market\"
status: \"Active\"
alignment_score: 88
epistemic-status: \"sprouting\"
categories: [\"Healthcare_IT\"]
tags: [\"AI影像\", \"中国医疗AI\", \"独角兽\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/从数字化到智能化：中国医疗信息化的过去、现在与未来.md\"]
---

# 数坤科技 (Shukun)

## 基本信息
中国领先的医疗 AI 影像厂商。其核心产品覆盖心脑血管、肺部、骨科等多个领域。

## 演进方向
正从单一的 AI 影像诊断工具向贯穿全临床业务流的“数字医生”演进，试图解决中国医疗 IT 的“集成困局”[^1]。

## 关联
- [属于:: [[Source_从数字化到智能化：中国医疗信息化的过去、现在与未来.md]]]
- [关联:: [[中国医疗信息化市场战略分析]]]

[^1]: [[Source_从数字化到智能化：中国医疗信息化的过去、现在与未来.md]]"""
    }
]

# Process writes
for item in sources + concepts + entities:
    write_file(item["file"], item["content"])

# Update overview
overview_content = """---
id: 20260421_7d8x6e
updated: '2026-04-21'
---
# Vector Lake 知识全景概览 (Overview)

Vector Lake 当前锚定于解决医疗 IT 与系统架构领域的结构性拐点与抗熵增难题。通过构建纯 Markdown 的多智能体状态机，本知识库重点映射了由核心中枢 Mentat 所指挥的本地自主智能体集群架构。这套架构抛弃了单纯的单体大模型幻想，转而拥抱物理约束、隔离边界与自动化防腐管线，并主张从被动的 RAG 检索向具有物理边界的 Agentic 编译跃迁。

在防御与认知层面，图谱深入探讨了四层壳模型、活沙箱化子代理与黑板模式的协作机制。这些物理维度的架构设计用于对抗大模型固有的上下文腐烂与注意力坍塌问题。同时，结合 Gotchas (避坑先验) 的负样本沉淀与零自我防御 (Zero-Ego) 哲学，确保整个系统在高度复杂的业务博弈中维持极高密度的逻辑信噪比与持续演化能力。医疗语义层（MSL）作为核心的置信度评估网与降级触发器，强行拦截概率引擎与 HIS 确定性引擎之间的错配风险。

近期研究揭示了 AI 在落地深水区（如医疗）遭遇的“合规红线与物理降维”。大模型正从云端全能幻觉退守边缘侧（1-bit 小脑化），其核心价值锚定于实时合规拦截与环境监听（Ambient AI Scribes）。在战略布局上，中国特色的顶层意志通过《意见》和“十五五”规划，推行“钳形攻势”以及设立“国家人工智能应用中试基地”，意图在合规与创新间建立沙箱。中国医疗 IT 正在经历从 HIS/EMR 时代的“扫盲式数字化”向以价值医疗（VBH）为导向的“制度深潜”跨越。这一转变催生了“数字化神经系统”这一核心概念，旨在通过重构激励罗盘（从按量到按价值）来实现临床与运营的实时闭环管控。

在商业护城河方面，“防御三位一体”框架（专有数据、工作流整合、监管准入）正取代单纯算法优势，促使资本发生“杠铃式”分化——涌向基础前沿（如 Xaira Therapeutics）或确切 ROI 工具（如 Abridge）。系统控制权的保卫与抵御算力附庸，仍然是智能体医院下半场的绝对核心命题。解决“集成困局”不仅需要 MSL 这种软件定义方案，更需要“可信数据空间”等物理层基础设施的支持，以保障数据要素在安全、主权与价值之间的动态平衡。"""
write_file("overview.md", overview_content)

# Update log
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
log_entry = f"## [{now}] Ingest | Healthcare AI Reports Ingestion Batch (5 Sources)"
append_file("log.md", log_entry)

print("Batch Ingestion Complete")
