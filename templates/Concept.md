# Entity Page Skeleton (`Concept_*.md` and the other seven entity prefixes)

Reference skeleton for the dual-schema shape declared in `schema.md` §4.A. Nothing in the code
reads this file; the section names below are the ones the corpus already uses on 5 370 pages
(100% of `concept`/`product`/`vendor`/`person`/`event`/`institution`/`policy`/`standard`), so this
file records the de-facto template rather than changing it.

Two variants of the same two headings coexist in the corpus: the annotated form
(`## 1. 编译事实 (Compiled Truth - READ MODEL)`, 3 999 pages) and the bare Chinese form
(`## 1. 编译事实`, 1 503 pages). Use the annotated form: it is the one `schema.md` declares, and
the parser matches on `编译事实`/`Compiled Truth` either way.

```markdown
---
id: 20260923_ab12cd
title: Concept_<名称>
aliases: []
type: concept                  # concept | vendor | institution | product | person | event | policy | standard
domain: System_Architecture    # one of the 9 canonical facets in SCHEMA_CATEGORIES.md
status: Active
epistemic-status: seed         # sprouting | evergreen | seed
categories: [System_Architecture]   # exactly one macro-domain
tags: []
sources: []
strategic_scope: core
evidence_tier: primary
created: '2026-09-23'
updated: '2026-09-23'
---

# <名称>

## 1. 编译事实 (Compiled Truth - READ MODEL)

*[System Directive: This section represents the LATEST consensus. NO historical narrative here. NO marketing fluff.]*

<50 字终极定义，ELI5 风格。> (Last Reshaped: [[2026-09-23]] timeline anchor)

### <该 type 允许的 H3 槽位之一，见 schema.md §4.A>

- [[Entity_Name]] <事实陈述，句内必须复述实体名，不得用代词> (Source: [[Source_X]], p.4)

## 2. 证据时间线 (Timeline - EVENT STORE)

- [2026-09-23] [Observation] <事件描述> [validates:: [[Target_Entity]]] (Source: [[Source_X]])
```

Rules that apply to this page:

1. Section 1 is overwritten in place; section 2 is append-only. Never delete timeline entries.
2. H3 headings are closed per type (`VALID_H3_SLOTS`) — inventing one is a fatal AST error.
3. Every line carries an inline `(Source: [[Source_*]])` anchor; every bullet restates the entity
   name (no pronouns).
4. If the page declares `tension_edges`, it must also carry
   `### 认知张力与未决争议 (Controversies & Tensions)` in section 1.
5. `categories` is exactly one macro-domain and `domain` one of the 9 canonical facets. A new page
   with `Uncategorized` or an invented category/domain is refused by the write gate.
