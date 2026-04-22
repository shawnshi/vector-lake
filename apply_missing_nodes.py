import os
import datetime
import random
import string

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
os.makedirs(wiki_dir, exist_ok=True)

def generate_id():
    today = datetime.datetime.now().strftime("%Y%m%d")
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{today}_{rand}"

def write_node(filename, content):
    path = os.path.join(wiki_dir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.strip() + '\n')

# 1. Entity_DISA
disa_content = """---
id: "{id}"
title: "DISA (美国国防信息系统局)"
type: "entity"
domain: "Strategy_and_Business"
topic_cluster: "Data_Strategy"
status: "Active"
alignment_score: 85
epistemic-status: "evergreen"
categories: ["System_Architecture"]
tags: ["数据生命周期", "IT基础设施", "DoD"]
created: "{date}"
updated: "{date}"
sources: ["raw/article/digitalhealthobserve/高质量医疗数据集建设：现状、挑战与未来路径.md"]
---

# 美国国防信息系统局 (DISA - Defense Information Systems Agency)

## 职能
为美国国防部（DoD）提供全球范围内的 IT 基础设施、通信支持与信息安全保障。

## 数据治理贡献
提供了权威的数据生命周期管理框架，将数据流转划分为采集、处理、分析、存储、共享、使用、归档、销毁 8 个阶段。该框架为医疗数据集的工业化建设提供了参考模型[^1]。

[^1]: [[Source_高质量医疗数据集建设：现状、挑战与未来路径.md]]
""".format(id=generate_id(), date=datetime.datetime.now().strftime('%Y-%m-%d'))
write_node("Entity_DISA.md", disa_content)

# 2. Concept_Responsibility_Black_Hole
bh_content = """---
id: "{id}"
title: "责任黑洞 (Responsibility Black Hole)"
type: "concept"
domain: "Philosophy_and_Cognitive"
topic_cluster: "Ethics"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
categories: ["Philosophy_and_Cognitive"]
tags: ["问责制", "AI伦理", "医疗法律"]
created: "{date}"
updated: "{date}"
sources: ["raw/article/医疗大语言模型应用二十讲/模块一：第一性原理 —— 洞见权力、风险与成本的本质.md"]
---

# 责任黑洞 (Responsibility Black Hole)

## 定义
在高度自动化或 AI 参与决策的系统中，由于系统行为的复杂性与随机性，导致当错误发生时，无法在现有的法律与伦理框架内清晰地界定责任归属的现象。

## 医疗表现
由于 AI 本质上是 [关联:: [[Concept_Probability_Machine]]]，其输出具有不可预测性。如果 AI 给出错误诊断，责任应由医生（使用者）、厂商（开发者）还是数据提供者承担？这种模糊性构成了医疗 AI 规模化落地的核心阻力[^1]。

[^1]: [[Source_模块一：第一性原理 —— 洞见权力、风险与成本的本质.md]]
""".format(id=generate_id(), date=datetime.datetime.now().strftime('%Y-%m-%d'))
write_node("Concept_Responsibility_Black_Hole.md", bh_content)

# 3. Concept_Power_Interest_Intersection
intersect_content = """---
id: "{id}"
title: "权力-利益交汇点 (Power-Interest Intersection)"
type: "concept"
domain: "Strategy_and_Business"
topic_cluster: "Strategy"
status: "Active"
alignment_score: 98
epistemic-status: "sprouting"
categories: ["Strategy_and_Business"]
tags: ["落地逻辑", "组织博弈", "利益相关者"]
created: "{date}"
updated: "{date}"
sources: ["raw/article/医疗大语言模型应用二十讲/模块二：场景为王 —— 寻找\"权力-利益\"的交汇点.md"]
---

# 权力-利益交汇点 (Power-Interest Intersection)

## 定义
指一个技术项目能够同时触达组织的“核心利益”（如营收、合规、成本）与“核心权力者”的个人及部门权力的重叠区域。

## 场景猎手逻辑
成功的医疗 AI 落地不应看技术先进性，而应优先嗅探以下区域：
1. **决策者痛苦区**：如医保拒付导致的直接收入损失。
2. **权力扩张区**：如通过 AI 平台实现跨院区、跨科室的质量统筹管理。
3. **法律避险区**：如通过 AI 审计降低医疗事故法律风险[^1]。

[^1]: [[Source_模块二：场景为王 —— 寻找“权力-利益”的交汇点.md]]
""".format(id=generate_id(), date=datetime.datetime.now().strftime('%Y-%m-%d'))
write_node("Concept_Power_Interest_Intersection.md", intersect_content)

# 4. Concept_Workflow_Stickiness
sticky_content = """---
id: "{id}"
title: "工作流粘性 (Workflow Stickiness)"
type: "concept"
domain: "Strategy_and_Business"
topic_cluster: "Product_Strategy"
status: "Active"
alignment_score: 97
epistemic-status: "sprouting"
categories: ["Strategy_and_Business"]
tags: ["护城河", "迁移成本", "系统融合"]
created: "{date}"
updated: "{date}"
sources: ["raw/article/医疗大语言模型应用二十讲/模块三：方案构建 —— 从\"功能设计\"到\"系统融合\"与\"风险控制\".md"]
---

# 工作流粘性 (Workflow Stickiness)

## 定义
指 AI 产品通过深度嵌入用户的核心、高频工作路径，从而形成的极高的用户依赖度与迁移成本。

## 竞争本质
在算法优势快速消退的时代，真正的护城河不在于模型本身，而在于 AI 与 [属于:: [[Entity_Epic_Systems]]] 或 [属于:: [[Entity_Winning_Health]]] 等核心系统的物理咬合程度。通过“静默监听"与“自动反馈”，AI 成为工作流中不可或缺的组件[^1]。

[^1]: [[Source_模块三：方案构建 —— 从"功能设计"到"系统融合"与"风险控制".md]]
""".format(id=generate_id(), date=datetime.datetime.now().strftime('%Y-%m-%d'))
write_node("Concept_Workflow_Stickiness.md", sticky_content)

print("SUCCESS: Missing nodes added.")
