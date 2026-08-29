---
name: resolve
metadata:
  version: 11.20.0
  tier: action-allowed
description: Resolve one current Vector Lake governance item after review and explicit approval of the exact action.
---

# Governance resolution

Use `review_governance_list` to verify the current item, then `resolve_governance_item` from `vector-lake-mcp` for the authorized mutation.

1. Preserve the stable `item_id`; do not rely only on a display index.
2. Validate the requested resolution as `skip`, `create`, `merge`, or `acknowledge` and show its expected effect.
3. If an outcome manifest is needed, stage the exact JSON in an authorized temporary file and present it with the proposal.
4. Wait for explicit user approval before calling `resolve_governance_item`.
5. Submit the approved `item_id`, resolution, and optional `payload_file` unchanged, then report the receipt.

A changed item version, target, or manifest requires renewed approval.
