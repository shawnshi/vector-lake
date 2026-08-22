"""Stable, bounded Agent-memory contract over Vector Lake primitives."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vector_lake.tool_memory import update_operational_memory
from vector_lake.tool_query import prepare_query_context
from vector_lake.tool_search import (
    assemble_context,
    resolve_exact_entities,
    search_vector_lake,
)


MEMORY_PROTOCOL_VERSION = "vector-lake-agent-memory/v1"
MEMORY_PROTOCOL_VERBS = (
    "recall",
    "remember",
    "entity",
    "synthesize",
    "context_pack",
    "delta",
)
_MAX_CONTEXT_CHARS = 200_000
_MAX_DELTA_LIMIT = 1_000


def capability_manifest() -> dict[str, Any]:
    """Describe the stable thin-client surface without overstating compatibility."""
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verbs": {
            "recall": {
                "mutability": "read",
                "modes": ["page", "memory", "claim"],
            },
            "remember": {
                "mutability": "governed_write",
                "memory_types": ["fact", "preference", "decision", "task_state"],
                "payload_transport": "sandboxed_payload_file",
            },
            "entity": {
                "mutability": "read",
                "matching": "nfkc_casefold_exact_key_id_title_alias",
            },
            "synthesize": {
                "mutability": "read",
                "mode": "proposal_only_dry_run",
            },
            "context_pack": {
                "mutability": "read",
                "budget_enforced_by_server": True,
            },
            "delta": {
                "mutability": "read",
                "scope": "current_page_projection_updates",
                "includes_deletions": False,
            },
        },
        "omitted_verbs": {
            "forget": (
                "Direct Agent retraction is intentionally unavailable. Vector Lake "
                "requires a governed claim or source lifecycle operation so audit "
                "history and AcceptedFact boundaries cannot be bypassed."
            )
        },
        "mcp_surface": {
            "environment": "VECTOR_LAKE_MCP_SURFACE=memory",
            "fail_closed": True,
            "includes_runtime_status": True,
        },
    }


def recall(
    query: str,
    *,
    top_k: int = 5,
    mode: str = "page",
    include_history: bool = False,
) -> dict[str, Any]:
    normalized_mode = str(mode or "page").strip().lower()
    if normalized_mode not in {"page", "memory", "claim"}:
        raise ValueError("mode must be page, memory, or claim")
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verb": "recall",
        "mode": normalized_mode,
        "include_history": bool(include_history),
        "result": search_vector_lake(
            query,
            top_k=top_k,
            mode=normalized_mode,
            include_history=include_history,
        ),
    }


def remember(memory_type: str, content: str) -> dict[str, Any]:
    result = update_operational_memory(memory_type, content)
    ok = not str(result).startswith("Error:")
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verb": "remember",
        "ok": ok,
        "committed": ok,
        "result": result,
    }


def entity(
    name: str,
    *,
    limit: int = 10,
    include_history: bool = False,
) -> dict[str, Any]:
    matches = resolve_exact_entities(
        name,
        limit=limit,
        include_history=include_history,
    )
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verb": "entity",
        "match_policy": "exact",
        "ambiguous": len(matches) > 1,
        "matches": matches,
    }


def synthesize(query: str) -> dict[str, Any]:
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verb": "synthesize",
        "proposal_only": True,
        "committed": False,
        "result": prepare_query_context(query, dry_run=True),
    }


def context_pack(query: str, *, max_chars: int = 32_000) -> dict[str, Any]:
    try:
        bounded_chars = int(max_chars)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars must be an integer") from exc
    bounded_chars = max(1_000, min(_MAX_CONTEXT_CHARS, bounded_chars))
    context = assemble_context(query, max_chars=bounded_chars)
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verb": "context_pack",
        "max_chars": bounded_chars,
        "context": context,
    }


def _parse_since(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("since must be an ISO 8601 datetime")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("since must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("since must include a timezone")
    return parsed.astimezone(timezone.utc)


def delta(since: str, *, limit: int = 100) -> dict[str, Any]:
    """Return current pages updated since a timestamp; deletion history is excluded."""
    from vector_lake.indexer import read_committed_index_snapshot

    since_dt = _parse_since(since)
    try:
        bounded_limit = max(1, min(_MAX_DELTA_LIMIT, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    index_data = read_committed_index_snapshot(_acquire_lock=False)
    changes = []
    for key, node in (index_data.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        raw_updated = str(node.get("updated_at") or node.get("updated") or "").strip()
        if not raw_updated:
            continue
        try:
            updated = _parse_since(raw_updated)
        except ValueError:
            continue
        if updated <= since_dt:
            continue
        changes.append(
            {
                "key": str(key),
                "title": node.get("title") or str(key),
                "type": node.get("type"),
                "status": node.get("status"),
                "updated_at": updated.isoformat(),
            }
        )
    changes.sort(key=lambda item: (item["updated_at"], item["key"]), reverse=True)
    omitted = max(0, len(changes) - bounded_limit)
    return {
        "contract_version": MEMORY_PROTOCOL_VERSION,
        "verb": "delta",
        "since": since_dt.isoformat(),
        "includes_deletions": False,
        "changes": changes[:bounded_limit],
        "omitted_count": omitted,
    }
