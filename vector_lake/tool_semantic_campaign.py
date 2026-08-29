"""Bounded, read-only semantic-readiness campaign reporting.

The campaign is intentionally stricter than the operational readiness summary:
it binds every page to one canonical/projection generation, counts current-version
claim assessments, and exposes deterministic debt pages without exporting claim or
evidence text.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter, OrderedDict
import copy
from dataclasses import dataclass, field, replace
import hashlib
import hmac
import json
import math
import os
from pathlib import PurePath
import re
import threading
import time
from typing import Any, Iterable

from vector_lake import db_store, indexer
from vector_lake.backup_capacity import projection_v2_reachable_inventory
from vector_lake.cancellation import CooperativeCancellation, cancellation_checkpoint
from vector_lake.governance_metrics import claim_governance_version
from vector_lake.projection_format_v2 import (
    ProjectionV2ContractError,
    read_committed_sidecar,
)


CAMPAIGN_CONTRACT = "vector-lake-semantic-readiness-campaign/v1"
CURSOR_CONTRACT = "vector-lake-semantic-readiness-campaign-cursor/v1"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_CURSOR_CHARS = 2048
CAMPAIGN_CACHE_CONTRACT = "vector-lake-semantic-readiness-campaign-cache/v1"
CAMPAIGN_CACHE_TTL_SECONDS = 120.0
CAMPAIGN_CACHE_TTL_ENV = "VECTOR_LAKE_SEMANTIC_CAMPAIGN_CURSOR_TTL_SECONDS"
MAX_CAMPAIGN_CACHE_ENTRIES = 2
MAX_CAMPAIGN_CACHE_BYTES = 384 * 1024 * 1024
MAX_SNAPSHOT_ROWS_TOTAL = 1_500_000
MAX_SNAPSHOT_JSON_BYTES = 256 * 1024 * 1024
MAX_SNAPSHOT_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SNAPSHOT_TEXT_FIELD_BYTES = 1024 * 1024
MAX_RECORD_JSON_BYTES = 4 * 1024 * 1024
MAX_PROJECTION_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_PROJECTION_BYTES_TOTAL = 384 * 1024 * 1024
MAX_GRAPH_MATERIALIZATION_BYTES = 256 * 1024 * 1024
MAX_PROJECTION_SIDECAR_BYTES = 1024 * 1024
MAX_BINDING_BYTES = 1024 * 1024
MAX_PROJECTION_NODES = 250_000
MAX_PROJECTION_EDGES = 2_000_000
MAX_DATABASE_WAL_BYTES = 512 * 1024 * 1024
MAX_DEBT_ITEMS = 500_000
MAX_DEBT_ITEM_BYTES = 4 * 1024 * 1024
MAX_DEBT_MATERIALIZATION_BYTES = 256 * 1024 * 1024
MAX_CACHED_INVENTORY_BYTES = 256 * 1024 * 1024
MAX_PAGES_AT_MAX_PAGE_SIZE = (
    MAX_DEBT_ITEMS + MAX_PAGE_SIZE - 1
) // MAX_PAGE_SIZE
_CANCELLATION_CHECKPOINT_ROWS = 128
_SINGLE_FLIGHT_WAIT_SECONDS = 0.1

SNAPSHOT_ROW_LIMITS = {
    "claim_assessments": 250_000,
    "claims": 250_000,
    "evidence": 250_000,
    "extraction_runs": 250_000,
    "runtime_generations": 7,
    "sources": 250_000,
}

_SNAPSHOT_TABLE_COLUMNS = {
    "claim_assessments": (
        "assessment_id",
        "claim_id",
        "outcome",
        "data_json",
        "recorded_at",
    ),
    "claims": ("claim_id", "data_json"),
    "evidence": ("evidence_id", "data_json"),
    "extraction_runs": ("run_id", "data_json", "recorded_at"),
    "runtime_generations": ("surface", "generation"),
    "sources": ("source_id", "data_json"),
}

_CANONICAL_GENERATION_SURFACES = (
    "claim_graph_edges",
    "claims",
    "entities",
    "evidence",
    "governance_queue",
    "page_graph_edges",
    "sources",
)
_REQUIRED_COLUMNS = {
    "claim_assessments": {
        "assessment_id",
        "claim_id",
        "outcome",
        "data_json",
        "recorded_at",
    },
    "claims": {"claim_id", "data_json"},
    "evidence": {"evidence_id", "data_json"},
    "extraction_runs": {"run_id", "data_json", "recorded_at"},
    "runtime_generations": {"surface", "generation"},
    "sources": {"source_id", "data_json"},
}
_DEBT_ORDER = {
    "unassessed_claim": 0,
    "low_evidence_claim": 1,
    "graph_generation_dirty": 2,
    "dangling_graph_edge": 3,
    "isolated_node": 4,
    "sparse_community": 5,
    "fragmented_graph": 6,
}


def _scan_checkpoint(label: str, position: int) -> None:
    interval = max(1, int(_CANCELLATION_CHECKPOINT_ROWS))
    if position % interval == 0:
        cancellation_checkpoint(label)


class SemanticCampaignContractError(RuntimeError):
    """The campaign cannot produce a trustworthy machine-readable report."""


class StaleCampaignCursor(SemanticCampaignContractError):
    """The canonical or projection generation changed between campaign pages."""


@dataclass
class _SnapshotBudget:
    rows_by_table: dict[str, int] = field(default_factory=dict)
    bytes_by_table: dict[str, int] = field(default_factory=dict)
    rows_total: int = 0
    json_bytes: int = 0
    source_bytes: int = 0

    def reserve_table(
        self,
        connection,
        *,
        table: str,
        where_sql: str = "",
        parameters: tuple[Any, ...] = (),
    ) -> None:
        """Reserve a table's complete selected input before rows are materialized."""
        if table in self.rows_by_table:
            raise SemanticCampaignContractError(
                f"semantic campaign snapshot table was reserved twice: {table}"
            )
        columns = _SNAPSHOT_TABLE_COLUMNS[table]
        byte_expressions = {
            column: (
                f"length(CAST(COALESCE(\"{column}\", '') AS BLOB))"
            )
            for column in columns
        }
        row_bytes_expression = " + ".join(byte_expressions.values()) or "0"
        select_expressions = [
            "COUNT(*) AS row_count",
            f"COALESCE(SUM({row_bytes_expression}), 0) AS source_bytes",
        ]
        select_expressions.extend(
            f"COALESCE(SUM({expression}), 0) AS sum_{index}"
            for index, expression in enumerate(byte_expressions.values())
        )
        select_expressions.extend(
            f"COALESCE(MAX({expression}), 0) AS max_{index}"
            for index, expression in enumerate(byte_expressions.values())
        )
        query = f'SELECT {", ".join(select_expressions)} FROM "{table}"'
        if where_sql:
            query += " WHERE " + where_sql
        aggregate = connection.execute(query, parameters).fetchone()
        table_rows = int(aggregate["row_count"])
        table_limit = int(SNAPSHOT_ROW_LIMITS[table])
        if table_rows > table_limit:
            raise SemanticCampaignContractError(
                f"semantic campaign snapshot row limit exceeded: {table} > {table_limit}"
            )
        rows_total = self.rows_total + table_rows
        if rows_total > MAX_SNAPSHOT_ROWS_TOTAL:
            raise SemanticCampaignContractError(
                "semantic campaign snapshot total row limit exceeded: "
                f"{rows_total} > {MAX_SNAPSHOT_ROWS_TOTAL}"
            )
        for index, column in enumerate(columns):
            field_bytes = int(aggregate[f"max_{index}"])
            field_limit = (
                MAX_RECORD_JSON_BYTES
                if column == "data_json"
                else MAX_SNAPSHOT_TEXT_FIELD_BYTES
            )
            if field_bytes > field_limit:
                raise SemanticCampaignContractError(
                    "semantic campaign snapshot field byte limit exceeded: "
                    f"{table}.{column} > {field_limit}"
                )
        table_bytes = int(aggregate["source_bytes"])
        source_bytes = self.source_bytes + table_bytes
        if source_bytes > MAX_SNAPSHOT_SOURCE_BYTES:
            raise SemanticCampaignContractError(
                "semantic campaign snapshot source byte limit exceeded: "
                f"{source_bytes} > {MAX_SNAPSHOT_SOURCE_BYTES}"
            )
        data_json_expression = byte_expressions.get("data_json")
        table_json_bytes = 0
        if data_json_expression is not None:
            data_json_index = columns.index("data_json")
            table_json_bytes = int(aggregate[f"sum_{data_json_index}"])
        json_bytes = self.json_bytes + table_json_bytes
        if json_bytes > MAX_SNAPSHOT_JSON_BYTES:
            raise SemanticCampaignContractError(
                "semantic campaign snapshot JSON byte limit exceeded: "
                f"{json_bytes} > {MAX_SNAPSHOT_JSON_BYTES}"
            )
        self.rows_by_table[table] = table_rows
        self.bytes_by_table[table] = table_bytes
        self.rows_total = rows_total
        self.json_bytes = json_bytes
        self.source_bytes = source_bytes


@dataclass
class _DebtBudget:
    count: int = 0
    materialized_bytes: int = 2

    def append(self, items: list[dict[str, Any]], item: dict[str, Any]) -> None:
        if self.count >= MAX_DEBT_ITEMS:
            raise SemanticCampaignContractError(
                "semantic campaign debt inventory limit exceeded: "
                f"> {MAX_DEBT_ITEMS}"
            )
        _item_fingerprint, item_bytes = _bounded_fingerprint(
            item,
            byte_limit=MAX_DEBT_ITEM_BYTES,
            label="debt item",
        )
        materialized_bytes = (
            self.materialized_bytes + item_bytes + (1 if self.count else 0)
        )
        if materialized_bytes > MAX_DEBT_MATERIALIZATION_BYTES:
            raise SemanticCampaignContractError(
                "semantic campaign debt materialization byte limit exceeded: "
                f"{materialized_bytes} > {MAX_DEBT_MATERIALIZATION_BYTES}"
            )
        items.append(item)
        self.count += 1
        self.materialized_bytes = materialized_bytes


@dataclass(frozen=True)
class _CampaignSnapshot:
    binding: dict[str, Any]
    cache_bytes: int
    campaign_fingerprint: str
    coverage: dict[str, Any]
    debt: dict[str, Any]
    debt_inventory_fingerprint: str
    expires_at: float
    generation_fingerprint: str
    items: tuple[dict[str, Any], ...]
    lease_seconds: float
    readiness: dict[str, Any]
    scan: dict[str, Any]
    source_fingerprint: str
    source_state: dict[str, Any]
    summary: dict[str, Any]


@dataclass
class _CampaignBuildFlight:
    event: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


_CAMPAIGN_CACHE: OrderedDict[str, _CampaignSnapshot] = OrderedDict()
_CAMPAIGN_CACHE_LOCK = threading.RLock()
_CAMPAIGN_CACHE_BYTES = 0
_CAMPAIGN_BUILD_FLIGHTS: dict[str, _CampaignBuildFlight] = {}


def _campaign_cache_now() -> float:
    return time.monotonic()


def _campaign_cache_ttl_seconds() -> float:
    raw = os.environ.get(CAMPAIGN_CACHE_TTL_ENV, str(CAMPAIGN_CACHE_TTL_SECONDS))
    try:
        ttl_seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise SemanticCampaignContractError(
            f"semantic campaign cursor TTL must be a positive finite number: {raw!r}"
        ) from exc
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise SemanticCampaignContractError(
            f"semantic campaign cursor TTL must be a positive finite number: {raw!r}"
        )
    return ttl_seconds


def _clear_campaign_snapshot_cache() -> None:
    global _CAMPAIGN_CACHE_BYTES
    with _CAMPAIGN_CACHE_LOCK:
        _CAMPAIGN_CACHE.clear()
        _CAMPAIGN_CACHE_BYTES = 0
        _CAMPAIGN_BUILD_FLIGHTS.clear()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _bounded_fingerprint(
    value: Any,
    *,
    byte_limit: int,
    label: str,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    encoded_bytes = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        encoded_bytes += len(encoded)
        if encoded_bytes > byte_limit:
            raise SemanticCampaignContractError(
                f"semantic campaign {label} byte limit exceeded: "
                f"{encoded_bytes} > {byte_limit}"
            )
        digest.update(encoded)
    return "sha256:" + digest.hexdigest(), encoded_bytes


def _bounded_plain_json(value: Any, *, byte_limit: int, label: str) -> Any:
    chunks: list[str] = []
    encoded_bytes = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(value):
        encoded_bytes += len(chunk.encode("utf-8"))
        if encoded_bytes > byte_limit:
            raise SemanticCampaignContractError(
                f"semantic campaign {label} byte limit exceeded: "
                f"{encoded_bytes} > {byte_limit}"
            )
        chunks.append(chunk)
    return json.loads("".join(chunks))


def _path_identity(path) -> tuple[Any, ...]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise SemanticCampaignContractError(
            f"semantic campaign source is unavailable: {path.name}: {exc}"
        ) from exc
    return (
        str(path.resolve()),
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _database_snapshot_identity() -> tuple[Any, ...]:
    path = db_store.peek_db_path().resolve()
    if not path.is_file():
        raise db_store.ReadOnlySnapshotUnavailable(f"database_missing:{path}")
    identity = db_store._read_only_snapshot_identity(path)
    wal_identity = identity[1]
    if len(wal_identity) > 3 and int(wal_identity[3]) > MAX_DATABASE_WAL_BYTES:
        raise SemanticCampaignContractError(
            "semantic campaign WAL byte limit exceeded: "
            f"{wal_identity[3]} > {MAX_DATABASE_WAL_BYTES}"
        )
    return identity


def _projection_snapshot_identity() -> dict[str, Any]:
    index_path = indexer.get_index_path()
    claim_graph_path = indexer.get_claim_graph_path()
    locator_identities_before = {
        index_path.name: _path_identity(index_path),
        claim_graph_path.name: _path_identity(claim_graph_path),
    }
    try:
        # Do not open the normal writable connection merely to validate a
        # read-only campaign snapshot: doing so can change WAL file identity
        # and make the campaign invalidate itself.  One pinned read-only
        # transaction binds both runtime checks around the object inventory.
        with db_store.read_only_transaction_snapshot() as connection:
            sidecar, sidecar_identity, runtime_before = read_committed_sidecar(
                index_path.parent,
                connection=connection,
            )
            inventory = projection_v2_reachable_inventory(wiki_dir=index_path.parent)
            sidecar_after, sidecar_identity_after, runtime_after = (
                read_committed_sidecar(
                    index_path.parent,
                    connection=connection,
                )
            )
    except (OSError, UnicodeError, ValueError, ProjectionV2ContractError) as exc:
        raise SemanticCampaignContractError(
            f"semantic campaign projection v2 is not committed and current: {exc}"
        ) from exc
    if inventory is None:
        raise SemanticCampaignContractError(
            "semantic campaign requires a committed projection v2; legacy v1 is "
            "accepted only by explicit migration or rollback"
        )
    locator_identities_after = {
        index_path.name: _path_identity(index_path),
        claim_graph_path.name: _path_identity(claim_graph_path),
    }
    if (
        sidecar_after != sidecar
        or sidecar_identity_after != sidecar_identity
        or runtime_after != runtime_before
        or locator_identities_after != locator_identities_before
        or inventory.get("sidecar") != sidecar
        or inventory.get("sidecar_sha256") != runtime_before.get("sidecar_sha256")
    ):
        raise SemanticCampaignContractError(
            "semantic campaign projection changed while its v2 closure was read"
        )

    total_projection_bytes = int(inventory["total_projection_bytes"])
    materialization_bytes = int(inventory["reachable_object_bytes"])
    if total_projection_bytes > MAX_PROJECTION_BYTES_TOTAL:
        raise SemanticCampaignContractError(
            "semantic campaign projection total byte limit exceeded: "
            f"{total_projection_bytes} > {MAX_PROJECTION_BYTES_TOTAL}"
        )
    if materialization_bytes > MAX_GRAPH_MATERIALIZATION_BYTES:
        raise SemanticCampaignContractError(
            "semantic campaign graph materialization byte limit exceeded before "
            f"decode: {materialization_bytes} > {MAX_GRAPH_MATERIALIZATION_BYTES}"
        )

    counts = sidecar.get("counts")
    if not isinstance(counts, dict):
        raise SemanticCampaignContractError(
            "semantic campaign projection v2 counts are missing"
        )
    count_specs = {
        "claim_edges": MAX_PROJECTION_EDGES,
        "claim_nodes": MAX_PROJECTION_NODES,
        "index_edges": MAX_PROJECTION_EDGES,
        "index_nodes": MAX_PROJECTION_NODES,
    }
    for name, limit in count_specs.items():
        count = counts.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SemanticCampaignContractError(
                f"semantic campaign projection count metadata is invalid: {name}"
            )
        if count > limit:
            raise SemanticCampaignContractError(
                "semantic campaign projection count limit exceeded: "
                f"{name} > {limit}"
            )

    artifact_state = {
        index_path.name: {
            "bytes": int(locator_identities_after[index_path.name][3]),
            "edge_count": counts["index_edges"],
            "identity": locator_identities_after[index_path.name],
            "node_count": counts["index_nodes"],
            "sha256": sidecar["index_root_sha256"],
        },
        claim_graph_path.name: {
            "bytes": int(locator_identities_after[claim_graph_path.name][3]),
            "edge_count": counts["claim_edges"],
            "identity": locator_identities_after[claim_graph_path.name],
            "node_count": counts["claim_nodes"],
            "sha256": sidecar["claim_graph_root_sha256"],
        },
    }
    return {
        "artifacts": artifact_state,
        "generation": str(sidecar["projection_generation"]),
        "materialization_bytes": materialization_bytes,
        "object_count": int(inventory["object_count"]),
        "sidecar_identity": sidecar_identity,
        "sidecar_sha256": str(inventory["sidecar_sha256"]),
        "total_projection_bytes": total_projection_bytes,
    }


def _physical_source_state() -> dict[str, Any]:
    return {
        "database": _database_snapshot_identity(),
        "projection": _projection_snapshot_identity(),
    }


def _validate_loaded_projection_bounds(index_data: dict[str, Any]) -> None:
    nodes = index_data.get("nodes")
    edges = index_data.get("weighted_edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        raise SemanticCampaignContractError("committed graph projection is malformed")
    if len(nodes) > MAX_PROJECTION_NODES:
        raise SemanticCampaignContractError(
            "semantic campaign loaded projection node limit exceeded: "
            f"{len(nodes)} > {MAX_PROJECTION_NODES}"
        )
    if len(edges) > MAX_PROJECTION_EDGES:
        raise SemanticCampaignContractError(
            "semantic campaign loaded projection edge limit exceeded: "
            f"{len(edges)} > {MAX_PROJECTION_EDGES}"
        )


def _generation_fingerprint(source_state: dict[str, Any]) -> str:
    return _fingerprint(
        {
            "projection_generation": source_state["projection"]["generation"],
            "runtime_generations": source_state["runtime_generations"],
        }
    )


def _current_source_state() -> dict[str, Any]:
    physical_before = _physical_source_state()
    with db_store.read_only_transaction_snapshot() as connection:
        _require_schema(connection)
        runtime_generations = _runtime_generations(connection)
    physical_after = _physical_source_state()
    if physical_before != physical_after:
        raise SemanticCampaignContractError(
            "semantic campaign source changed during generation validation"
        )
    return {
        **physical_after,
        "runtime_generations": runtime_generations,
    }


def _drop_cached_snapshot_locked(
    campaign_fingerprint: str,
) -> _CampaignSnapshot | None:
    global _CAMPAIGN_CACHE_BYTES
    snapshot = _CAMPAIGN_CACHE.pop(campaign_fingerprint, None)
    if snapshot is not None:
        _CAMPAIGN_CACHE_BYTES = max(
            0,
            _CAMPAIGN_CACHE_BYTES - snapshot.cache_bytes,
        )
    return snapshot


def _prune_expired_snapshots_locked(now: float) -> None:
    expired = [
        campaign_fingerprint
        for campaign_fingerprint, snapshot in _CAMPAIGN_CACHE.items()
        if snapshot.expires_at <= now
    ]
    for campaign_fingerprint in expired:
        _drop_cached_snapshot_locked(campaign_fingerprint)


def _cache_snapshot(snapshot: _CampaignSnapshot) -> None:
    global _CAMPAIGN_CACHE_BYTES
    max_entries = int(MAX_CAMPAIGN_CACHE_ENTRIES)
    max_bytes = int(MAX_CAMPAIGN_CACHE_BYTES)
    if max_entries <= 0:
        raise SemanticCampaignContractError(
            "semantic campaign cache entry limit must be positive"
        )
    if max_bytes <= 0:
        raise SemanticCampaignContractError(
            "semantic campaign cache byte limit must be positive"
        )
    if snapshot.cache_bytes > max_bytes:
        raise SemanticCampaignContractError(
            "semantic campaign cache entry byte limit exceeded: "
            f"{snapshot.cache_bytes} > {max_bytes}"
        )
    with _CAMPAIGN_CACHE_LOCK:
        _prune_expired_snapshots_locked(_campaign_cache_now())
        _drop_cached_snapshot_locked(snapshot.campaign_fingerprint)
        while _CAMPAIGN_CACHE and (
            len(_CAMPAIGN_CACHE) >= max_entries
            or _CAMPAIGN_CACHE_BYTES + snapshot.cache_bytes > max_bytes
        ):
            oldest_fingerprint = next(iter(_CAMPAIGN_CACHE))
            _drop_cached_snapshot_locked(oldest_fingerprint)
        if _CAMPAIGN_CACHE_BYTES + snapshot.cache_bytes > max_bytes:
            raise SemanticCampaignContractError(
                "semantic campaign cache total byte limit exceeded: "
                f"{_CAMPAIGN_CACHE_BYTES + snapshot.cache_bytes} > {max_bytes}"
            )
        _CAMPAIGN_CACHE[snapshot.campaign_fingerprint] = snapshot
        _CAMPAIGN_CACHE_BYTES += snapshot.cache_bytes
        _CAMPAIGN_CACHE.move_to_end(snapshot.campaign_fingerprint)


def _evict_cached_snapshot(campaign_fingerprint: str) -> None:
    with _CAMPAIGN_CACHE_LOCK:
        _drop_cached_snapshot_locked(campaign_fingerprint)


def _campaign_cache_usage() -> tuple[int, int]:
    with _CAMPAIGN_CACHE_LOCK:
        return len(_CAMPAIGN_CACHE), _CAMPAIGN_CACHE_BYTES


def _cached_snapshot(campaign_fingerprint: str) -> _CampaignSnapshot:
    with _CAMPAIGN_CACHE_LOCK:
        snapshot = _CAMPAIGN_CACHE.get(campaign_fingerprint)
        if snapshot is None:
            raise StaleCampaignCursor(
                "semantic readiness campaign cache is unavailable; "
                "restart pagination without a cursor"
            )
        if snapshot.expires_at <= _campaign_cache_now():
            _drop_cached_snapshot_locked(campaign_fingerprint)
            raise StaleCampaignCursor(
                "semantic readiness campaign cache expired; "
                "restart pagination without a cursor"
            )
        return snapshot


def _renew_cached_snapshot(snapshot: _CampaignSnapshot) -> _CampaignSnapshot:
    lease_seconds = _campaign_cache_ttl_seconds()
    with _CAMPAIGN_CACHE_LOCK:
        current = _CAMPAIGN_CACHE.get(snapshot.campaign_fingerprint)
        if current is None:
            raise StaleCampaignCursor(
                "semantic readiness campaign cache is unavailable; "
                "restart pagination without a cursor"
            )
        now = _campaign_cache_now()
        if current.expires_at <= now:
            _drop_cached_snapshot_locked(snapshot.campaign_fingerprint)
            raise StaleCampaignCursor(
                "semantic readiness campaign cache expired; "
                "restart pagination without a cursor"
            )
        renewed = replace(
            current,
            expires_at=now + lease_seconds,
            lease_seconds=lease_seconds,
        )
        _CAMPAIGN_CACHE[snapshot.campaign_fingerprint] = renewed
        _CAMPAIGN_CACHE.move_to_end(snapshot.campaign_fingerprint)
        return renewed


def _bounded_page_size(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


def _encode_cursor(campaign_fingerprint: str, offset: int) -> str:
    unsigned = {
        "campaign_fingerprint": campaign_fingerprint,
        "contract": CURSOR_CONTRACT,
        "offset": int(offset),
    }
    payload = dict(unsigned)
    payload["checksum"] = _fingerprint(unsigned)
    return base64.urlsafe_b64encode(_canonical_json_bytes(payload)).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any] | None:
    normalized = str(cursor or "").strip()
    if not normalized:
        return None
    if len(normalized) > MAX_CURSOR_CHARS:
        raise ValueError("cursor exceeds the maximum encoded length")
    try:
        padding = "=" * (-len(normalized) % 4)
        raw = base64.b64decode(normalized + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is not a valid semantic campaign cursor") from exc
    if not isinstance(payload, dict) or payload.get("contract") != CURSOR_CONTRACT:
        raise ValueError("cursor contract is unsupported")
    checksum = str(payload.get("checksum") or "")
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    if not hmac.compare_digest(checksum, _fingerprint(unsigned)):
        raise ValueError("cursor checksum is invalid")
    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("cursor offset is invalid")
    campaign_fingerprint = payload.get("campaign_fingerprint")
    if not isinstance(campaign_fingerprint, str) or not campaign_fingerprint.startswith(
        "sha256:"
    ):
        raise ValueError("cursor campaign fingerprint is invalid")
    return payload


def _require_schema(connection) -> None:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - tables)
    missing_columns: list[str] = []
    for table, required in _REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        observed = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        missing_columns.extend(
            f"{table}.{column}" for column in sorted(required - observed)
        )
    if missing_tables or missing_columns:
        detail = []
        if missing_tables:
            detail.append("missing_tables=" + ",".join(missing_tables))
        if missing_columns:
            detail.append("missing_columns=" + ",".join(missing_columns))
        raise SemanticCampaignContractError(
            "semantic campaign schema is not ready: " + ";".join(detail)
        )


def _decode_record(value: Any, *, table: str, identifier: str) -> dict[str, Any]:
    try:
        record = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SemanticCampaignContractError(
            f"canonical {table} record contains invalid JSON: {identifier}"
        ) from exc
    if not isinstance(record, dict):
        raise SemanticCampaignContractError(
            f"canonical {table} record is not an object: {identifier}"
        )
    return record


def _load_record_map(
    connection,
    table: str,
    identifier_column: str,
    budget: _SnapshotBudget,
) -> dict[str, dict]:
    budget.reserve_table(connection, table=table)
    rows = connection.execute(
        f"SELECT {identifier_column}, data_json FROM {table} "
        f"ORDER BY {identifier_column}",
    )
    records: dict[str, dict] = {}
    for position, row in enumerate(rows):
        _scan_checkpoint(f"semantic_campaign:{table}", position)
        identifier = str(row[identifier_column])
        records[identifier] = _decode_record(
            row["data_json"], table=table, identifier=identifier
        )
    return records


def _unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )


def _page_key(claim: dict[str, Any]) -> str:
    locator = claim.get("locator")
    locator_page = locator.get("page_key") if isinstance(locator, dict) else ""
    value = str(locator_page or claim.get("source_page") or "").strip()
    return PurePath(value).stem if value else ""


def _is_verified_source(source: dict[str, Any]) -> bool:
    digest = source.get("content_hash")
    return source.get("integrity_status") == "verified" and isinstance(
        digest, str
    ) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) is not None


def _coverage(
    numerator: int,
    denominator: int,
    *,
    unit: str,
    criterion: str,
) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "denominator": int(denominator),
        "numerator": int(numerator),
        "ratio": round(numerator / denominator, 6) if denominator else None,
        "unit": unit,
    }


def _update_ordered_row_digest(digest, row, columns: tuple[str, ...]) -> None:
    encoded = _canonical_json_bytes([row[column] for column in columns])
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _ordered_row_generation(digest, row_count: int) -> dict[str, Any]:
    return {
        "algorithm": "ordered-row-sha256-v1",
        "row_count": row_count,
        "token": "sha256:" + digest.hexdigest(),
    }


def _runtime_generations(
    connection,
    budget: _SnapshotBudget | None = None,
) -> dict[str, int]:
    where_sql = (
        f"surface IN ({','.join('?' for _ in _CANONICAL_GENERATION_SURFACES)})"
    )
    if budget is not None:
        budget.reserve_table(
            connection,
            table="runtime_generations",
            where_sql=where_sql,
            parameters=_CANONICAL_GENERATION_SURFACES,
        )
    rows = connection.execute(
        "SELECT surface, generation FROM runtime_generations "
        f"WHERE {where_sql} ORDER BY surface",
        _CANONICAL_GENERATION_SURFACES,
    )
    generations: dict[str, int] = {}
    for position, row in enumerate(rows):
        _scan_checkpoint("semantic_campaign:runtime_generations", position)
        surface = str(row["surface"])
        generations[surface] = int(row["generation"])
    missing = sorted(set(_CANONICAL_GENERATION_SURFACES) - set(generations))
    if missing:
        raise SemanticCampaignContractError(
            "semantic campaign generation registry is incomplete: " + ",".join(missing)
        )
    generations = {
        surface: generations[surface] for surface in _CANONICAL_GENERATION_SURFACES
    }
    return generations


def _campaign_binding(
    connection,
    index_data: dict[str, Any],
    supplemental_generations: dict[str, dict[str, Any]],
    budget: _SnapshotBudget,
) -> dict[str, Any]:
    generations = _runtime_generations(connection, budget)
    manifest = index_data.get(indexer.PROJECTION_MANIFEST_KEY)
    if not isinstance(manifest, dict):
        raise SemanticCampaignContractError("projection manifest is missing")
    projection_generation = str(manifest.get("generation") or "")
    if not projection_generation:
        raise SemanticCampaignContractError("projection generation is missing")
    projection_canonical = manifest.get("canonical_generation")
    if not isinstance(projection_canonical, dict):
        raise SemanticCampaignContractError(
            "projection canonical-generation binding is missing"
        )
    graph_state = index_data.get("graph_state")
    if not isinstance(graph_state, dict):
        raise SemanticCampaignContractError("graph state is missing")
    canonical_generation = {
        "algorithm": "campaign-runtime-generations-sha256-v1",
        "runtime_generations": generations,
    }
    canonical_generation["token"] = _fingerprint(canonical_generation)
    graph_shape = {
        "edge_count": len(index_data.get("weighted_edges") or []),
        "node_count": len(index_data.get("nodes") or {}),
        "state": graph_state,
    }
    return {
        "canonical_generation": canonical_generation,
        "projection_graph_generation": {
            "graph_generation": projection_generation,
            "graph_state": graph_state,
            "graph_state_fingerprint": _fingerprint(graph_shape),
            "projection_canonical_generation": projection_canonical,
            "projection_generation": projection_generation,
            "published_at": manifest.get("published_at"),
            "shared_generation": True,
        },
        "supplemental_generations": supplemental_generations,
    }


def _assessment_snapshot(
    connection,
    budget: _SnapshotBudget,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    ordered_assessments: dict[
        str,
        list[tuple[tuple[str, str], dict[str, Any]]],
    ] = {}
    columns = (
        "assessment_id",
        "claim_id",
        "outcome",
        "data_json",
        "recorded_at",
    )
    digest = hashlib.sha256()
    row_count = 0
    budget.reserve_table(connection, table="claim_assessments")
    rows = connection.execute(
        "SELECT assessment_id, claim_id, outcome, data_json, recorded_at "
        "FROM claim_assessments ORDER BY assessment_id",
    )
    for position, row in enumerate(rows):
        _scan_checkpoint("semantic_campaign:claim_assessments", position)
        _update_ordered_row_digest(digest, row, columns)
        row_count += 1
        record = _decode_record(
            row["data_json"],
            table="claim_assessments",
            identifier=str(row["assessment_id"]),
        )
        record.setdefault("assessment_id", str(row["assessment_id"]))
        record.setdefault("claim_id", str(row["claim_id"]))
        record.setdefault("outcome", str(row["outcome"] or ""))
        record.setdefault("recorded_at", row["recorded_at"])
        ordered_assessments.setdefault(str(row["claim_id"]), []).append(
            (
                (
                    str(row["recorded_at"] or ""),
                    str(row["assessment_id"]),
                ),
                record,
            )
        )
    assessments = {
        claim_id: [
            record for _sort_key, record in sorted(records, key=lambda item: item[0])
        ]
        for claim_id, records in ordered_assessments.items()
    }
    return assessments, _ordered_row_generation(digest, row_count)


def _extraction_run_snapshot(
    connection,
    budget: _SnapshotBudget,
) -> tuple[set[str], dict[str, Any]]:
    columns = ("run_id", "data_json", "recorded_at")
    digest = hashlib.sha256()
    row_count = 0
    run_ids: set[str] = set()
    budget.reserve_table(connection, table="extraction_runs")
    rows = connection.execute(
        "SELECT run_id, data_json, recorded_at FROM extraction_runs "
        "ORDER BY run_id",
    )
    for position, row in enumerate(rows):
        _scan_checkpoint("semantic_campaign:extraction_runs", position)
        _update_ordered_row_digest(digest, row, columns)
        row_count += 1
        run_ids.add(str(row["run_id"]))
    return run_ids, _ordered_row_generation(digest, row_count)


def _claim_debt(
    claims: dict[str, dict],
    evidence: dict[str, dict],
    sources: dict[str, dict],
    extraction_run_ids: set[str],
    assessments: dict[str, list[dict[str, Any]]],
    debt_budget: _DebtBudget,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    evidence_complete_count = 0
    extraction_complete_count = 0
    assessment_complete_count = 0
    supported_assessment_count = 0
    current_outcomes: Counter[str] = Counter()
    low_evidence_claims: set[str] = set()
    unassessed_claims: set[str] = set()

    for position, claim_id in enumerate(sorted(claims)):
        _scan_checkpoint("semantic_campaign:claim_debt", position)
        claim = claims[claim_id]
        claim_version = claim_governance_version(claim)
        current_assessments = [
            assessment
            for assessment in assessments.get(claim_id, [])
            if str(assessment.get("claim_version") or "") == claim_version
        ]
        if current_assessments:
            assessment_complete_count += 1
            latest_outcome = str(current_assessments[-1].get("outcome") or "unknown")
            current_outcomes[latest_outcome] += 1
            if any(
                str(assessment.get("outcome") or "") == "supported"
                for assessment in current_assessments
            ):
                supported_assessment_count += 1
        else:
            unassessed_claims.add(claim_id)
            debt_budget.append(
                items,
                {
                    "debt_id": f"unassessed-claim:{claim_id}",
                    "debt_type": "unassessed_claim",
                    "disposition": "human_review",
                    "page_key": _page_key(claim),
                    "reasons": ["no_current_version_assessment"],
                    "remediation": {
                        "automatic_apply": False,
                        "tool": "record_claim_assessment",
                    },
                    "subject_id": claim_id,
                }
            )

        evidence_ids = _unique_strings(claim.get("evidence_ids"))
        missing_evidence = [item for item in evidence_ids if item not in evidence]
        evidence_records = [evidence[item] for item in evidence_ids if item in evidence]
        evidence_complete = bool(evidence_ids) and not missing_evidence
        if evidence_complete:
            evidence_complete_count += 1

        extraction_run_id = str(claim.get("extraction_run_id") or "").strip()
        extraction_complete = bool(extraction_run_id) and (
            extraction_run_id in extraction_run_ids
        )
        if extraction_complete:
            extraction_complete_count += 1

        source_ids = _unique_strings(claim.get("source_ids"))
        source_ids.extend(
            str(record.get("source_id") or "").strip()
            for record in evidence_records
            if str(record.get("source_id") or "").strip()
        )
        source_ids = list(dict.fromkeys(source_ids))
        missing_sources = [item for item in source_ids if item not in sources]
        source_records = [sources[item] for item in source_ids if item in sources]

        reasons: list[str] = []
        if not evidence_ids:
            reasons.append("claim_has_no_evidence_refs")
        elif missing_evidence:
            reasons.append(f"missing_evidence_records:{len(missing_evidence)}")
        if evidence_records:
            unresolved_locators = sum(
                1
                for record in evidence_records
                if not isinstance(record.get("source_locator"), dict)
                or record["source_locator"].get("kind") == "unresolved"
            )
            if unresolved_locators:
                reasons.append(f"evidence_raw_locator_incomplete:{unresolved_locators}")
            unsafe_lineage = sum(
                1 for record in evidence_records if record.get("lineage_safe") is not True
            )
            if unsafe_lineage:
                reasons.append(f"evidence_lineage_unsafe:{unsafe_lineage}")
        if not source_ids:
            reasons.append("claim_has_no_source_refs")
        elif missing_sources:
            reasons.append(f"missing_source_records:{len(missing_sources)}")
        unverified_sources = sum(
            1 for record in source_records if not _is_verified_source(record)
        )
        if unverified_sources:
            reasons.append(f"source_integrity_unverified:{unverified_sources}")
        if not extraction_complete:
            reasons.append("extraction_run_missing")

        if reasons:
            low_evidence_claims.add(claim_id)
            page_key = _page_key(claim)
            automatic = reasons == ["extraction_run_missing"] and bool(page_key)
            debt_budget.append(
                items,
                {
                    "debt_id": f"low-evidence-claim:{claim_id}",
                    "debt_type": "low_evidence_claim",
                    "disposition": (
                        "automatic_repair" if automatic else "human_review"
                    ),
                    "page_key": page_key,
                    "reasons": reasons,
                    "remediation": {
                        "automatic_apply": False,
                        "preview_required": automatic,
                        "tool": (
                            "evidence_foundation_backfill"
                            if automatic
                            else "review"
                        ),
                    },
                    "subject_id": claim_id,
                }
            )

    claim_total = len(claims)
    verified_sources = sum(1 for source in sources.values() if _is_verified_source(source))
    coverage = {
        "assessment": _coverage(
            assessment_complete_count,
            claim_total,
            unit="claim",
            criterion="claim has at least one assessment bound to its current governance version",
        ),
        "evidence": _coverage(
            evidence_complete_count,
            claim_total,
            unit="claim",
            criterion="claim has one or more evidence ids and every referenced evidence row exists",
        ),
        "extraction": _coverage(
            extraction_complete_count,
            claim_total,
            unit="claim",
            criterion="claim extraction_run_id resolves to the extraction-run ledger",
        ),
        "source_integrity": _coverage(
            verified_sources,
            len(sources),
            unit="source",
            criterion="canonical source is verified and has a 64-character SHA-256 digest",
        ),
    }
    summary = {
        "assessment_outcomes_current": dict(sorted(current_outcomes.items())),
        "claims_with_current_supported_assessment": supported_assessment_count,
        "low_evidence_claim_ids": low_evidence_claims,
        "unassessed_claim_ids": unassessed_claims,
        "coverage": coverage,
    }
    return items, summary


def _graph_components(nodes: set[str], edges: Iterable[dict[str, Any]]) -> tuple[
    list[list[str]], list[tuple[str, str]]
]:
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    dangling: set[tuple[str, str]] = set()
    for position, edge in enumerate(edges):
        _scan_checkpoint("semantic_campaign:graph_edges", position)
        if not isinstance(edge, dict):
            raise SemanticCampaignContractError("weighted graph edge is not an object")
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target or source == target:
            dangling.add((source or "<missing>", target or "<missing>"))
            continue
        if source not in nodes or target not in nodes:
            dangling.add((source, target))
            continue
        adjacency[source].add(target)
        adjacency[target].add(source)

    components: list[list[str]] = []
    unseen = set(nodes)
    visited_nodes = 0
    while unseen:
        root = min(unseen)
        stack = [root]
        unseen.remove(root)
        members: list[str] = []
        while stack:
            _scan_checkpoint("semantic_campaign:graph_components", visited_nodes)
            visited_nodes += 1
            current = stack.pop()
            members.append(current)
            for neighbor in sorted(adjacency[current], reverse=True):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        components.append(sorted(members))
    components.sort(key=lambda members: (-len(members), members[0] if members else ""))
    return components, sorted(dangling)


def _topology_debt(
    index_data: dict[str, Any],
    debt_budget: _DebtBudget,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    nodes = index_data.get("nodes")
    edges = index_data.get("weighted_edges")
    graph_state = index_data.get("graph_state")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        raise SemanticCampaignContractError("committed graph projection is malformed")
    if not isinstance(graph_state, dict):
        raise SemanticCampaignContractError("committed graph state is malformed")

    components, dangling = _graph_components(set(map(str, nodes)), edges)
    items: list[dict[str, Any]] = []
    if graph_state.get("dirty") is True:
        debt_budget.append(
            items,
            {
                "debt_id": "graph-generation-dirty",
                "debt_type": "graph_generation_dirty",
                "disposition": "automatic_repair",
                "reasons": [str(graph_state.get("reason") or "unknown")],
                "remediation": {
                    "automatic_apply": False,
                    "preview_required": True,
                    "tool": "projection_rebuild_index",
                },
                "subject_id": str(
                    index_data[indexer.PROJECTION_MANIFEST_KEY]["generation"]
                ),
            }
        )
    for source, target in dangling:
        edge_fingerprint = _fingerprint([source, target]).split(":", 1)[1][:16]
        debt_budget.append(
            items,
            {
                "debt_id": f"dangling-graph-edge:{edge_fingerprint}",
                "debt_type": "dangling_graph_edge",
                "disposition": "human_review",
                "reasons": ["edge endpoint is missing, empty, or self-referential"],
                "remediation": {"automatic_apply": False, "tool": "review"},
                "subject_ids": [source, target],
            }
        )
    for members in components:
        if len(members) == 1:
            node = members[0]
            debt_budget.append(
                items,
                {
                    "debt_id": f"isolated-node:{node}",
                    "debt_type": "isolated_node",
                    "disposition": "human_review",
                    "reasons": ["node has no retained semantic graph edges"],
                    "remediation": {
                        "automatic_apply": False,
                        "tool": "trigger_audit_graph",
                    },
                    "subject_id": node,
                }
            )
        elif len(members) <= 5:
            component_fingerprint = _fingerprint(members).split(":", 1)[1][:16]
            debt_budget.append(
                items,
                {
                    "debt_id": f"sparse-community:{component_fingerprint}",
                    "debt_type": "sparse_community",
                    "disposition": "human_review",
                    "reasons": [f"connected component contains {len(members)} nodes"],
                    "remediation": {
                        "automatic_apply": False,
                        "tool": "trigger_audit_graph",
                    },
                    "subject_ids": members,
                }
            )
    if len(components) > 1:
        debt_budget.append(
            items,
            {
                "debt_id": "fragmented-graph",
                "debt_type": "fragmented_graph",
                "disposition": "human_review",
                "metrics": {
                    "component_count": len(components),
                    "largest_component_nodes": len(components[0]),
                    "node_count": len(nodes),
                },
                "reasons": ["committed graph contains multiple connected components"],
                "remediation": {
                    "automatic_apply": False,
                    "tool": "trigger_audit_graph",
                },
                "subject_id": "committed-graph",
            }
        )
    counts = Counter(str(item["debt_type"]) for item in items)
    return items, dict(sorted(counts.items()))


def _build_campaign_snapshot() -> _CampaignSnapshot:
    cancellation_checkpoint("semantic_campaign:start")
    physical_before = _physical_source_state()
    snapshot_budget = _SnapshotBudget()
    debt_budget = _DebtBudget()
    with db_store.read_only_transaction_snapshot() as connection:
        _require_schema(connection)
        index_data = indexer.read_committed_index_snapshot(
            connection=connection,
            _acquire_lock=False,
        )
        _validate_loaded_projection_bounds(index_data)
        claims = _load_record_map(connection, "claims", "claim_id", snapshot_budget)
        evidence = _load_record_map(
            connection,
            "evidence",
            "evidence_id",
            snapshot_budget,
        )
        sources = _load_record_map(
            connection,
            "sources",
            "source_id",
            snapshot_budget,
        )
        extraction_run_ids, extraction_generation = _extraction_run_snapshot(
            connection,
            snapshot_budget,
        )
        assessments, assessment_generation = _assessment_snapshot(
            connection,
            snapshot_budget,
        )
        binding = _campaign_binding(
            connection,
            index_data,
            {
                "claim_assessments": assessment_generation,
                "extraction_runs": extraction_generation,
            },
            snapshot_budget,
        )
        binding = _bounded_plain_json(
            binding,
            byte_limit=MAX_BINDING_BYTES,
            label="binding",
        )
        claim_items, claim_summary = _claim_debt(
            claims,
            evidence,
            sources,
            extraction_run_ids,
            assessments,
            debt_budget,
        )
        topology_items, topology_by_type = _topology_debt(
            index_data,
            debt_budget,
        )

    cancellation_checkpoint("semantic_campaign:materialized")
    physical_after = _physical_source_state()
    if physical_before != physical_after:
        raise SemanticCampaignContractError(
            "semantic campaign source changed during the initial snapshot; retry"
        )

    items = claim_items
    items.extend(topology_items)
    items.sort(
        key=lambda item: (
            _DEBT_ORDER[str(item["debt_type"])],
            str(item["debt_id"]),
        )
    )
    disposition_counts = Counter(str(item["disposition"]) for item in items)
    low_evidence_ids = claim_summary.pop("low_evidence_claim_ids")
    unassessed_ids = claim_summary.pop("unassessed_claim_ids")
    debt_summary = {
        "automatic_repair": int(disposition_counts.get("automatic_repair", 0)),
        "human_review": int(disposition_counts.get("human_review", 0)),
        "low_evidence_claims": len(low_evidence_ids),
        "topology_by_type": topology_by_type,
        "topology_total": len(topology_items),
        "total_findings": len(items),
        "unassessed_claims": len(unassessed_ids),
        "unique_claims_with_debt": len(low_evidence_ids | unassessed_ids),
    }
    inventory_fingerprint, inventory_bytes = _bounded_fingerprint(
        items,
        byte_limit=MAX_CACHED_INVENTORY_BYTES,
        label="cached debt inventory",
    )
    if inventory_bytes != debt_budget.materialized_bytes:
        raise SemanticCampaignContractError(
            "semantic campaign debt materialization accounting mismatch"
        )
    campaign_identity = {
        "binding": binding,
        "coverage": claim_summary["coverage"],
        "debt": debt_summary,
        "debt_inventory_fingerprint": inventory_fingerprint,
    }
    claim_total = len(claims)
    ready = claim_total > 0 and not items
    source_state = {
        **physical_after,
        "runtime_generations": binding["canonical_generation"][
            "runtime_generations"
        ],
    }
    generation_fingerprint = _generation_fingerprint(source_state)
    source_fingerprint = _fingerprint(source_state)
    campaign_fingerprint = _fingerprint(
        {
            **campaign_identity,
            "generation_fingerprint": generation_fingerprint,
            "source_fingerprint": source_fingerprint,
        }
    )
    readiness = {
        "ready": ready,
        "status": "ready" if ready else ("empty" if claim_total == 0 else "not_ready"),
    }
    scan = {
        "bytes_by_table": {
            table: snapshot_budget.bytes_by_table.get(table, 0)
            for table in sorted(SNAPSHOT_ROW_LIMITS)
        },
        "cached_inventory_bytes": inventory_bytes,
        "debt_materialization_bytes": debt_budget.materialized_bytes,
        "graph_materialization_bytes": int(
            physical_before["projection"]["materialization_bytes"]
        ),
        "json_bytes": snapshot_budget.json_bytes,
        "rows_by_table": {
            table: snapshot_budget.rows_by_table.get(table, 0)
            for table in sorted(SNAPSHOT_ROW_LIMITS)
        },
        "rows_total": snapshot_budget.rows_total,
        "source_bytes": snapshot_budget.source_bytes,
    }
    summary = {
        "assessment_outcomes_current": claim_summary[
            "assessment_outcomes_current"
        ],
        "claims_total": claim_total,
        "claims_with_current_supported_assessment": claim_summary[
            "claims_with_current_supported_assessment"
        ],
        "evidence_total": len(evidence),
        "sources_total": len(sources),
    }
    max_cache_bytes = int(MAX_CAMPAIGN_CACHE_BYTES)
    if max_cache_bytes <= 0:
        raise SemanticCampaignContractError(
            "semantic campaign cache byte limit must be positive"
        )
    _cache_metadata_fingerprint, cache_metadata_bytes = _bounded_fingerprint(
        {
            "binding": binding,
            "campaign_fingerprint": campaign_fingerprint,
            "coverage": claim_summary["coverage"],
            "debt": debt_summary,
            "debt_inventory_fingerprint": inventory_fingerprint,
            "generation_fingerprint": generation_fingerprint,
            "readiness": readiness,
            "scan": scan,
            "source_fingerprint": source_fingerprint,
            "source_state": source_state,
            "summary": summary,
        },
        byte_limit=max_cache_bytes,
        label="cache metadata",
    )
    cache_bytes = inventory_bytes + cache_metadata_bytes + 64
    if cache_bytes > max_cache_bytes:
        raise SemanticCampaignContractError(
            "semantic campaign cache entry byte limit exceeded: "
            f"{cache_bytes} > {max_cache_bytes}"
        )
    lease_seconds = _campaign_cache_ttl_seconds()
    return _CampaignSnapshot(
        binding=binding,
        cache_bytes=cache_bytes,
        campaign_fingerprint=campaign_fingerprint,
        coverage=claim_summary["coverage"],
        debt=debt_summary,
        debt_inventory_fingerprint=inventory_fingerprint,
        expires_at=_campaign_cache_now() + lease_seconds,
        generation_fingerprint=generation_fingerprint,
        items=tuple(items),
        lease_seconds=lease_seconds,
        readiness=readiness,
        scan=scan,
        source_fingerprint=source_fingerprint,
        source_state=source_state,
        summary=summary,
    )


def _validate_cached_snapshot(snapshot: _CampaignSnapshot) -> None:
    try:
        source_state = _current_source_state()
    except (db_store.ReadOnlySnapshotUnavailable, SemanticCampaignContractError) as exc:
        _evict_cached_snapshot(snapshot.campaign_fingerprint)
        raise StaleCampaignCursor(
            "semantic readiness campaign source is unavailable; "
            "restart pagination without a cursor"
        ) from exc
    if snapshot.expires_at <= _campaign_cache_now():
        _evict_cached_snapshot(snapshot.campaign_fingerprint)
        raise StaleCampaignCursor(
            "semantic readiness campaign cache expired; "
            "restart pagination without a cursor"
        )
    if not hmac.compare_digest(
        _generation_fingerprint(source_state),
        snapshot.generation_fingerprint,
    ) or not hmac.compare_digest(
        _fingerprint(source_state),
        snapshot.source_fingerprint,
    ):
        _evict_cached_snapshot(snapshot.campaign_fingerprint)
        raise StaleCampaignCursor(
            "semantic readiness campaign changed; restart pagination without a cursor"
        )


def _matching_cached_snapshot_for_source_locked(
    source_state: dict[str, Any],
) -> _CampaignSnapshot | None:
    now = _campaign_cache_now()
    _prune_expired_snapshots_locked(now)
    generation_fingerprint = _generation_fingerprint(source_state)
    source_fingerprint = _fingerprint(source_state)
    lease_seconds = _campaign_cache_ttl_seconds()
    for campaign_fingerprint in reversed(tuple(_CAMPAIGN_CACHE)):
        snapshot = _CAMPAIGN_CACHE[campaign_fingerprint]
        if not hmac.compare_digest(
            snapshot.generation_fingerprint,
            generation_fingerprint,
        ) or not hmac.compare_digest(
            snapshot.source_fingerprint,
            source_fingerprint,
        ):
            continue
        renewed = replace(
            snapshot,
            expires_at=now + lease_seconds,
            lease_seconds=lease_seconds,
        )
        _CAMPAIGN_CACHE[campaign_fingerprint] = renewed
        _CAMPAIGN_CACHE.move_to_end(campaign_fingerprint)
        return renewed
    return None


def _first_page_snapshot() -> tuple[_CampaignSnapshot, bool]:
    while True:
        source_state = _current_source_state()
        generation_fingerprint = _generation_fingerprint(source_state)
        with _CAMPAIGN_CACHE_LOCK:
            cached = _matching_cached_snapshot_for_source_locked(source_state)
            if cached is not None:
                return cached, True
            flight = _CAMPAIGN_BUILD_FLIGHTS.get(generation_fingerprint)
            is_builder = flight is None
            if flight is None:
                flight = _CampaignBuildFlight()
                _CAMPAIGN_BUILD_FLIGHTS[generation_fingerprint] = flight

        if not is_builder:
            while not flight.event.wait(timeout=_SINGLE_FLIGHT_WAIT_SECONDS):
                cancellation_checkpoint("semantic_campaign:single_flight_wait")
            cancellation_checkpoint("semantic_campaign:single_flight_wait")
            if flight.error is not None:
                raise flight.error
            continue

        try:
            snapshot = _build_campaign_snapshot()
            cancellation_checkpoint("semantic_campaign:before_cache_publish")
            _cache_snapshot(snapshot)
        except CooperativeCancellation:
            # A cancelled builder must not poison same-generation waiters with
            # another operation's cancellation token. They retry the flight.
            raise
        except BaseException as exc:
            with _CAMPAIGN_CACHE_LOCK:
                flight.error = exc
            raise
        finally:
            with _CAMPAIGN_CACHE_LOCK:
                if _CAMPAIGN_BUILD_FLIGHTS.get(generation_fingerprint) is flight:
                    _CAMPAIGN_BUILD_FLIGHTS.pop(generation_fingerprint, None)
                flight.event.set()
        return snapshot, False


def _render_campaign_page(
    snapshot: _CampaignSnapshot,
    *,
    page_size: int,
    cursor_payload: dict[str, Any] | None,
    cache_hit: bool,
) -> dict[str, Any]:
    offset = int(cursor_payload["offset"]) if cursor_payload is not None else 0
    if offset > len(snapshot.items):
        raise ValueError("cursor offset exceeds the current campaign debt inventory")
    page_items = copy.deepcopy(list(snapshot.items[offset : offset + page_size]))
    next_offset = offset + len(page_items)
    next_cursor = (
        _encode_cursor(snapshot.campaign_fingerprint, next_offset)
        if next_offset < len(snapshot.items)
        else None
    )
    page_fingerprint = _fingerprint(
        {
            "campaign_fingerprint": snapshot.campaign_fingerprint,
            "items": page_items,
            "limit": page_size,
            "offset": offset,
        }
    )
    cache_entries, cache_total_bytes = _campaign_cache_usage()
    return {
        "binding": copy.deepcopy(snapshot.binding),
        "campaign_fingerprint": snapshot.campaign_fingerprint,
        "contract": CAMPAIGN_CONTRACT,
        "coverage": copy.deepcopy(snapshot.coverage),
        "debt": copy.deepcopy(snapshot.debt),
        "debt_inventory_fingerprint": snapshot.debt_inventory_fingerprint,
        "page": {
            "has_more": next_cursor is not None,
            "items": page_items,
            "limit": page_size,
            "next_cursor": next_cursor,
            "offset": offset,
            "page_fingerprint": page_fingerprint,
            "returned": len(page_items),
        },
        "read_only": True,
        "readiness": copy.deepcopy(snapshot.readiness),
        "snapshot": {
            "cache": {
                "contract": CAMPAIGN_CACHE_CONTRACT,
                "entries": cache_entries,
                "entry_bytes": snapshot.cache_bytes,
                "hit": cache_hit,
                "lease": "sliding",
                "max_bytes": MAX_CAMPAIGN_CACHE_BYTES,
                "max_entries": MAX_CAMPAIGN_CACHE_ENTRIES,
                "total_bytes": cache_total_bytes,
                "ttl_seconds": snapshot.lease_seconds,
            },
            "current_page_rows_scanned": 0 if cache_hit else snapshot.scan["rows_total"],
            "generation_fingerprint": snapshot.generation_fingerprint,
            "hard_limits": {
                "binding_bytes": MAX_BINDING_BYTES,
                "cache_bytes": MAX_CAMPAIGN_CACHE_BYTES,
                "cached_inventory_bytes": MAX_CACHED_INVENTORY_BYTES,
                "debt_item_bytes": MAX_DEBT_ITEM_BYTES,
                "debt_items": MAX_DEBT_ITEMS,
                "debt_materialization_bytes": MAX_DEBT_MATERIALIZATION_BYTES,
                "findings_per_page": MAX_PAGE_SIZE,
                "graph_materialization_bytes": MAX_GRAPH_MATERIALIZATION_BYTES,
                "json_bytes": MAX_SNAPSHOT_JSON_BYTES,
                "max_pages_at_max_page_size": MAX_PAGES_AT_MAX_PAGE_SIZE,
                "projection_artifact_bytes": MAX_PROJECTION_ARTIFACT_BYTES,
                "projection_bytes_total": MAX_PROJECTION_BYTES_TOTAL,
                "projection_edges": MAX_PROJECTION_EDGES,
                "projection_nodes": MAX_PROJECTION_NODES,
                "projection_sidecar_bytes": MAX_PROJECTION_SIDECAR_BYTES,
                "record_json_bytes": MAX_RECORD_JSON_BYTES,
                "rows_by_table": dict(sorted(SNAPSHOT_ROW_LIMITS.items())),
                "rows_total": MAX_SNAPSHOT_ROWS_TOTAL,
                "source_bytes": MAX_SNAPSHOT_SOURCE_BYTES,
                "text_field_bytes": MAX_SNAPSHOT_TEXT_FIELD_BYTES,
                "wal_bytes": MAX_DATABASE_WAL_BYTES,
            },
            "initial_scan": copy.deepcopy(snapshot.scan),
            "source_fingerprint": snapshot.source_fingerprint,
        },
        "summary": copy.deepcopy(snapshot.summary),
    }


def build_semantic_readiness_campaign_report(
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str = "",
) -> dict[str, Any]:
    """Build one deterministic page bound to current canonical and graph state."""
    page_size = _bounded_page_size(limit)
    cursor_payload = _decode_cursor(cursor)
    if cursor_payload is not None:
        campaign_fingerprint = str(cursor_payload["campaign_fingerprint"])
        snapshot = _cached_snapshot(campaign_fingerprint)
        _validate_cached_snapshot(snapshot)
        snapshot = _renew_cached_snapshot(snapshot)
        return _render_campaign_page(
            snapshot,
            page_size=page_size,
            cursor_payload=cursor_payload,
            cache_hit=True,
        )

    snapshot, cache_hit = _first_page_snapshot()
    return _render_campaign_page(
        snapshot,
        page_size=page_size,
        cursor_payload=None,
        cache_hit=cache_hit,
    )


def semantic_readiness_campaign_report(
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str = "",
) -> str:
    """Return a stable JSON page without initializing or mutating Vector Lake."""
    return json.dumps(
        build_semantic_readiness_campaign_report(limit=limit, cursor=cursor),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


__all__ = [
    "CAMPAIGN_CONTRACT",
    "CURSOR_CONTRACT",
    "MAX_PAGE_SIZE",
    "SemanticCampaignContractError",
    "StaleCampaignCursor",
    "build_semantic_readiness_campaign_report",
    "semantic_readiness_campaign_report",
]
