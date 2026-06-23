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

**【强制】入湖收容协议 (Ingest Protocol):** 
在任何执行知识入湖 (Ingest) 或合并的环节，**绝对禁止使用 `Uncategorized`** 以及任何自创分类。如果遇到边缘概念，必须向现有的 9 大宏观分类进行降维对齐，或者触发 `propose_schema_mutation` 工具报警。
