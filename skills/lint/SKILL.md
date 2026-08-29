---
name: lint
metadata:
  version: 11.20.0
  tier: action-allowed
description: Inspect Vector Lake Wiki quality and optionally apply only explicitly authorized lint fixes.
---

# Wiki lint

Use `lint_vector_lake` from `vector-lake-mcp`.

- Run `auto_fix=False` for the initial audit and present every issue category and affected page.
- Do not treat a lint preview as permission to modify Wiki files.
- Call `auto_fix=True` only after the user explicitly approves the proposed fixes and scope.
- After an authorized fix, rerun with `auto_fix=False` and report fixed, remaining, and newly surfaced issues.
- Never silently widen lint repair into schema migration, merge, deletion, or content rewriting.
