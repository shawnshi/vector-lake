---
name: gc
metadata:
  version: 11.20.0
  tier: action-allowed
description: Preview and explicitly confirm pruning of old isolated or orphaned Vector Lake entities.
---

# Orphan garbage collection

Use `gc_vector_lake` from `vector-lake-mcp` with a bounded `days` value.

1. Always begin with `dry_run=True` and no `orphan_confirmation`.
2. Present every candidate and the exact fingerprint returned by the current preview.
3. Require explicit user approval of that preview before deletion.
4. Apply with `dry_run=False` and the unchanged fingerprint as `orphan_confirmation`.
5. If the candidate set or fingerprint changes, generate a new preview and obtain new approval.

Report the final receipt and residual orphan count; a dry run is never proof of deletion.
