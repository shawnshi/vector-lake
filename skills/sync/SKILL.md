---
name: sync
metadata:
  version: 11.20.0
  tier: action-allowed
description: Scan configured raw sources and enqueue a bounded Vector Lake ingest batch without claiming completion.
---

# Raw-source scan and enqueue

`sync_vector_lake` is a compatibility alias for `prepare_ingest_batch`; both scan configured roots and enqueue bounded ingest jobs.

- Use this skill only when the user explicitly requests a sync or ingest scan.
- Call `sync_vector_lake` from `vector-lake-mcp` and report the exact scan/enqueue receipt.
- Use `list_ingest_tasks` when queue state is needed; distinguish `queued`, `awaiting_subagent`, processing, failed, and finalized work.
- `VECTOR_LAKE_RAW_FULL_SCAN_COMPLETE_V1` means the current inventory scan completed. It does not mean queued work was generated, finalized, indexed, or reconciled.
- Never report Wiki ingestion complete without separate finalization and current projection receipts.
- Do not invent a background-worker or delegation requirement; use only capabilities actually available in the current host.
