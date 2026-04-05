# Vector Lake Schema & Governance (LLM-Wiki Pattern)

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
  tags: ["tag1", "tag2"]
  created: "YYYY-MM-DD"
  updated: "YYYY-MM-DD"
  sources: ["raw/doc1.pdf", "raw/doc2.txt"]
  ---
  ```
- **Bidirectional Linking**: Use Obsidian-style wikilinks `[[Page Name]]` whenever referring to an existing entity, concept, or source. This is critical for graph rendering.
- **Contradictions & Synthesis**: If new information contradicts existing wiki content, DO NOT just overwrite it silently. Explicitly document the contradiction (e.g., "> **Conflict Note:** Source A claims X, but Source B claims Y.").

## 4. Workflows

### A. Ingestion (Processing a new source)
When triggered to process a new source from `MEMORY/raw/`:
1. **Read** the raw source thoroughly.
2. **Create/Update Source Page**: Create a summary page for the source itself in `wiki/`.
3. **Extract & Link**: Identify key entities and concepts. Create new pages for them if they don't exist. If they do exist, *update* them with the new insights and ensure bidirectional links are added.
4. **Update Index**: Add links to any newly created pages in `wiki/index.md`.
5. **Log Operation**: Append an entry to `wiki/log.md`.

### B. Query-to-Page (Synthesizing answers)
When asked a complex question requiring synthesis across multiple wiki pages:
1. Provide the answer to the user.
2. If the synthesis generates new, valuable insights or comparisons, **immediately write** this synthesis into a new Markdown page in `wiki/` (e.g., `wiki/Synthesis_Comparison_A_vs_B.md`).
3. Update `index.md` and `log.md`.

### C. Linting (Health check)
When executing a lint pass:
1. Scan for broken links, orphan pages (no inbound links), and outdated claims.
2. Refactor and merge redundant pages.
3. Append a linting report to `log.md`.

## 5. Style Guidelines
- High information density.
- Professional, objective tone.
- Omit conversational filler.