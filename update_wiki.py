import os
import datetime

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def write_file(filename, lines):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

def append_file(filename, lines):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

src_lines = [
    '---',
    'id: "20260419_src_tee_sandbox_fl"',
    'title: "联邦学习 | 隔离沙箱 | 隐私计算"',
    'type: "source"',
    'domain: "System_Architecture"',
    'topic_cluster: "General"',
    'status: "Active"',
    'epistemic-status: "evergreen"',
    'categories: ["System_Architecture"]',
    'tags: ["隐私计算", "TEE", "联邦学习", "隔离沙箱"]',
    'created: "2026-04-19"',
    'updated: "2026-04-19"',
    'sources: ["raw/research/research_tee_sandbox_fl_privacy.md"]',
    '---',
    '# 联邦学习 | 隔离沙箱 | 隐私计算',
    '',
    '## 核心主旨',
    '本文档深入拆解隐私计算三大支柱（隔离沙箱、TEE、联邦学习），为医疗主权架构和数据城邦防御提供底层基础设施锚点，并明确指出单一技术的局限性，引出“TEE+联邦学习”的混合架构趋势。',
    '',
    '## 关键实体',
    '- **Intel SGX / ARM TrustZone**：提供 TEE 的硬件级黑盒，支撑隐私计算底层架构（边缘/举例）[属于:: [[Concept_可信执行环境_TEE]]]。',
    '',
    '## 关键概念',
    '- **[包含:: [[Concept_隐私计算_Privacy_Computing]]]**：核心目标，旨在实现“数据所有权”与“使用权”分离。',
    '- **[包含:: [[Concept_隔离沙箱]]] / [关联:: [[Concept_AI_监管合规沙箱_NMPA标准]]]**：软件层面的隔离技术，防外不防内。',
    '- **[包含:: [[Concept_可信执行环境_TEE]]]**：基于硬件隔离实现的机密计算技术，提供物理级隔离，保护“计算过程本身”。',
    '- **[包含:: [[Concept_联邦学习_FL]]]**：分布式协作架构，数据不动模型动，但存在“逆向工程”泄露梯度的脆弱性。',
    '',
    '## 核心论点与发现',
    '1. **隐私保护技术分工不同**：隐私计算是目的，沙箱是软件初级手段（围栏），TEE 是硬件物理防御（保险箱），联邦学习是分布式协作架构。',
    '2. **联邦学习存在隐私泄漏敞口**：通过“逆向工程”，攻击者仍可从交换的模型参数（梯度）中还原部分隐私数据。',
    '3. **“TEE + 联邦学习”混合架构成为医疗标准**：必须将 TEE 嵌入到联邦学习的聚合服务器中，利用硬件黑盒确保参数融合过程的绝对安全。',
    '',
    '## 与现有知识库的联系及张力',
    '- 延伸了 **[支持:: [[Concept_物理沙箱]]]** 与 **[支持:: [[Concept_Asymmetric_Evolution]]]** 的技术落地手段。',
    '- **矛盾与张力**：联邦学习常被宣传为解决隐私的银弹，但其“参数逆向工程”的脆弱性与此形成张力，强调必须施加强制硬件约束（TEE）的悲观执行策略。'
]
write_file("Source_research_tee_sandbox_fl_privacy.md", src_lines)

priv_lines = [
    '---',
    'id: "20260419_concept_privacy_comp"',
    'title: "隐私计算 (Privacy-Preserving Computing)"',
    'type: "concept"',
    'domain: "System_Architecture"',
    'topic_cluster: "General"',
    'status: "Active"',
    'epistemic-status: "evergreen"',
    'categories: ["System_Architecture"]',
    'tags: ["数据要素", "数据可用不可见"]',
    'created: "2026-04-19"',
    'updated: "2026-04-19"',
    'sources: ["raw/research/research_tee_sandbox_fl_privacy.md"]',
    '---',
    '# 隐私计算 (Privacy-Preserving Computing)',
    '',
    '## 定义',
    '隐私计算是一种技术体系，旨在不泄露原始数据的前提下实现数据的计算、分析与建模，核心是实现“数据所有权”与“数据使用权”的分离。',
    '',
    '## 技术支柱',
    '隐私计算是整套防御体系的顶层范式，其底层依赖于三大支柱的组合：',
    '- **软件隔离**：[依赖:: [[Concept_AI_监管合规沙箱_NMPA标准]]]',
    '- **硬件加密**：[依赖:: [[Concept_可信执行环境_TEE]]]',
    '- **分布式协作**：[依赖:: [[Concept_联邦学习_FL]]]',
    '',
    '## 医疗场景价值',
    '在医疗“数据城邦”架构中，隐私计算确保高价值医疗数据合规出域，支撑跨院科研及 AI 训练，是医疗大健康领域的刚需底座[^1]。',
    '',
    '[^1]: [[Source_research_tee_sandbox_fl_privacy.md]]'
]
write_file("Concept_隐私计算_Privacy_Computing.md", priv_lines)

tee_lines = [
    '---',
    'id: "20260419_concept_tee"',
    'title: "可信执行环境 (TEE - Trusted Execution Environment)"',
    'type: "concept"',
    'domain: "System_Architecture"',
    'topic_cluster: "General"',
    'status: "Active"',
    'epistemic-status: "evergreen"',
    'categories: ["System_Architecture"]',
    'tags: ["硬件隔离", "机密计算", "Enclave", "物理沙箱"]',
    'created: "2026-04-19"',
    'updated: "2026-04-19"',
    'sources: ["raw/research/research_tee_sandbox_fl_privacy.md"]',
    '---',
    '# 可信执行环境 (TEE - Trusted Execution Environment)',
    '',
    '## 定义',
    '基于硬件隔离实现的机密计算技术，在 CPU 内部划分加密内存区域（如 Enclave），提供物理级隔离。代表产品包括 Intel SGX、ARM TrustZone。',
    '',
    '## 在架构中的生态位',
    'TEE 补足了传统软件隔离沙箱的局限性。软件沙箱依赖于 OS 内核，防外不防内（无法防范内存嗅探等计算过程的泄露）；而 TEE 作为真正的“物理沙箱”，是保护“计算过程本身”隐私的核心硬件锁[^1]。',
    '',
    '## 与联邦学习的混合应用',
    '在医疗极高合规要求的场景下，单纯的 [对比:: [[Concept_联邦学习_FL]]] 存在参数被逆向工程的风险。“TEE + 联邦学习”的混合架构正成为业界标准，利用 TEE 的硬件黑盒保护聚合端的梯度融合[^1]。',
    '',
    '[^1]: [[Source_research_tee_sandbox_fl_privacy.md]]'
]
write_file("Concept_可信执行环境_TEE.md", tee_lines)

fl_lines = [
    '---',
    'id: "20260419_concept_fl"',
    'title: "联邦学习 (Federated Learning)"',
    'type: "concept"',
    'domain: "Artificial_Intelligence"',
    'topic_cluster: "General"',
    'status: "Active"',
    'epistemic-status: "evergreen"',
    'categories: ["Artificial_Intelligence"]',
    'tags: ["分布式训练", "数据不动模型动", "逆向工程攻击"]',
    'created: "2026-04-19"',
    'updated: "2026-04-19"',
    'sources: ["raw/research/research_tee_sandbox_fl_privacy.md"]',
    '---',
    '# 联邦学习 (Federated Learning)',
    '',
    '## 定义',
    '一种“数据不动模型动”的分布式机器学习协作架构，各参与方仅交换模型参数（梯度）而非原始数据，从而在形式上满足数据不出域的要求。',
    '',
    '## 医疗场景应用',
    '完美支撑了“物理隔离，逻辑开放”的 [关联:: [[Concept_Asymmetric_Evolution]]] 解法，是医疗机构参与 AI 联合训练的基础架构。',
    '',
    '## ⚠️ 脆弱性先验 (Gotchas)',
    '**严禁将联邦学习视为绝对安全的银弹。** 联邦学习本身存在隐私泄漏的敞口：通过“逆向工程”，攻击者仍可能从交换的模型参数（梯度）中还原出部分原始数据特征。因此，在医疗高安全场景下，必须与 [必须配合:: [[Concept_可信执行环境_TEE]]] 或差分隐私构成混合架构，通过物理级硬件黑盒（TEE）加固聚合服务器[^1]。若供应商方案仅提联邦学习而无硬件级保障，应视为单点故障 (SPOF) 予以驳回。',
    '',
    '[^1]: [[Source_research_tee_sandbox_fl_privacy.md]]'
]
write_file("Concept_联邦学习_FL.md", fl_lines)

nmpa_lines = [
    '',
    '## 技术纵深与局限性 (2026-04-19 Update)',
    '软件层面的合规沙箱为不可信程序提供了受限环境，但其本质是“软件围栏”，防外不防内，严重依赖操作系统内核安全，难以抵御内存嗅探。为了实现真正的医疗级安全，传统的合规沙箱必须向下演进，与提供“物理保险箱”的 [依赖:: [[Concept_可信执行环境_TEE]]] 结合，构成多层递进的防御体系[^1]。',
    '',
    '[^1]: [[Source_research_tee_sandbox_fl_privacy.md]]'
]
nmpa_path = os.path.join(wiki_dir, "Concept_AI_监管合规沙箱_NMPA标准.md")
if os.path.exists(nmpa_path):
    append_file("Concept_AI_监管合规沙箱_NMPA标准.md", nmpa_lines)

ovv_lines = [
    '---',
    'id: "20260419_overview"',
    'title: "Vector Lake Overview"',
    'type: "synthesis"',
    'domain: "Medical_IT"',
    'topic_cluster: "General"',
    'status: "Active"',
    'epistemic-status: "evergreen"',
    'categories: ["System_Architecture"]',
    'tags: ["全局概览", "医疗IT"]',
    'created: "2026-04-19"',
    'updated: "2026-04-19"',
    'sources: []',
    '---',
    '# Vector Lake Overview',
    '',
    'Vector Lake (V7.2) 是一座以医疗信息化 (HIT) 为主轴、兼顾战略架构与 AI 基础设施的深层认知网络。本知识库旨在通过结构化压缩行业变量，揭示医疗 IT 市场的结构性拐点（如 DRG/DIP 控费驱动下的系统重构），并为 AI Agent 在医疗场景的临床决策与院内运营落地提供高维推演支撑。',
    '',
    '当前，医疗 IT 正从单纯的“软件交付”向“数据城邦”及“主权 AI 私有化部署”跃迁。核心讨论焦点集中在技术层面的物理隔离与逻辑重构上。例如，通过融合隔离沙箱、可信执行环境 (TEE) 和联邦学习 (FL) 的多重隐私计算架构，打造符合 NMPA 标准的合规防护网；在应用层，强调“洁净室重构”和责任承保模型，剥离 AI 幻觉与系统脆弱性，重塑医疗临床的解释权。',
    '',
    '伴随技术的演进，知识库持续关注组织、人才与涌现性冲突。从医疗大模型、智能体编排 (Agentic Workflows) 的普及，到它在真实医疗质量管理、疾病诊疗网络及县域医共体下引发的“审计反转”与“自动化偏见防御”，Vector Lake 坚持悲观执行与非对称审计策略，致力于为头部医疗企业和医疗机构构筑牢不可破的逻辑底座。'
]
write_file("overview.md", ovv_lines)
write_file("Synthesis_overview.md", ovv_lines)

now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
log_entry = [f"## [{now}] Ingest | Source_research_tee_sandbox_fl_privacy.md"]
append_file("log.md", log_entry)
append_file("Synthesis_log.md", log_entry)

print("Files written successfully")
