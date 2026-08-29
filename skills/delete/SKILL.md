---
name: delete
metadata:
  version: 11.20.0
  tier: action-allowed
description: Preview and, after explicit approval, cascade-delete one exact raw source and its derived Vector Lake artifacts.
---

# Source deletion

Use `delete_source` from `vector-lake-mcp`.

1. Resolve one exact `raw_path`; never broaden it to a directory, wildcard, or inferred neighboring source.
2. Call `delete_source(raw_path=..., dry_run=True)` and present the complete blast radius.
3. Stop for explicit user approval of that target and current preview.
4. Only then call the same tool with the identical `raw_path` and `dry_run=False`.
5. Report the mutation receipt and any residual artifacts. Do not claim deletion from the preview or from file absence alone.

A changed target or blast radius invalidates prior approval and requires a new preview.
