import os
import datetime

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def write_f(filename, lines):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

def append_f(filename, lines):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

# 1. Sources
write_f("Source_诊断的不确定性：当我们在医疗中引入随机性引擎.md", [
    '---', 'id: "20260422_src001"', 'title: "诊断的不确定性：当我们在医疗中引入随机性引擎"', 'type: "source"',
    'domain: "Medical_IT"', 'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "seed"', 'ttl: 365', 'categories: ["Healthcare_IT"]', 'tags: ["不确定性", "Martin_Fowler", "概率机器"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/诊断的不确定性：当我们在医疗中引入随机性引擎.md"]', '---',
    '# 诊断的不确定性：当我们在医疗中引入随机性引擎', '', '## 核心主旨',
    '探讨医疗软件工程从“确定性（Determinism）”向“非确定性（Non-determinism）”的范式跃迁。指出 AI 作为概率引擎与循证医学（EBM）之间的冲突，并提出医生角色应向“验证架构师”转变。', '',
    '## 关键实体',
    '- **[属于:: [[Entity_Martin_Fowler]]]**：软件工程领袖，提出 AI 带来的最大变量是从确定性到非确定性的跃迁。',
    '- **[属于:: [[Medical Semantic Layer (MSL)]]]**：作为管理概率风险的物理锚点。', '',
    '## 关键概念',
    '- **[包含:: [[Concept_Probability_Chasm_概率鸿沟]]]**：医疗的 100% 责任与 AI 的概率输出之间的鸿沟。',
    '- **[包含:: [[Concept_Cognitive_Verification_认知验证]]]**：医生核心能力从“记忆知识”转向“验证建议”。', '',
    '## 核心论点',
    '1. **范式断裂**：AI 允许自然语言交互，但也强迫接受结果的不确定性。',
    '2. **三明治模型**：医疗 AI 架构应分为通识层（LLM）、机构层（RAG/院情）和个人层（数字孪生）。',
    '3. **数字孪生 (Digital Twin)**：通过提示词工程训练出的、外化医生临床思维的个性化 AI 助手。'
])

write_f("Source_跨越“概率鸿沟”：医疗AI从“技术实验”到“核心生产力”的工程跃迁.md", [
    '---', 'id: "20260422_src002"', 'title: "跨越“概率鸿沟”：医疗AI从“技术实验”到“核心生产力”的工程跃迁"', 'type: "source"',
    'domain: "Medical_IT"', 'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "seed"', 'ttl: 365', 'categories: ["Artificial_Intelligence"]', 'tags: ["概率鸿沟", "工程化", "核心生产力"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/跨越“概率鸿沟”：医疗AI从“技术实验”到“核心生产力”的工程跃迁.md"]', '---',
    '# 跨越“概率鸿沟”：医疗AI从“技术实验”到“核心生产力”的工程跃迁', '', '## 核心主旨',
    '分析医疗 AI 落地的深层摩擦，强调通过“架构包围算法”实现从实验性技术向核心生产力的跨越。', '',
    '## 关键概念',
    '- **[包含:: [[Concept_Probability_Chasm_概率鸿沟]]]**：AI 落地医疗的首要阻碍。',
    '- **[包含:: [[Concept_架构包围算法]]]**：利用 MSL 和刚性围栏来对冲算法的概率波动。'
])

write_f("Source_跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md", [
    '---', 'id: "20260422_src003"', 'title: "跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)"', 'type: "source"',
    'domain: "Medical_IT"', 'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "seed"', 'ttl: 365', 'categories: ["Healthcare_IT"]', 'tags: ["OpenAI_Frontier", "AI原生医院", "Teacher-Student"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md"]', '---',
    '# 跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)', '', '## 核心主旨',
    '定义 AI 原生医院（AI-Native Hospital）的演进路径，强调将 AI 作为核心操作系统，而非 HIS 的插件。提出基于 Teacher-Student 蒸馏的韧性设计。', '',
    '## 关键实体',
    '- **[属于:: [[Entity_OpenAI_Frontier]]]**：AI 原生医院的基座架构，提供强大的推理与多模态理解。',
    '- **[属于:: [[Entity_Agentic_Hospital]]]**：医院数智化的高级形态。', '',
    '## 关键概念',
    '- **[包含:: [[Concept_Teacher_Student_Distillation_教师-学生蒸馏架构]]]**：解决“数字黑暗”下的系统生存问题。',
    '- **[包含:: [[Concept_HITL_2.0]]]**：针对 AI 逻辑路径的实时审计与防御性校验。', '',
    '## 核心论点',
    '1. **语义层底座**：利用大模型作为通用翻译层，桥接 Legacy SQL 孤岛，降低集成成本。',
    '2. **问责重构**：将责任从“结果追溯”前移至“规则定义”与“实时审计”。',
    '3. **韧性设计**：通过 Teacher（云端大模型）向 Student（本地 SLM）的逻辑蒸馏，确保断网时的临床连续性。'
])

write_f("Source_软件的三次浪潮与智能体时代的黎明：范式、未来与前沿的深度解析.md", [
    '---', 'id: "20260422_src004"', 'title: "软件的三次浪潮与智能体时代的黎明：范式、未来与前沿的深度解析"', 'type: "source"',
    'domain: "System_Architecture"', 'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 95',
    'epistemic-status: "seed"', 'ttl: 365', 'categories: ["System_Architecture"]', 'tags: ["Software_3.0", "Andrej_Karpathy", "范式迁移"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/软件的三次浪潮与智能体时代的黎明：范式、未来与前沿的深度解析.md"]', '---',
    '# 软件的三次浪潮与智能体时代的黎明：范式、未来与前沿的深度解析', '', '## 核心主旨',
    '系统梳理软件 1.0/2.0/3.0 的演进逻辑，展望智能体时代（Software 4.0）的特征，探讨计算权力的重新集中化与交互范式的坍缩。', '',
    '## 关键实体',
    '- **[属于:: [[Entity_Andrej_Karpathy]]]**：定义了软件 2.0/3.0 的演进路径。', '',
    '## 关键概念',
    '- **[包含:: [[Concept_Software_3.0]]]**：将 LLM 视为新的操作系统，自然语言为编程语言。',
    '- **[包含:: [[Concept_Vibe_Coding]]]**：基于感性氛围与实时反馈的开发模式。',
    '- **[包含:: [[Concept_Autonomy_Slider_自主性滑块]]]**：动态调节 AI 介入程度的交互设计。', '',
    '## 核心论点',
    '1. **权力归口**：软件 3.0 导致算力向超大规模云服务商集中。',
    '2. **交互坍缩**：未来软件将消失在极简的智能体界面中。',
    '3. **软件 4.0 预言**：具备持续学习能力、神经符号集成及无处不在的个性化运行时。'
])

write_f("Source_软件的终局与医疗逻辑的重构：Nadella 的“三位一体”预言深处-20260420.md", [
    '---', 'id: "20260422_src005"', 'title: "软件的终局与医疗逻辑的重构：Nadella 的“三位一体”预言深处-20260420"', 'type: "source"',
    'domain: "Medical_IT"', 'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "seed"', 'ttl: 365', 'categories: ["Healthcare_IT"]', 'tags: ["Satya_Nadella", "三位一体架构", "交互终局"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/软件的终局与医疗逻辑的重构：Nadella 的“三位一体”预言深处-20260420.md"]', '---',
    '# 软件的终局与医疗逻辑的重构：Nadella 的“三位一体”预言深处-20260420', '', '## 核心主旨',
    '通过 Satya Nadella 的“三位一体”预言，探讨软件界面向 Inbox/Messaging/Canvas 的收敛，及其对医疗工作站重构的深远影响。', '',
    '## 关键实体',
    '- **[属于:: [[Entity_Satya_Nadella]]]**：提出三位一体交互范式。', '',
    '## 关键概念',
    '- **[包含:: [[Concept_三位一体架构]]]**：Inbox (异步过滤), Messaging (意图对齐), Canvas (非线性思维)。',
    '- **[属于:: [[Concept_Agentic_HIS]]]**：通过智能体彻底重塑的医院信息系统。'
])

# 2. Entities
write_f("Entity_Satya_Nadella.md", [
    '---', 'id: "20260422_ent013"', 'title: "Satya Nadella"', 'type: "entity"', 'domain: "Strategy_and_Business"',
    'topic_cluster: "AI_Leadership"', 'status: "Active"', 'alignment_score: 90', 'epistemic-status: "evergreen"',
    'ttl: 1095', 'categories: ["Strategy_and_Business"]', 'tags: ["Microsoft", "CEO", "三位一体预言"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/软件的终局与医疗逻辑的重构：Nadella 的“三位一体”预言深处-20260420.md"]', '---',
    '# Satya Nadella', '', '微软 CEO。在 AI 时代提出了关于软件演进的关键预言。', '', '## 核心贡献',
    '- **三位一体架构预言**：认为未来软件将坍缩为 **[包含:: [[Concept_三位一体架构]]]** (Inbox, Messaging, Canvas)。',
    '- **意图驱动计算**：强调系统应围绕人类意图而非软件模块进行组织。'
])

write_f("Entity_Martin_Fowler.md", [
    '---', 'id: "20260422_ent014"', 'title: "Martin Fowler"', 'type: "entity"', 'domain: "System_Architecture"',
    'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 90', 'epistemic-status: "evergreen"',
    'ttl: 1095', 'categories: ["System_Architecture"]', 'tags: ["ThoughtWorks", "软件工程", "非确定性"]',
    'created: "2026-04-22"', 'updated: "2026-04-22"', 'sources: ["raw/article/digitalhealthobserve/诊断的不确定性：当我们在医疗中引入随机性引擎.md"]', '---',
    '# Martin Fowler', '', '软件工程大师。针对 AI 时代的开发范式提出了深刻见解。', '', '## 核心观点',
    '- **非确定性跃迁**：指出 AI 带来的最大变革是从确定性逻辑向非确定性概率的跃迁。',
    '- **Vibe Coding 警告**：提醒开发者警惕“感性编程”导致的认知闭环断裂。'
])

# 3. Concepts
write_f("Concept_Probability_Chasm_概率鸿沟.md", [
    '---', 'id: "20260422_con001"', 'title: "概率鸿沟 (The Probability Chasm)"', 'type: "concept"',
    'domain: "Healthcare_IT"', 'topic_cluster: "Philosophy_and_Cognitive"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "evergreen"', 'ttl: 1825', 'categories: ["Philosophy_and_Cognitive", "Healthcare_IT"]',
    'tags: ["确定性", "风险管理", "AI伦理"]', 'created: "2026-04-22"', 'updated: "2026-04-22"',
    'sources: ["raw/article/digitalhealthobserve/跨越“概率鸿沟”：医疗AI从“技术实验”到“核心生产力”的工程跃迁.md", "raw/article/digitalhealthobserve/诊断的不确定性：当我们在医疗中引入随机性引擎.md"]', '---',
    '# 概率鸿沟 (The Probability Chasm)', '', '## 定义',
    '医疗行业要求的 100% 确定性责任与 AI 本质上的概率预测之间的本质冲突。', '', '## 表现形式',
    '- **责任真空**：当概率性的 AI 给出错误建议时，法律责任的界定变得模糊。',
    '- **自动化偏见**：医生过分依赖高概率建议而丧失临床直觉。', '', '## 跨越路径',
    '- **架构包围算法**：不追求消灭概率，而是通过 **[依赖:: [[Medical Semantic Layer (MSL)]]]** 和刚性围栏来约束风险。',
    '- **[关联:: [[Concept_HITL_2.0]]]**：将责任从结果转向规则定义与路径审计。'
])

write_f("Concept_Software_3.0.md", [
    '---', 'id: "20260422_con002"', 'title: "软件 3.0 (Software 3.0)"', 'type: "concept"',
    'domain: "System_Architecture"', 'topic_cluster: "Paradigm_Shift"', 'status: "Active"', 'alignment_score: 95',
    'epistemic-status: "sprouting"', 'ttl: 1825', 'categories: ["System_Architecture"]',
    'tags: ["LLM_as_OS", "Vibe_Coding", "自然语言编程"]', 'created: "2026-04-22"', 'updated: "2026-04-22"',
    'sources: ["raw/article/digitalhealthobserve/软件的三次浪潮与智能体时代的黎明：范式、未来与前沿的深度解析.md"]', '---',
    '# 软件 3.0 (Software 3.0)', '', '## 定义',
    '由 **[属于:: [[Entity_Andrej_Karpathy]]]** 提出，指代以 LLM 为核心运行时、自然语言（Prompt）为编程语言的新一代开发范式。', '', '## 特征',
    '- **LLM 即操作系统**：模型不仅是工具，更是调度知识、推理与 IO 的核心引擎。',
    '- **Vibe Coding**：开发过程从“编写逻辑”转向“意图对齐”与“感性氛围调整”。',
    '- **计算权力重组**：算力向超大规模服务商集中。'
])

write_f("Concept_HITL_2.0.md", [
    '---', 'id: "20260422_con003"', 'title: "HITL 2.0 (Human-in-the-Loop 2.0)"', 'type: "concept"',
    'domain: "Healthcare_IT"', 'topic_cluster: "Governance"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "seed"', 'ttl: 1825', 'categories: ["Healthcare_IT", "Artificial_Intelligence"]',
    'tags: ["问责机制", "路径审计", "人机协作"]', 'created: "2026-04-22"', 'updated: "2026-04-22"',
    'sources: ["raw/article/digitalhealthobserve/跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md"]', '---',
    '# HITL 2.0 (Human-in-the-Loop 2.0)', '', '## 定义',
    '新一代人机协作范式，从简单的“结果审核”转向对 AI 决策逻辑路径的“实时审计”与“责任确权”。', '', '## 核心特征',
    '- **逻辑可见性**：AI 不再仅提供结论，必须展示其推理证据链。',
    '- **3D 逻辑审计**：引入仿真度、防御力、信息熵等维度。',
    '- **防御性干预**：在置信度不足时，强制人类专家进入“深度推理”模式。'
])

write_f("Concept_Teacher_Student_Distillation_教师-学生蒸馏架构.md", [
    '---', 'id: "20260422_con004"', 'title: "教师-学生蒸馏架构 (Teacher-Student Distillation Architecture)"', 'type: "concept"',
    'domain: "System_Architecture"', 'topic_cluster: "Resilience"', 'status: "Active"', 'alignment_score: 95',
    'epistemic-status: "seed"', 'ttl: 1825', 'categories: ["System_Architecture", "Artificial_Intelligence"]',
    'tags: ["SLM", "离线自治", "边缘计算"]', 'created: "2026-04-22"', 'updated: "2026-04-22"',
    'sources: ["raw/article/digitalhealthobserve/跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md"]', '---',
    '# 教师-学生蒸馏架构 (Teacher-Student Distillation Architecture)', '', '## 定义',
    '一种通过云端强大模型（Teacher）将逻辑与领域知识蒸馏至边缘侧小模型（Student/SLM）的部署模式。', '', '## 战略意义',
    '- **生存底座 (Survival Base)**：在“数字黑暗”（断网/云端故障）时刻，SLM 保留核心推理能力。',
    '- **ROI 优化**：将高频任务下沉至 SLM，降低成本。',
    '- **隐私强化**：敏感逻辑在本地执行。'
])

# 4. Updates
write_f("overview.md", [
    '---', 'id: 20260422_ov008', 'updated: "20260422"', '---',
    '# Vector Lake 知识全景概览 (Overview)', '',
    'Vector Lake 正在经历从“生成式 AI”向“代理式 AI (Agentic AI)”与**认知工业化 (Industrialization of Cognition)** 的双重范式重组。我们的核心战略已演进为**架构包围算法**：通过构建具备刚性约束力的**医疗语义层 (MSL)**、**证据网 (Evidence-Mesh)** 及 **Teacher-Student 蒸馏架构**，来对冲 AI 幻觉并保卫智力主权。', '',
    '在临床交互侧，我们正目睹从“录入逻辑”向“全域感知”的跃迁。Satya Nadella 的**三位一体架构**（Inbox, Messaging, Canvas）正在炸毁束缚医生的**表单监狱**，重夺**事实获取权**。同时，通过人为引入**认知摩擦 (Cognitive Friction)** 与 **HITL 2.0** 协议，我们确保在追求极致效率的同时，依然维持人类对关键决策的终审权。', '',
    '在全球视野下，软件正经历从 1.0 确定性到 3.0 概率性的惊险一跃。**Andrej Karpathy** 预言的“大型机时代”正在回潮，算力高度集中。在这一背景下，医疗 IT 的未来在于**价值共创**，供应商必须通过**逻辑湖 (Logic Lake)** 捕获专家的隐性智慧，构建跨越**概率鸿沟**的工程化防线。'
])

append_f("log.md", [f"## [{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}] Ingest Batch | Probability Chasm & Software 3.0 (5 sources)"])

print("Batch Ingest Success")
