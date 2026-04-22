import os
wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

def append_to_file(filename, lines):
    path = os.path.join(wiki_dir, filename)
    if os.path.exists(path):
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n' + '\n'.join(lines) + '\n')

msl_lines = [
    '',
    '## 对冲概率风险 (2026-04-22 Update)',
    '资料进一步明确了 MSL 作为“固态物理围栏”在对冲 [关联:: [[Concept_Probability_Machine]]] (概率机器) 风险中的决定性作用。通过符号化的语义对齐，MSL 将 LLM 的概率输出强行映射到医疗行业的确定性空间，从而剥离算法幻觉，实现架构级的安全防护[^1]。',
    '',
    '[^1]: [[Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质.md]]'
]
append_to_file("Concept_MSL_医疗语义层.md", msl_lines)

hosp_lines = [
    '',
    '## 落地探测器逻辑 (2026-04-22 Update)',
    '在智能体医院的落地过程中，引入 [关联:: [[Concept_Power_Interest_Intersection]]] (权力-利益交汇点) 作为探测器。AI 原生医院的建设不仅仅是技术叠加，更是对医院内部权力结构的微调。必须通过 [关联:: [[Concept_Pain_Index]]] (痛苦指数) 模型优先筛选决策者真实痛苦且具备高支付意愿的黄金场景，才能打破系统的免疫排斥[^1]。',
    '',
    '[^1]: [[Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点.md]]'
]
append_to_file("Concept_Agentic_Hospital.md", hosp_lines)

print("SUCCESS: MSL and Agentic Hospital updated.")

