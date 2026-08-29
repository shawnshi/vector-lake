---
name: audit
metadata:
  version: 11.20.0
  tier: action-allowed
description: Preview Vector Lake topology debt and apply only an exact, explicitly confirmed audit plan.
---

# Graph topology audit

Use the `trigger_audit_graph` tool from `vector-lake-mcp`.

- Start with `dry_run=True`. Report the observed topology gaps, proposed queue changes, and the complete confirmation fingerprint returned by that preview.
- A preview is evidence, not authorization. Do not create governance items or update operational memory during the review.
- Call `dry_run=False` only after the user explicitly approves that current preview; pass its fingerprint unchanged as `confirmation`.
- If the graph or preview changes, discard the old approval and generate a new preview.
- Report the tool receipt and distinguish applied, skipped, and failed items. Never infer a successful mutation from the plan alone.
