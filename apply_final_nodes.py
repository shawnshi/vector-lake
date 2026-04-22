import os
import datetime

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def write_file(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.strip() + '\n')

nodes = [
    {
        "file": "Entity_Alex_Zhavoronkov.md",
        "content": r"""
---
id: \"20260421_p4g5h6\"
title: \"Entity_Alex_Zhavoronkov\"
type: \"entity\"
domain: \"Biomedicine\"
topic_cluster: \"Leadership\"
status: \"Active\"
alignment_score: 82
epistemic-status: \"seed\"
categories: [\"Strategy_and_Business\"]
tags: [\"Insilico_Medicine\", \"AI制药\", \"长寿研究\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md\"]
---

# Alex Zhavoronkov

## 基本信息
英矽智能 (Insilico Medicine) 创始人兼首席执行官。AI 制药领域的先驱。

## 贡献
他领导了首个完全由 AI 发现并设计进入临床试验的候选药物，是 AI + 药物研发工程化的重要推动者[^1]。

## 关联
- [属于:: [[Entity_Insilico_Medicine]]]

[^1]: [[Source_全球医疗AI独角兽格局：市场领导者、技术平台与投资逻辑分析报告.md]]
"""
    },
    {
        "file": "Entity_毛新生.md",
        "content": r"""
---
id: \"20260421_p5i6j7\"
title: \"Entity_毛新生\"
type: \"entity\"
domain: \"Medical_IT\"
topic_cluster: \"Leadership\"
status: \"Active\"
alignment_score: 85
epistemic-status: \"seed\"
categories: [\"Strategy_and_Business\"]
tags: [\"数坤科技\", \"AI影像\", \"中国医疗AI\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/从数字化到智能化：中国医疗信息化的过去、现在与未来.md\"]
---

# 毛新生 (Mao Xinsheng)

## 基本信息
数坤科技董事长。曾任 IBM 中国开发中心首席技术官。

## 角色
他代表了中国本土医疗 AI 影像从单点算法向大规模临床系统工程转化的领导力量[^1]。

## 关联
- [属于:: [[Entity_数坤科技]]]

[^1]: [[Source_从数字化到智能化：中国医疗信息化的过去、现在与未来.md]]
"""
    },
    {
        "file": "Concept_National_AI_Pilot_Base.md",
        "content": r"""
---
id: \"20260421_c9d0e1\"
title: \"Concept_National_AI_Pilot_Base\"
type: \"concept\"
domain: \"Medical_IT\"
topic_cluster: \"Policy\"
status: \"Active\"
alignment_score: 95
epistemic-status: \"seed\"
categories: [\"Strategy_and_Business\"]
tags: [\"中试基地\", \"医疗AI\", \"沙盒监管\"]
created: \"2026-04-21\"
updated: \"2026-04-21\"
sources: [\"raw/article/digitalhealthobserve/医疗AI的“十五五”：十五五”时期大型公立医院数字化建设迈向高质量发展的重点与路径战略分析.md\"]
---

# 国家人工智能应用中试基地 (National AI Pilot Base)

## 定义
在“十五五”规划背景下提出的政策性基础设施，旨在为医疗 AI 产品提供受控的、模拟真实临床环境的“中试”沙箱。

## 作用
解决 AI 产品在正式进入大规模临床应用前的合规验证、责任边界界定及医保准入测试等问题[^1]。

## 关联
- [支持:: [[Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md]]]
- [关联:: [[Concept_制度深潜]]]

[^1]: [[Source_医疗AI的“十五五”：从技术狂欢到制度深潜.md]]
"""
    }
]

for node in nodes:
    write_file(node["file"], node["content"])

print("Final nodes applied.")
