"""Shared host decisions over the finalizer's existing text error protocol."""
from __future__ import annotations

from enum import Enum


# Host mechanical closures are not model/source rejection evidence.
REJECT_DUPLICATE = (
    "该原始文件的 Source 页已发布（frontmatter sources 已声明此 raw 路径），"
    "此任务为重复准备；由 ingest runner 自动关闭以免重复入库。"
)
REJECT_MISSING_SOURCE = "原始文件在 raw 目录下已不存在，任务无法完成；由 ingest runner 自动关闭。"

MECHANICAL_REJECTION_REASONS = frozenset({REJECT_DUPLICATE, REJECT_MISSING_SOURCE})


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
