---
name: memory-update
metadata:
  version: 11.20.0
  tier: action-allowed
description: Persist one user-approved operational memory through Vector Lake's governed payload-file contract.
---

# Operational memory update

Use `update_operational_memory` from `vector-lake-mcp` only when the user explicitly asks to persist durable memory.

1. Select exactly one supported `memory_type`: `preference`, `decision`, `fact`, or `task_state`.
2. Draft the exact payload from verifiable current evidence and show it to the user.
3. Wait for explicit approval of both the type and payload.
4. Stage only that approved text in an authorized temporary file and pass its absolute path as `payload_file`.
5. Report the returned receipt and identifier. Do not claim persistence if the tool rejects or only previews the payload.

Do not write an alternate MEMORY ledger when the governed tool is unavailable.
