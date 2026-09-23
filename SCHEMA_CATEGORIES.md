# Vector Lake Schema Categories (受控词表)

This document defines the rigid ontology for the `categories` field in Vector Lake V7.0.
All entities, concepts, and synthesis logic nodes must belong to one of these macro-domains. 
Folksonomy and fine-grained labels should go into the `tags` field instead.

## Allowed Categories
- `Uncategorized`: Default category for imported legacy nodes ONLY. **Do not use for new nodes.**
- `Artificial_Intelligence`: AI models, architectures, AI/Agent methodology, training algorithms (e.g. LLM, RAG, RL, Agent Architecture).
- `Healthcare_IT`: Digital health systems, hospital implementations, electronic health records (e.g. EHR, HIS, Epic).
- `Strategy_and_Business`: Market analysis, corporate strategy, go-to-market, market intelligence, business strategy.
- `System_Architecture`: Software engineering, distributed networks, technical systems, data architecture, development methodology.
- `Philosophy_and_Cognitive`: Knowledge management, epistemology, human-computer interaction, dialectics.
- `Biomedicine`: Clinical science, pharmacology, molecular biology.
- `Policy_and_Governance`: Policy analysis, regulations, industry guidelines, and compliance frameworks.
- `Entities_and_Actors`: Real-world actors including organizations, people, researchers, companies, and industry ecosystems.

## Enforcement
Agents should generally use the above categories. However, if a concept fundamentally falls outside these bounds and warrants a new domain, Agents **may** propose new categories using the `propose_schema_mutation` MCP tool. 
Proposed categories will be logged in the governance queue for review. Once approved, this document will be automatically updated.

## Domain Facet (受控词表)

`categories` is the macro axis; `domain` is a second, narrower facet, and it is **also** closed. The
write gate accepts only these 9 values **on a new node**; existing pages keep the value they carry
until the migration pass below runs.

- `Medical_IT` — 医疗信息化：产品、项目、政策落地、厂商
- `Artificial_Intelligence` — 大模型、Agent、训练与推理方法
- `System_Architecture` — 软件与分布式系统、数据架构、工程方法
- `Enterprise_Software` — 企业软件与平台产品、交付与运维
- `Strategy_and_Business` — 战略、市场、投资、商业模式
- `Policy_and_Governance` — 政策、监管、合规
- `Biomedicine` — 临床与基础医学
- `Cognitive_Science` — 认知、知识管理、哲学
- `General` — 尚未收窄。不是兜底垃圾桶：能收窄就不要用

迁移现状（2026-09-23 普查）：存量 7 425 个非生成物节点携带 **192 个不同 domain 值**，其中
67 个只用在 1 个页面上，并且近义并存是明摆着的——同一个主题有 `AI_Industry` / `AI_Research` /
`General_AI` / `AI_Architecture` / `AI` / `Artificial_Intelligence` 六个值，`Enterprise_Software` /
`Enterprise_IT` / `Enterprise_Architecture` 三个值，`Digital_Transformation` 与
`Digital Transformation` 两种拼写。`tool_lint` 在 “15. Domain Vocabulary” 一节报告它们；把 192 个
值映射到这 9 个是本文件之外的一次独立、可复核的批处理，不随本词表翻车式重写全部页面。

**【强制】入湖收容协议 (Ingest Protocol):** 
在任何执行知识入湖 (Ingest) 或合并的环节，**绝对禁止使用 `Uncategorized`** 以及任何自创分类。如果遇到边缘概念，必须向现有的 9 大宏观分类进行降维对齐，或者触发 `propose_schema_mutation` 工具报警。
