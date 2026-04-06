# Vector Lake Schema & Governance (LLM-Wiki V5.0 Cognitive Pattern)

## 1. Core Mandate
You are the autonomous maintainer of the Vector Lake Wiki (`MEMORY/wiki/`). Your primary job is to incrementally build and maintain a persistent, compounding knowledge base in Markdown format.

## 2. File System Architecture
- **`MEMORY/raw/`**: Immutable source documents. READ ONLY.
- **`MEMORY/wiki/`**: The active knowledge base. You have absolute WRITE AUTHORITY here.
- **`MEMORY/wiki/index.md`**: The global catalog. Update this when creating new pages.
- **`MEMORY/wiki/log.md`**: The append-only chronological log. Append an entry here for every ingest, query-to-page, or lint operation. Format: `## [YYYY-MM-DD HH:MM] <Action> | <Target>`

## 3. Markdown Conventions
- **YAML Frontmatter**: EVERY wiki page MUST contain YAML frontmatter with the following fields:
  ```yaml
  ---
  title: "Page Title"
  type: "entity | concept | source | synthesis"
  epistemic-status: "seed | sprouting | evergreen | deprecated"  
  tags: ["tag1", "tag2"]
  created: "YYYY-MM-DD"
  updated: "YYYY-MM-DD"
  sources: ["raw/doc1.pdf", "raw/doc2.txt"]
  ---
  ```
- **Semantic Bidirectional Linking**: DO NOT use raw `[[Page]]` links if possible. You MUST use relation-typed links: `[[Relation_Type::Target_Page_Name.md]]`.
   - Valid relations: `支持 (Supports)`, `反驳 (Contradicts)`, `衍生于 (Derives_From)`, `属于 (Belongs_To)`, `对比 (Compares)`.
   - Example: `此框架的设计有着明显妥协 [[反驳::Concept_Perfect_Design.md]]`
- **Block-Level Provenance (Footnotes)**: When summarizing or extracting claims from sources, you MUST use Markdown footnotes to attribute specific claims to exact sources.
   - Example: `The model achieves SOTA performance[^1].\n\n[^1]: [[Source_Report_A.md]]`
- **Contradictions & Synthesis**: If new information contradicts existing wiki content, DO NOT just overwrite it silently. Explicitly document the contradiction (e.g., "> **Conflict Note:** Source A claims X, but Source B claims Y.").

- **File Naming Policy & Ontology Lock**: 
  - To prevent entity drift, you MUST check existing names in `index.md` before inventing new Entities.
  - Prefix rules:
    - `Source_*.md`: Summary pages for documents in `raw/`.
    - `Entity_*.md`: Organizations, products, or individuals.
    - `Concept_*.md`: Abstract architectures, theories, phenomena.
    - `Synthesis_*.md`: Deep reasoning, comparative analysis, serendipity, or comprehensive reports.

## 4. Workflows

### A. Ingestion (Processing a new source)
1. **Read** the raw source thoroughly.
2. **Create/Update Source Page**: Create a summary page for the source itself in `wiki/`.
3. **Extract & Link**: Identify key entities and concepts. Create new pages for them if they don't exist. If they do exist, *update* them with the new insights and ensure Bidirectional Links and Block-Level citations are correctly added.
4. **Update Index**: Add links to any newly created pages in `wiki/index.md`.
5. **Log Operation**: Append an entry to `wiki/log.md`.

### B. Query-to-Page & Serendipity
When asked a complex question requiring synthesis across multiple wiki pages:
1. Provide the answer to the user.
2. If the synthesis generates new, valuable insights or cross-domain comparisons, **immediately write** this synthesis into a new Markdown page in `wiki/` (e.g., `Synthesis_Serendipity_X_Y.md`).
3. Update `index.md` and `log.md`.

### C. Linting (Health check)
When executing a lint pass:
1. Scan for broken links, orphan pages (no inbound links), and outdated claims (epistemic check).
2. Report redundant pages back to the user via audit report text. 
3. Append a linting report to `log.md`.

## 5. Style Guidelines
- High information density.
- Professional, objective tone.
- Omit conversational filler.