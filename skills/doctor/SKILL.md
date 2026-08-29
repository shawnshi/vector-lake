---
name: doctor
metadata:
  version: 11.20.0
  tier: read-only
description: Diagnose Vector Lake dependencies, effective paths, infrastructure health, and semantic readiness.
---

# Runtime diagnosis

Call `doctor_vector_lake` from `vector-lake-mcp` and report its observed results.

- Distinguish failed checks, warnings, unavailable optional capabilities, infrastructure summary, and semantic readiness.
- Preserve the effective MEMORY, raw, Wiki, meta, database, and index paths shown by the tool.
- Do not turn a successful process exit or a single green check into an overall healthy verdict.
- Do not install dependencies, edit configuration, restart processes, or repair data unless the user separately authorizes that action.
- If the diagnostic itself fails, include the exact failure or incident identifier and leave the affected checks unverified.
