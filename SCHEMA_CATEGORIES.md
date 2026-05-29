# Vector Lake Schema Categories (受控词表)

This document defines the rigid ontology for the `categories` field in Vector Lake V7.0.
All entities, concepts, and synthesis logic nodes must belong to one of these macro-domains. 
Folksonomy and fine-grained labels should go into the `tags` field instead.

## Allowed Categories
- `Uncategorized`: Default category for imported legacy nodes.
- `Artificial_Intelligence`: AI models, architectures, agent methodologies (e.g. LLM, RAG, RL).
- `Healthcare_IT`: Digital health systems, hospital implementations, electronic health records (e.g. EHR, HIS, Epic).
- `Strategy_and_Business`: Market analysis, corporate strategy, go-to-market.
- `System_Architecture`: Software engineering, distributed networks, technical systems.
- `Philosophy_and_Cognitive`: Knowledge management, epistemology, human-computer interaction, dialectics.
- `Biomedicine`: Clinical science, pharmacology, molecular biology.

## Enforcement
Agents should generally use the above categories. However, if a concept fundamentally falls outside these bounds and warrants a new domain, Agents **may** propose new categories using the `propose_schema_mutation` MCP tool. 
Proposed categories will be logged in the governance queue for review. Once approved, this document will be automatically updated.
