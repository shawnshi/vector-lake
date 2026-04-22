import os
import datetime
import random
import string

wiki_dir = r"C:\\Users\\shich\\.gemini\\MEMORY\\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def generate_id():
    today = datetime.datetime.now().strftime("%Y%m%d")
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{today}_{rand}"

def write_node(filename, content):
    path = os.path.join(wiki_dir, filename)
    # Check if exists to preserve some info if needed, but here we overwrite as instructed for step 2
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.strip() + '\n')

def append_to_log(entry):
    log_path = os.path.join(wiki_dir, "log.md")
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"\n## [{now}] {entry}\n")

# --- DATA DEFINITIONS ---

# 1. Source Pages
source_pages = [
    {
        "file": "Source_高质量医疗数据集建设：现状、挑战与未来路径.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"Source_高质量医疗数据集建设：现状、挑战与未来路径\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"Data_Governance\"
status: \"Active\"
alignment_score: 100
epistemic-status: \"evergreen\"
categories: [\"Healthcare_IT\"]
tags: [\"数据资产\", \"数据质量\", \"VAULTIS\", \"DoD\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/digitalhealthobserve/高质量医疗数据集建设：现状、挑战与未来路径.md\"]
---

# 高质量医疗数据集建设：现状、挑战与未来路径

## 核心摘要
本文探讨了医疗人工智能时代高质量数据集的战略价值与建设路径。借鉴美国国防部（DoD）的 VAULTIS 框架，提出将数据视为“战略资产”而非单纯的 IT 负担。文章详细拆解了数据生命周期的 8 个阶段，并强调了“数据即产品 (DaaP)”的核心理念，旨在解决医疗数据中的语义碎片化与标注一致性难题。

## 关键观点
- **数据即战略资产**：借鉴 [[Entity_DoD]] 经验，数据是支撑 AI 优势的基础物理基座[^1]。
- **VAULTIS 框架**：Visible (可见), Accessible (可访问), Understandable (可理解), Linked (可链接), Trustworthy (可信), Interoperable (互操作), Secure (安全)[^1]。
- **数据产品化 (DaaP)**：从“囤积原始数据”向“交付标准数据产品”转型[^1]。

## 关联实体
- [提及:: [[Entity_DoD]]]
- [提及:: [[Entity_DISA]]]
- [关联:: [[Concept_Data_as_a_Product]]]

[^1]: [[Source_高质量医疗数据集建设：现状、挑战与未来路径.md]]"""
    },
    {
        "file": "Source_序言.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"Source_序言\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"General\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"evergreen\"
categories: [\"Philosophy_and_Cognitive\"]
tags: [\"认知向导\", \"第一性原理\", \"医疗AI\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/序言.md\"]
---

# 序言 (医疗大语言模型应用二十讲)

## 核心摘要
作为《二十讲》的总纲，本文定义了在 AI 时代，数字化顾问与专家的全新角色——“认知向导”。文章主张剥离一切算法谄媚，回归医疗业务与人性的“第一性原理”，通过构建跨越系统裂缝的洞察力来重塑专业价值。

## 关键观点
- **认知向导 (Cognitive Navigator)**：不只是交付软件，而是交付对他人的认知资源管理[^1]。
- **第一性原理 (First Principles)**：回归权力、风险、成本、人性、系统这五大基石[^1]。

## 关联实体
- [作者:: [[Entity_Shawn_Shi]]]
- [关联:: [[Concept_Cognitive_Navigator]]]

[^1]: [[Source_序言.md]]"""
    },
    {
        "file": "Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"Theory\"
status: \"Active\"
alignment_score: 100
epistemic-status: \"evergreen\"
categories: [\"Philosophy_and_Cognitive\"]
tags: [\"概率机器\", \"责任黑洞\", \"认知偏差\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/模块一：第一性原理 —— 洞见权力、风险与成本的本质.md\"]
---

# 模块一：第一性原理 —— 洞见权力、风险与成本的本质

## 核心摘要
深入解构了 LLM 的物理属性与社会学风险。文章指出 LLM 本质上是“概率机器”，其幻觉不可消除，只能通过架构包围。同时探讨了 AI 带来的“责任黑洞”以及如何利用/防御人类的“拟人化偏见”与“权威偏见”。

## 关键观点
- **概率机器 (Probability Machine)**：LLM 交付的是概率预测而非绝对真理。幻觉是其生成能力的副产品[^1]。
- **责任黑洞 (Responsibility Black Hole)**：AI 无法在法律意义上承担最终责任，这导致了医疗 AI 落地时的核心屏障[^1]。
- **架构包围算法**：通过刚性的 [支持:: [[Concept_MSL_医疗语义层]]] 与审计钩子来对冲概率引擎的不确定性[^1]。

## 关联概念
- [衍生于:: [[Concept_Probability_Machine]]]
- [关联:: [[Concept_Responsibility_Black_Hole]]]
- [提及:: [[Concept_Medical_AI_Biases]]]

[^1]: [[Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质.md]]"""
    },
    {
        "file": "Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"Strategy\"
status: \"Active\"
alignment_score: 98
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\"]
tags: [\"痛苦指数\", \"权力交汇\", \"黄金场景\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/模块二：场景为王 —— 寻找“权力-利益”的交汇点.md\"]
---

# 模块二：场景为王 —— 寻找“权力-利益”的交汇点

## 核心摘要
提供了一套筛选医疗 AI 落地场景的量化方法论。提出了“痛苦指数”模型，并主张寻找“真实痛苦、支付意愿与权力格局”的交汇点。文章强调，医疗 AI 的落地本质上是对医院内部权力的微调与重组。

## 关键观点
- **痛苦指数 (Pain Index)**：衡量场景价值的公式：频率 × 时长 × 枯燥/风险系数[^1]。
- **权力-利益交汇点**：AI 项目必须触达决策者的核心利益（如合规控费、营收增长）才能生存[^1]。
- **场景分级**：文书减负是入门（生存），核心流程重构是进阶（护城河）[^1]。

## 关联概念
- [衍生于:: [[Concept_Pain_Index]]]
- [关联:: [[Concept_Power_Interest_Intersection]]]

[^1]: [[Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点.md]]"""
    },
    {
        "file": "Source_模块三：方案构建 —— 从“功能设计”到“系统融合”与“风险控制”.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"Source_模块三：方案构建 —— 从“功能设计”到“系统融合”与“风险控制”\"
type: \"source\"
domain: \"Medical_IT\"
topic_cluster: \"Architecture\"
status: \"Active\"
alignment_score: 97
epistemic-status: \"evergreen\"
categories: [\"System_Architecture\"]
tags: [\"工作流粘性\", \"系统融合\", \"防御性设计\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/模块三：方案构建 —— 从“功能设计”到“系统融合”与“风险控制”.md\"]
---

# 模块三：方案构建 —— 从“功能设计”到“系统融合”与“风险控制”

## 核心摘要
探讨了 AI 产品从原型到系统融合的鸿沟。提出了“工作流粘性”作为终极护城河，并讨论了在医疗高压环境下如何进行防御性架构设计。强调了“人机协同”中的认知摩擦机制。

## 关键观点
- **工作流粘性 (Workflow Stickiness)**：真正的壁垒不在于算法模型，而在于 AI 与临床深度工作流的“物理咬合”[^1]。
- **系统融合**：AI 必须从“单体工具”降维融入现有的 HIS/EMR 体系，通过静默监听实现价值[^1]。

## 关联概念
- [关联:: [[Concept_Workflow_Stickiness]]]
- [支持:: [[Concept_Architecture_as_Strategy]]]

[^1]: [[Source_模块三：方案构建 —— 从“功能设计”到“系统融合”与“风险控制”.md]]"""
    }
]

# 2. Concept Pages
concept_pages = [
    {
        "file": "Concept_Cognitive_Navigator.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"认知向导 (Cognitive Navigator)\" 
type: \"concept\"
domain: \"Philosophy_and_Cognitive\"
topic_cluster: \"Expertise\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"sprouting\"
categories: [\"Philosophy_and_Cognitive\"]
tags: [\"咨询转型\", \"认知资源\", \"AI范式\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/序言.md\"]
---

# 认知向导 (Cognitive Navigator)

## 定义
在 AI 算力泛滥、知识获取成本趋零的背景下，专家的新角色定位：不再以提供知识为主要价值，而是通过管理他人的认知资源，将复杂技术变量降维、对齐并重塑为具备决策推动力的深刻洞察。

## 核心能力
- **逻辑脱壳**：穿透各种营销话术（如“赋能”、“智慧”），直击物理本质。
- **语义对齐**：在算法的概率空间与医疗的确定性空间之间建立映射。

## 关联
- [定义者:: [[Entity_Shawn_Shi]]]
- [关联:: [[Concept_Dimensionality_Reduction_Communication]]]
"""
    },
    {
        "file": "Concept_Probability_Machine.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"概率机器 (Probability Machine)\" 
type: \"concept\"
domain: \"Artificial_Intelligence\"
topic_cluster: \"Theory\"
status: \"Active\"
alignment_score: 100
epistemic-status: \"sprouting\"
categories: [\"Artificial_Intelligence\"]
tags: [\"LLM本质\", \"幻觉\", \"风险控制\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/模块一：第一性原理 —— 洞见权力、风险与成本的本质.md\"]
---

# 概率机器 (Probability Machine)

## 定义
揭示大语言模型（LLM）底层物理内核的概念：模型输出的本质是对下一个 Token 的概率预测，而非基于因果逻辑的必然结论。

## 核心特征
- **幻觉共生**：幻觉不是 Bug，而是概率引擎生成新内容的动力源。
- **不确定性**：即使是同一 Prompt，在不同温度值下也可能产生语义漂移。

## 架构对策
[支持:: [[Concept_架构包围算法]]]：必须使用 [支持:: [[Concept_MSL_医疗语义层]]] 等符号系统对概率输出进行刚性校验。
"""
    },
    {
        "file": "Concept_Data_as_a_Product.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"数据即产品 (Data as a Product - DaaP)\" 
type: \"concept\"
domain: \"Medical_IT\"
topic_cluster: \"Data_Governance\"
status: \"Active\"
alignment_score: 100
epistemic-status: \"sprouting\"
categories: [\"System_Architecture\"]
tags: [\"数据治理\", \"DataOps\", \"可信数据\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/digitalhealthobserve/高质量医疗数据集建设：现状、挑战与未来路径.md\"]
---

# 数据即产品 (Data as a Product - DaaP)

## 定义
一种数据治理的新范式。将数据集视为具有明确所有者、版本号、SLO（服务水平目标）及用户反馈机制的“标准产品”，而非静止的文件系统。

## 关键特征
- **自描述性**：自带完备的元数据与语义标签。
- **可发现性**：注册于统一的数据编织（Data Fabric）中。
- **可信性**：经过严格的质量审计与一致性清洗。

## 关联
- [关联:: [[Smart Data Fabric (智能数据编织)]]]
- [关联:: [[Concept_可信数据空间]]]
"""
    },
    {
        "file": "Concept_Medical_AI_Biases.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"医疗 AI 认知偏见 (Medical AI Biases)\" 
type: \"concept\"
domain: \"Philosophy_and_Cognitive\"
topic_cluster: \"HCI\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"sprouting\"
categories: [\"Philosophy_and_Cognitive\"]
tags: [\"自动化偏见\", \"拟人化偏见\", \"人机交互\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/模块一：第一性原理 —— 洞见权力、风险与成本的本质.md\"]
---

# 医疗 AI 认知偏见 (Medical AI Biases)

## 定义
指医疗场景中，人类用户在与 AI 系统交互时容易产生的三种系统性心理偏差，这些偏差往往被算法利用或放大。

## 三大核心偏见
1. **自动化偏见 (Automation Bias)**：过度相信机器的计算结果，即使其逻辑明显错误。
2. **权威偏见 (Authority Bias)**：当 AI 以专家语调输出时，人类倾向于放弃自主审计。
3. **拟人化偏见 (Anthropomorphism)**：赋予 LLM “思考”或“理解”的人性化错觉，从而忽视其“概率机器”的本质。

## 防御
通过 [支持:: [[Concept_Cognitive_Friction]]] (认知摩擦) 强制人类在决策点进行干预[^1]。

[^1]: [[Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质.md]]
"""
    },
    {
        "file": "Concept_Pain_Index.md",
        "content": f"""---
id: \"{generate_id()}\" 
title: \"痛苦指数 (Pain Index)\" 
type: \"concept\"
domain: \"Strategy_and_Business\"
topic_cluster: \"Methodology\"
status: \"Active\"
alignment_score: 98
epistemic-status: \"sprouting\"
categories: [\"Strategy_and_Business\"]
tags: [\"场景筛选\", \"ROI测算\", \"落地策略\"]
created: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/模块二：场景为王 —— 寻找“权力-利益”的交汇点.md\"]
---

# 痛苦指数 (Pain Index)

## 定义
衡量医疗 AI 场景落地价值的量化评估模型。

## 计算公式
**痛苦指数 = 频率 × 时长 × 枯燥/风险系数**

- **频率**：该动作在日常工作中发生的频次。
- **时长**：单次动作消耗的物理时间。
- **系数**：体现了人类对该工作的厌恶程度或该工作出错后的连带风险压力[^1]。

## 应用
痛苦指数越高，AI 替换带来的“获得感”越强，落地阻力越小。

[^1]: [[Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点.md]]
"""
    }
]

# 3. Entity Pages (Updates/Creates)
entity_pages = [
    {
        "file": "Entity_DoD.md",
        "content": f"""---
id: \"20260422_dod001\" 
title: \"DoD (美国国防部)\" 
type: \"entity\"
domain: \"Strategy_and_Business\"
topic_cluster: \"Data_Strategy\"
status: \"Active\"
alignment_score: 90
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\"]
tags: [\"数据战略\", \"VAULTIS\", \"国家安全\"]
created: \"2026-04-10\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/digitalhealthobserve/高质量医疗数据集建设：现状、挑战与未来路径.md\"]
---

# 美国国防部 (DoD - Department of Defense)

## 战略定位
全球数据战略的引领者。其提出的“数据即战略资产”理念，以及旨在打破数据孤岛的 VAULTIS 框架，被广泛引用于医疗数据治理领域[^1]。

## 核心贡献 (Data)
- **VAULTIS 框架**：Visible, Accessible, Understandable, Linked, Trustworthy, Interoperable, Secure.
- **数据生命周期管理**：定义了从采集到销毁的 8 阶段标准流程。

[^1]: [[Source_高质量医疗数据集建设：现状、挑战与未来路径.md]]
"""
    },
    {
        "file": "Entity_Shawn_Shi.md",
        "content": f"""---
id: \"20260422_shawn01\" 
title: \"Shawn Shi (师成)\" 
type: \"entity\"
domain: \"Medical_IT\"
topic_cluster: \"Consulting\"
status: \"Active\"
alignment_score: 100
epistemic-status: \"evergreen\"
categories: [\"Strategy_and_Business\"]
tags: [\"INTJ\", \"认知向导\", \"医疗AI专家\"]
created: \"2026-04-10\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
sources: [\"raw/article/医疗大语言模型应用二十讲/序言.md\"]
---

# 师成 (Shawn Shi)

## 身份定位
医疗数字化转型咨询专家，[关联:: [[Entity_Winning_Health]]] 战略咨询部总经理。提出 [定义:: [[Concept_Cognitive_Navigator]]] (认知向导) 理念及医疗 AI 落地五大基石第一性原理（权力、风险、成本、人性、系统）[^1]。

## 核心主张
- **修系统不修人**：产出不及预期通常是系统架构问题而非单点技术问题。
- **架构包围算法**：坚持用工程的确定性防御概率的不确定性。

[^1]: [[Source_序言.md]]
"""
    }
]

# 4. Global Pages (overview & log)
overview_content = f"""---
id: \"20260422_ov010\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
---
# Vector Lake 知识全景概览 (Overview)

Vector Lake 正在经历从“生成式 AI”向“代理式 AI (Agentic AI)”与**认知工业化 (Cognitive Industrialization)** 的双重范式重组。我们的核心战略已演进为**架构包围算法**：通过构建具备刚性约束力的 **[[Concept_MSL_医疗语义层.md]]**、**[[Concept_Evidence_Mesh.md]]** 及 Teacher-Student 蒸馏架构，来对冲 AI 幻觉并保卫智力主权。

近期，**Shawn Shi (师成)** 在《医疗大语言模型应用二十讲》中提出了 **[[Concept_Cognitive_Navigator.md]]** (认知向导) 的新范式，强调回归“第一性原理”（权力、风险、成本、人性、系统）来透视医疗 AI 的本质。这一认知体系将 LLM 定位为 **[[Concept_Probability_Machine.md]]** (概率机器)，并警示了由其引发的 **Concept_Responsibility_Black_Hole.md** (责任黑洞) 与各种 **[[Concept_Medical_AI_Biases.md]]** (认知偏见)。

在数据基座层面，高质量医疗数据集正被重构为 **[[Concept_Data_as_a_Product.md]]** (数据即产品)，借鉴 **[[Entity_DoD.md]]** 的 VAULTIS 框架实现从“数据囤积”向“数据治理”的跃迁。针对场景落地，**[[Concept_Pain_Index.md]]** (痛苦指数) 与“权力-利益交汇点”提供了量化的筛选标尺。

在行业大势上，中国医疗 IT 正处于从 HIS/EMR 时代的“扫盲式数字化”向以价值医疗（VBH）为导向的“制度深潜”跨越。这一转变催生了“数字化神经系统”这一核心概念，旨在通过重构激励罗盘来实现临床与运营的实时闭环管控。"""

log_entry = f"Ingest | Batch: Data Strategy & Medical LLM 20 Lectures (Part 1-3)"

# --- EXECUTION ---

for page in source_pages + concept_pages + entity_pages:
    write_node(page["file"], page["content"])

write_node("overview.md", overview_content)
append_to_log(log_entry)

# Handle CAS and Winning_Health briefly as minor updates or new
# Winning Health is Entity_Winning_Health
winning_health = f'''---
id: \"20260422_win001\" 
title: \"卫宁健康 (Winning Health)\" 
type: \"entity\"
domain: \"Medical_IT\"
topic_cluster: \"Vendor\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"evergreen\"
categories: [\"Healthcare_IT\"]
tags: [\"WiNEX\", \"WiNGPT\", \"行业领导者\"]
created: \"2026-03-01\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
---
# 卫宁健康 (Winning Health) [300253]

## 战略地位
中国医疗信息化（HIT）领域的领军企业。通过 [产品:: [[Entity_WiNEX]]] 实现架构云化，并利用 [技术:: [[System_WiNGPT]]] 探索 Agentic Hospital 的落地路径。

## 最新动向
在 AI 时代，卫宁正通过 [关联:: [[Concept_MSL_医疗语义层]]] 与 [关联:: [[Concept_架构包围算法]]] 的策略，尝试解决概率机器在严肃医疗场景中的落地摩擦[^1]。

[^1]: [[Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质.md]]
'''
write_node("Entity_Winning_Health.md", winning_health)

cas = f'''---
id: \"20260422_cas001\" 
title: \"复杂适应系统 (CAS - Complex Adaptive System)\" 
type: \"concept\"
domain: \"Philosophy_and_Cognitive\"
topic_cluster: \"Theory\"
status: \"Active\"
alignment_score: 90
epistemic-status: \"evergreen\"
categories: [\"Philosophy_and_Cognitive\"]
tags: [\"系统论\", \"非线性\", \"涌现\"]
created: \"2026-04-10\" 
updated: \"{datetime.datetime.now().strftime('%Y-%m-%d')}\" 
---
# 复杂适应系统 (CAS - Complex Adaptive System)

## 定义
将医院视为由众多具备自主决策能力的个体（医生、管理、患者）组成的“热带雨林”。其核心特征是动态博弈、非线性反馈与**涌现效应**。

## 医疗 AI 的张力
AI 的引入不仅仅是技术的叠加，更是对 CAS 内部平衡的扰动。不考虑“权力-利益地图”的强制落地，往往会引发系统的免疫排斥[^1]。

[^1]: [[Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点.md]]
'''
write_node("Concept_CAS.md", cas)

print("SUCCESS: All nodes written to Wiki.")
