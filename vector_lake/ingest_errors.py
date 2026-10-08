"""Shared host decisions over the finalizer's existing text error protocol."""
from __future__ import annotations

from enum import Enum


class IngestFailureKind(str, Enum):
    VERSION_CONFLICT = "version_conflict"
    LEASE_LOST = "lease_lost"
    SOURCE_CHANGED = "source_changed"
    PAYLOAD_INVALID = "payload_invalid"


def classify_ingest_failure(reason: str) -> IngestFailureKind:
    """Keep legacy error text compatible while giving repair/retry one classification."""
    lowered = str(reason or "").casefold()
    if any(marker in lowered for marker in (
        "is no longer finalizable", "cannot be finalized from status", "lease is stale",
        "lease has expired", "lease_token does not match", "lease_owner does not match",
        "lease_generation does not match",
    )):
        return IngestFailureKind.LEASE_LOST
    if any(marker in lowered for marker in (
        "version conflict", "target_hash is stale", "target_projection_hash is stale",
        "source_hash is stale", "source_projection_hash is stale",
    )):
        return IngestFailureKind.VERSION_CONFLICT
    if "source changed" in lowered:
        return IngestFailureKind.SOURCE_CHANGED
    return IngestFailureKind.PAYLOAD_INVALID


def failure_is_transient(reason: str) -> bool:
    return classify_ingest_failure(reason) in {
        IngestFailureKind.VERSION_CONFLICT, IngestFailureKind.LEASE_LOST,
    }


def failure_needs_redispatch(reason: str) -> bool:
    return classify_ingest_failure(reason) != IngestFailureKind.PAYLOAD_INVALID
