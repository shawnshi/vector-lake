import os

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

def write_f(filename, lines):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

write_f("Concept_Agentic_Hospital.md", [
    '---', 'id: "20260406_rukcpe"', 'title: "Agentic Hospital (智能体医院)" ', 'type: "concept"',
    'domain: "Healthcare_IT"', 'topic_cluster: "General"', 'status: "Active"', 'alignment_score: 100',
    'epistemic-status: "evergreen"', 'categories: ["System_Architecture", "Healthcare_IT"]',
    'tags: ["智能体", "医院数字化", "Agentic AI", "顶层设计", "OpenAI_Frontier"]',
    'created: "2026-04-06"', 'updated: "2026-04-22"',
    'sources: ["raw/article/digitalhealthobserve/医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md", "raw/article/digitalhealthobserve/跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md"]', '---',
    '# Agentic Hospital (智能体医院)', '', '## 定义与本质',
    '智能体医院（Agentic Hospital）是医疗数智化下半场的关键范式。它要求彻底从“以表单流转为中心”转向“以意图与协作为中心”[^1]。', '',
    '## 核心架构 (2026-04-22 Update)',
    '- **AI 原生底座**：采用 **[属于:: [[Entity_OpenAI_Frontier]]]** 架构作为医院的“核心操作系统”，实现全院级的实时推理与意图对齐[^2]。',
    '- **Teacher-Student 蒸馏**：利用 **[依赖:: [[Concept_Teacher_Student_Distillation_教师-学生蒸馏架构]]]** 确保云端逻辑在边缘侧 SLM 的可靠执行，对冲“数字黑暗”风险[^2]。',
    '- **HITL 2.0 审计**：通过 **[关联:: [[Concept_HITL_2.0]]]** 协议实现责任的确权与逻辑路径的实时审计[^2]。', '',
    '## 战略意义',
    '该范式宣告了“表单驱动”交互的终结，通过 **[关联:: [[Concept_三位一体架构]]]** 实现交互维度的极度坍缩，使医生回归临床决策者角色。', '',
    '[^1]: [[Source_医疗数智化的下半场：从‘套壳聊天’到‘智能体医院’的范式坍塌与重构_20260304_final.md]]',
    '[^2]: [[Source_跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md]]'
])

print("Agentic Hospital Updated")
