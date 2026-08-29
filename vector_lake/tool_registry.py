"""Lazy public tool exports for the CLI and MCP gateways.

Every MCP process imports the gateway, but most invoke only a small subset of
the available tools. Keeping this registry lazy avoids loading graph,
projection, embedding, and scientific dependencies into every idle process.
"""

from importlib import import_module
from threading import RLock
from typing import Callable


_GROUPS: dict[str, tuple[str, ...]] = {
    "vector_lake.claim_assessment": ("record_claim_assessment",),
    "vector_lake.tool_delete": ("delete_source",),
    "vector_lake.tool_doctor": (
        "doctor_vector_lake",
        "semantic_readiness_vector_lake",
    ),
    "vector_lake.tool_debt": ("debt_vector_lake",),
    "vector_lake.tool_evidence": (
        "build_evidence_packet",
        "export_evidence_packet",
    ),
    "vector_lake.tool_gc": ("gc_vector_lake",),
    "vector_lake.tool_backup_retention": ("backup_retention_maintenance",),
    "vector_lake.tool_auto_ingest": (
        "auto_ingest_attempt_receipt_retention",
        "auto_ingest_budget_status",
    ),
    "vector_lake.tool_graph": ("audit_graph", "visualize_vector_lake"),
    "vector_lake.tool_lint": ("lint_vector_lake",),
    "vector_lake.tool_merge": ("merge_suggestions_vector_lake",),
    "vector_lake.tool_piea": ("check_duplicate_entity",),
    "vector_lake.tool_ingest": (
        "claim_ingest_tasks",
        "expire_ingest_tasks",
        "finalize_ingest",
        "list_ingest_tasks",
        "prepare_ingest_batch",
        "reconcile_ingest_job_debt",
        "reconcile_orphan_ingest_task_packets",
    ),
    "vector_lake.tool_query": (
        "prepare_query_context",
        "finalize_query_synthesis",
    ),
    "vector_lake.tool_review": ("review_vector_lake",),
    "vector_lake.tool_search": ("assemble_context", "search_vector_lake"),
    "vector_lake.tool_semantic_campaign": (
        "semantic_readiness_campaign_report",
    ),
    "vector_lake.tool_sync": ("sync_vector_lake",),
    "vector_lake.tool_trace": ("trace_vector_lake",),
    "vector_lake.tool_timeline": ("rebuild_timeline_events_from_claims",),
    "vector_lake.tool_projection": (
        "canonical_backfill_missing_wiki",
        "evidence_foundation_backfill",
        "reconcile_canonical_content_from_wiki",
        "embedding_backfill_projection",
        "projection_diff_report",
        "rebuild_index_projection",
        "restore_missing_wiki_from_canonical",
    ),
    "vector_lake.tool_research": ("research_vector_lake",),
    "vector_lake.tool_purpose": ("review_strategic_purpose",),
    "vector_lake.tool_governance_maintenance": (
        "classify_orphan_source_debt",
        "cleanup_operational_memory",
        "compact_change_set_history",
        "history_retention_maintenance",
        "operational_memory_search_index_maintenance",
        "register_unsupported_claim_debt",
        "retire_legacy_topology_queue",
    ),
}

_EXPORTS = {
    name: (module_name, name)
    for module_name, names in _GROUPS.items()
    for name in names
}
__all__ = sorted(_EXPORTS)
_LOAD_LOCK = RLock()


def __getattr__(name: str) -> Callable:
    """Import and cache a public tool on its first invocation."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    with _LOAD_LOCK:
        cached = globals().get(name)
        if cached is not None:
            return cached
        module_name, attribute_name = target
        value = getattr(import_module(module_name), attribute_name)
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
