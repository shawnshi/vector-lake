import os
import json

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

files_to_read = [
    "Source_诊断的不确定性：当我们在医疗中引入随机性引擎.md",
    "Source_跨越“概率鸿沟”：医疗AI从“技术实验”到“核心生产力”的工程跃迁.md",
    "Source_跨越概率鸿沟：基于 OpenAI Frontier 架构的 AI 原生医院建设与问责重构 (2026)_2026-02-07.md",
    "Source_软件的三次浪潮与智能体时代的黎明：范式、未来与前沿的深度解析.md",
    "Source_软件的终局与医疗逻辑的重构：Nadella 的“三位一体”预言深处-20260420.md",
    "Entity_Andrej_Karpathy.md",
    "Concept_Agentic_Hospital.md",
    "Concept_三位一体架构.md",
    "Concept_表单监狱.md",
    "Concept_认知摩擦_Cognitive_Friction.md",
    "overview.md",
    "log.md"
]

results = {}

for filename in files_to_read:
    path = os.path.join(wiki_dir, filename)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                results[filename] = f.read()
        except Exception as e:
            results[filename] = f"ERROR: {str(e)}"
    else:
        results[filename] = "NOT_FOUND"

print(json.dumps(results, ensure_ascii=False, indent=2))
