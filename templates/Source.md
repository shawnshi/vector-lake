# Source Page Skeleton (`Source_*.md`)

Reference skeleton for the canonical source page. The ingest prompt points here; nothing in the
code reads this file, so it is a contract you can follow rather than one a gate enforces.

Why it exists: `schema.md` §4.B declares `Source_*` free-form, and the corpus took that literally
— 1 798 source pages carry **3 200 distinct H2 headings**, 2 810 of them used exactly once, with
six competing names for the same section (`Source Summary`, `Raw Preview`, `来源核验`, `概要摘录`,
`核心摘要`, `结构化摘录`). The three headings below are the ones the corpus already uses most
(171 / 171 / 147 pages), so adopting them costs nothing and gives the retrieval layer something to
depend on. Enforcing them would mean rewriting 1 798 pages, which is why it is not enforced yet.

```markdown
---
id: 20260923_ab12cd
title: Source_<原文件名或文章标题>
aliases: []
type: source
domain: Medical_IT            # one of the 9 canonical facets in SCHEMA_CATEGORIES.md
status: Active
epistemic-status: seed        # sprouting | evergreen | seed
categories: [Healthcare_IT]   # exactly one macro-domain, never Uncategorized on a new page
tags: []
sources: []
strategic_scope: core
evidence_tier: primary
created: '2026-09-23'
updated: '2026-09-23'
---

## 来源核验

- Raw 路径：`raw/<目录>/<文件>.md`
- Raw SHA-256：`<hex>`
- 以下内容仅由该 raw 文件确定性抽取；未补写 raw 中不存在的事实。

## 概要摘录

<原文摘要，保留作者主张与限定条件；区分事实、来源主张与推断。>

## 结构化摘录

<按主题分节摘录，必要时保留原文章节标题，但不要新建第四个顶层 H2。>

## Graph Integration

- [related_to:: [[Target_Entity]]] <该来源对目标实体的一句话贡献。> (confidence: 0.9)
```

Rules that do apply to this page:

1. The filename is the claimed `canonical_name` and is the only page that may carry this source's
   provenance; exactly one such page per ingest.
2. `categories` must be a single macro-domain, and `domain` one of the 9 canonical facets.
3. Heading budget: three H2 sections plus `Graph Integration`. If you need a fourth, the content
   probably belongs on the target entity page instead.
