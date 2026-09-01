---
name: doctor
metadata:
  version: 11.20.0
  tier: read-only
description: Diagnose Vector Lake dependencies, effective paths, infrastructure health, and semantic readiness.
---

# Runtime diagnosis

Call the `doctor_vector_lake` tool from `vector-lake-mcp` with `mode="quick"` first and report its observed results. Run `doctor_vector_lake(mode="deep")` only when the user explicitly requests deep projection or semantic diagnosis; deep mode is a long-running heavy task and may exceed a host's synchronous MCP timeout.

- For quick mode, distinguish failed checks and warnings, preserve `semantic_readiness.status=not_checked`, and do not imply that deep projection consistency was verified.
- For deep mode, distinguish failed checks, warnings, unavailable optional capabilities, infrastructure summary, and semantic readiness.
- Preserve the effective MEMORY, raw, Wiki, meta, database, and index paths shown by the tool.
- Do not turn a successful process exit or a single green check into an overall healthy verdict.
- Do not install dependencies, edit configuration, restart processes, or repair data unless the user separately authorizes that action.
- If the diagnostic itself fails, include the exact failure or incident identifier and leave the affected checks unverified.
