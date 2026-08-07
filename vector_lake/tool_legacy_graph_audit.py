"""Read-only reconciliation for retired and current SQLite graph surfaces.

The public API accepts a caller-owned connection. It never opens another
database, changes pragmas, commits, closes, deletes, or compacts. Legacy
weighted edges and current predicate-bearing relation edges are intentionally
reported as different semantic models; pair overlap is diagnostic evidence,
not migration authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


__all__ = ["audit_legacy_graph_connection"]

_HASH_CONTRACT = "vector-lake-sqlite-ordered-rowset-sha256-v1"
_DEFAULT_SAMPLE_LIMIT = 20
_MAX_SAMPLE_LIMIT = 100
_FETCH_BATCH_SIZE = 500
_LEGACY_TABLE_COLUMNS = {
    "wiki_nodes": (
        "node_key",
        "title",
        "type",
        "domain",
        "topic_cluster",
        "status",
        "metadata_json",
        "updated_at",
    ),
    "wiki_edges": ("source", "target", "weight"),
}
_CURRENT_REQUIRED_COLUMNS = {
    "entities": ("entity_id", "data_json"),
    "claim_graph_nodes": ("node_id", "data_json", "updated_at"),
    "claim_graph_edges": (
        "source_id",
        "target_id",
        "relation",
        "weight",
        "updated_at",
    ),
    "page_graph_edges": (
        "source_id",
        "target_id",
        "relation",
        "weight",
        "updated_at",
    ),
}
_TABLE_COLUMNS = {**_LEGACY_TABLE_COLUMNS, **_CURRENT_REQUIRED_COLUMNS}


@dataclass(frozen=True)
class _TableSnapshot:
    name: str
    object_type: str | None
    columns: tuple[str, ...]
    selected_columns: tuple[str, ...]
    row_count: int | None
    stable_hash: str | None

    @property
    def is_table(self) -> bool:
        return self.object_type == "table"


@dataclass
class _NodeState:
    keys: set[str] = field(default_factory=set)
    key_counts: dict[str, int] = field(default_factory=dict)
    payloads: dict[str, list[bytes]] = field(default_factory=dict)
    invalid_payloads: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    denormalized_mismatches: set[str] = field(default_factory=set)


@dataclass
class _EdgeState:
    valid_count: int = 0
    source_keys: set[str] = field(default_factory=set)
    target_keys: set[str] = field(default_factory=set)
    directed_pairs: set[tuple[str, str]] = field(default_factory=set)
    undirected_pairs: set[tuple[str, str]] = field(default_factory=set)
    strongest_weights: dict[tuple[str, str], float] = field(default_factory=dict)
    semantic_rows: set[tuple[str, str, str, str]] = field(default_factory=set)
    invalid_rows: list[str] = field(default_factory=list)
    self_loop_pairs: set[tuple[str, str]] = field(default_factory=set)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _row_value(row: Any, index: int, key: str) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _row_values(row: Any, columns: tuple[str, ...]) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return tuple(row[column] for column in columns)
    return tuple(row)


def _typed_sqlite_value(value: Any) -> list[str]:
    if value is None:
        return ["null", ""]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    return [f"python:{type(value).__name__}", repr(value)]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _update_digest(digest: Any, encoded: bytes) -> None:
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _evidence(label: str, values: Iterable[Any], sample_limit: int) -> dict[str, Any]:
    unique = {_canonical_json_bytes(value): _json_safe(value) for value in values}
    ordered = sorted(unique)
    digest = hashlib.sha256()
    _update_digest(
        digest,
        _canonical_json_bytes({"contract": _HASH_CONTRACT, "label": label}),
    )
    for encoded in ordered:
        _update_digest(digest, encoded)
    return {
        "count": len(ordered),
        "stable_hash": "sha256:" + digest.hexdigest(),
        "sample": [unique[encoded] for encoded in ordered[:sample_limit]],
    }


def _strict_json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError("JSON payload is not text")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    parsed = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(parsed, dict):
        raise ValueError("JSON payload is not an object")
    return parsed


def _normalise_key(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value)
    return key if key.strip() else None


def _normalise_weight(value: Any) -> float | None:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return None
    return weight if math.isfinite(weight) else None


def _pair(source: str, target: str, *, directed: bool) -> tuple[str, str]:
    return (source, target) if directed else tuple(sorted((source, target)))


def _read_table_snapshot(
    connection: sqlite3.Connection,
    table_name: str,
    selected_columns: tuple[str, ...],
    consume_row: Callable[[Mapping[str, Any], int], None] | None = None,
) -> _TableSnapshot:
    """Stream one known table in deterministic key-first column order."""
    object_row = connection.execute(
        "SELECT type FROM main.sqlite_master WHERE name = ?",
        (table_name,),
    ).fetchone()
    if object_row is None:
        return _TableSnapshot(table_name, None, (), selected_columns, None, None)

    object_type = str(_row_value(object_row, 0, "type") or "")
    if object_type != "table":
        return _TableSnapshot(
            table_name,
            object_type,
            (),
            selected_columns,
            None,
            None,
        )

    quoted_table = _quote_identifier(table_name)
    column_rows = connection.execute(
        f"PRAGMA main.table_info({quoted_table})"
    ).fetchall()
    columns = tuple(
        str(_row_value(row, 1, "name") or "") for row in column_rows
    )
    if not set(selected_columns).issubset(columns):
        return _TableSnapshot(
            table_name,
            object_type,
            columns,
            selected_columns,
            None,
            None,
        )

    projection = ", ".join(_quote_identifier(column) for column in selected_columns)
    ordering = ", ".join(_quote_identifier(column) for column in selected_columns)
    cursor = connection.execute(
        f"SELECT {projection} FROM main.{quoted_table} ORDER BY {ordering}"
    )
    digest = hashlib.sha256()
    _update_digest(
        digest,
        _canonical_json_bytes(
            {
                "contract": _HASH_CONTRACT,
                "table": table_name,
                "columns": selected_columns,
            }
        ),
    )
    row_count = 0
    while True:
        batch = cursor.fetchmany(_FETCH_BATCH_SIZE)
        if not batch:
            break
        for row in batch:
            values = _row_values(row, selected_columns)
            _update_digest(
                digest,
                _canonical_json_bytes(
                    [_typed_sqlite_value(value) for value in values]
                ),
            )
            row_count += 1
            if consume_row is not None:
                consume_row(dict(zip(selected_columns, values)), row_count)
    return _TableSnapshot(
        table_name,
        object_type,
        columns,
        selected_columns,
        row_count,
        "sha256:" + digest.hexdigest(),
    )


def _consume_legacy_node(state: _NodeState) -> Callable[[Mapping[str, Any], int], None]:
    def consume(row: Mapping[str, Any], row_number: int) -> None:
        key = _normalise_key(row["node_key"])
        row_id = key or f"row:{row_number}"
        if key is None:
            state.missing_keys.append(row_id)
            return
        state.keys.add(key)
        state.key_counts[key] = state.key_counts.get(key, 0) + 1
        try:
            payload = _strict_json_object(row["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            state.invalid_payloads.append(row_id)
            return
        encoded = _canonical_json_bytes(payload)
        state.payloads.setdefault(key, []).append(encoded)
        for column in ("title", "type", "domain", "topic_cluster", "status"):
            if str(row[column] or "") != str(payload.get(column, "") or ""):
                state.denormalized_mismatches.add(key)

    return consume


def _consume_entity(state: _NodeState) -> Callable[[Mapping[str, Any], int], None]:
    def consume(row: Mapping[str, Any], row_number: int) -> None:
        entity_id = _normalise_key(row["entity_id"])
        row_id = entity_id or f"row:{row_number}"
        try:
            payload = _strict_json_object(row["data_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            state.invalid_payloads.append(row_id)
            return
        page_key = _normalise_key(payload.get("page_key"))
        if page_key is None:
            state.missing_keys.append(row_id)
            return
        state.keys.add(page_key)
        state.key_counts[page_key] = state.key_counts.get(page_key, 0) + 1
        state.payloads.setdefault(page_key, []).append(_canonical_json_bytes(payload))

    return consume


def _consume_edge(
    state: _EdgeState,
    *,
    source_column: str,
    target_column: str,
    relation_column: str | None,
) -> Callable[[Mapping[str, Any], int], None]:
    def consume(row: Mapping[str, Any], row_number: int) -> None:
        source = _normalise_key(row[source_column])
        target = _normalise_key(row[target_column])
        relation = (
            _normalise_key(row[relation_column])
            if relation_column is not None
            else None
        )
        weight = _normalise_weight(row["weight"])
        if (
            source is None
            or target is None
            or weight is None
            or (relation_column is not None and relation is None)
        ):
            state.invalid_rows.append(f"row:{row_number}")
            return
        state.valid_count += 1
        state.source_keys.add(source)
        state.target_keys.add(target)
        directed_pair = _pair(source, target, directed=True)
        undirected_pair = _pair(source, target, directed=False)
        state.directed_pairs.add(directed_pair)
        state.undirected_pairs.add(undirected_pair)
        previous = state.strongest_weights.get(undirected_pair)
        if previous is None or weight > previous:
            state.strongest_weights[undirected_pair] = weight
        if relation_column is not None:
            state.semantic_rows.add((source, target, str(relation), weight.hex()))
        if source == target:
            state.self_loop_pairs.add(directed_pair)

    return consume


def _schema_issues(snapshots: Mapping[str, _TableSnapshot]) -> list[str]:
    issues: list[str] = []
    for table_name, expected_columns in _LEGACY_TABLE_COLUMNS.items():
        snapshot = snapshots[table_name]
        if snapshot.object_type is None:
            continue
        if not snapshot.is_table:
            issues.append(
                f"legacy_object_type_mismatch:{table_name}:{snapshot.object_type}"
            )
        elif snapshot.columns != expected_columns:
            issues.append(f"legacy_schema_mismatch:{table_name}")
    for table_name, required_columns in _CURRENT_REQUIRED_COLUMNS.items():
        snapshot = snapshots[table_name]
        if snapshot.object_type is None:
            issues.append(f"current_table_missing:{table_name}")
        elif not snapshot.is_table:
            issues.append(
                f"current_object_type_mismatch:{table_name}:{snapshot.object_type}"
            )
        else:
            missing = sorted(set(required_columns) - set(snapshot.columns))
            if missing:
                issues.append(
                    f"current_schema_missing_columns:{table_name}:{','.join(missing)}"
                )
    return issues


def _node_report(
    legacy: _NodeState,
    canonical: _NodeState,
    sample_limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    covered = legacy.keys & canonical.keys
    legacy_only = legacy.keys - canonical.keys
    canonical_only = canonical.keys - legacy.keys
    duplicate_legacy = {
        key for key, count in legacy.key_counts.items() if count > 1
    }
    duplicate_canonical = {
        key for key, count in canonical.key_counts.items() if count > 1
    }
    payload_matches: set[str] = set()
    payload_mismatches: set[str] = set()
    for key in covered:
        legacy_payloads = legacy.payloads.get(key, [])
        canonical_payloads = canonical.payloads.get(key, [])
        if (
            len(legacy_payloads) == 1
            and len(canonical_payloads) == 1
            and legacy_payloads[0] == canonical_payloads[0]
        ):
            payload_matches.add(key)
        else:
            payload_mismatches.add(key)

    facts = {
        "legacy_keys": legacy.keys,
        "canonical_page_keys": canonical.keys,
        "covered_keys": covered,
        "legacy_only_keys": legacy_only,
        "canonical_only_keys": canonical_only,
        "payload_match_keys": payload_matches,
        "payload_mismatch_keys": payload_mismatches,
        "duplicate_legacy_keys": duplicate_legacy,
        "duplicate_canonical_page_keys": duplicate_canonical,
        "invalid_legacy_metadata": legacy.invalid_payloads,
        "invalid_entity_data": canonical.invalid_payloads,
        "missing_legacy_keys": legacy.missing_keys,
        "missing_entity_page_keys": canonical.missing_keys,
        "legacy_denormalized_mismatch_keys": legacy.denormalized_mismatches,
    }
    report = {
        name: _evidence(f"node:{name}", values, sample_limit)
        for name, values in facts.items()
    }
    report["coverage_ratio"] = len(covered) / len(legacy.keys) if legacy.keys else 1.0
    return report, facts


def _merged_strongest_weights(*states: _EdgeState) -> dict[tuple[str, str], float]:
    merged: dict[tuple[str, str], float] = {}
    for state in states:
        for pair, weight in state.strongest_weights.items():
            current = merged.get(pair)
            if current is None or weight > current:
                merged[pair] = weight
    return merged


def _edge_report(
    legacy: _EdgeState,
    claim: _EdgeState,
    page: _EdgeState,
    *,
    legacy_node_keys: set[str],
    canonical_page_keys: set[str],
    sample_limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    relation_pairs = claim.undirected_pairs | page.undirected_pairs
    relation_directed = claim.directed_pairs | page.directed_pairs
    overlap = legacy.undirected_pairs & relation_pairs
    relation_weights = _merged_strongest_weights(claim, page)
    weight_mismatches = {
        pair
        for pair in overlap
        if legacy.strongest_weights[pair].hex() != relation_weights[pair].hex()
    }
    endpoints = legacy.source_keys | legacy.target_keys
    facts = {
        "legacy_only_pairs": legacy.undirected_pairs - relation_pairs,
        "relation_only_pairs": relation_pairs - legacy.undirected_pairs,
        "overlap_pairs": overlap,
        "weight_mismatch_pairs": weight_mismatches,
        "claim_only_pairs": claim.directed_pairs - page.directed_pairs,
        "page_only_pairs": page.directed_pairs - claim.directed_pairs,
        "claim_only_semantic_rows": claim.semantic_rows - page.semantic_rows,
        "page_only_semantic_rows": page.semantic_rows - claim.semantic_rows,
        "missing_from_legacy_nodes": endpoints - legacy_node_keys,
        "missing_from_canonical_page_keys": endpoints - canonical_page_keys,
    }
    endpoint_report = {
        "source_keys": _evidence(
            "edge:legacy-source-keys", legacy.source_keys, sample_limit
        ),
        "target_keys": _evidence(
            "edge:legacy-target-keys", legacy.target_keys, sample_limit
        ),
        "all_keys": _evidence("edge:legacy-endpoints", endpoints, sample_limit),
        "missing_from_legacy_nodes": _evidence(
            "edge:missing-from-legacy-nodes",
            facts["missing_from_legacy_nodes"],
            sample_limit,
        ),
        "missing_from_canonical_page_keys": _evidence(
            "edge:missing-from-canonical-pages",
            facts["missing_from_canonical_page_keys"],
            sample_limit,
        ),
    }
    pair_report = {
        "primary_mode": "undirected_consumer_pair",
        "legacy_pairs": _evidence(
            "edge:legacy-undirected-pairs", legacy.undirected_pairs, sample_limit
        ),
        "relation_pairs": _evidence(
            "edge:relation-undirected-pairs", relation_pairs, sample_limit
        ),
        "overlap_pairs": _evidence("edge:overlap-pairs", overlap, sample_limit),
        "legacy_only_pairs": _evidence(
            "edge:legacy-only-pairs", facts["legacy_only_pairs"], sample_limit
        ),
        "relation_only_pairs": _evidence(
            "edge:relation-only-pairs", facts["relation_only_pairs"], sample_limit
        ),
        "coverage_ratio": (
            len(overlap) / len(legacy.undirected_pairs)
            if legacy.undirected_pairs
            else 1.0
        ),
        "directed_legacy_pairs": _evidence(
            "edge:legacy-directed-pairs", legacy.directed_pairs, sample_limit
        ),
        "directed_relation_pairs": _evidence(
            "edge:relation-directed-pairs", relation_directed, sample_limit
        ),
        "directed_overlap_pairs": _evidence(
            "edge:directed-overlap",
            legacy.directed_pairs & relation_directed,
            sample_limit,
        ),
        "weight_mismatch_pairs": _evidence(
            "edge:weight-mismatch-pairs", weight_mismatches, sample_limit
        ),
    }
    relation_diff = {
        name: _evidence(f"edge:{name}", facts[name], sample_limit)
        for name in (
            "claim_only_pairs",
            "page_only_pairs",
            "claim_only_semantic_rows",
            "page_only_semantic_rows",
        )
    }
    invalid_rows = {
        "wiki_edges": _evidence(
            "edge:invalid:wiki_edges", legacy.invalid_rows, sample_limit
        ),
        "claim_graph_edges": _evidence(
            "edge:invalid:claim_graph_edges", claim.invalid_rows, sample_limit
        ),
        "page_graph_edges": _evidence(
            "edge:invalid:page_graph_edges", page.invalid_rows, sample_limit
        ),
    }
    self_loops = {
        "wiki_edges": _evidence(
            "edge:self-loop:wiki_edges", legacy.self_loop_pairs, sample_limit
        ),
        "claim_graph_edges": _evidence(
            "edge:self-loop:claim_graph_edges", claim.self_loop_pairs, sample_limit
        ),
        "page_graph_edges": _evidence(
            "edge:self-loop:page_graph_edges", page.self_loop_pairs, sample_limit
        ),
    }
    return {
        "legacy_edge_endpoints": endpoint_report,
        "relation_graph_pair_overlap": pair_report,
        "claim_page_relation_diff": relation_diff,
        "invalid_rows": invalid_rows,
        "self_loop_pairs": self_loops,
    }, facts


def _normalise_external_findings(findings: Iterable[Any] | Any | None) -> list[Any]:
    if findings is None:
        return []
    if isinstance(findings, (str, bytes, Mapping)):
        items = [findings]
    else:
        try:
            items = list(findings)
        except TypeError:
            items = [findings]
    return [_json_safe(item) for item in items]


def _table_public(snapshot: _TableSnapshot) -> dict[str, Any]:
    return {
        "exists": snapshot.object_type is not None,
        "is_table": snapshot.is_table,
        "object_type": snapshot.object_type,
        "columns": list(snapshot.columns),
        "hashed_columns": list(snapshot.selected_columns),
        "row_count": snapshot.row_count,
        "stable_hash": snapshot.stable_hash,
    }


def audit_legacy_graph_connection(
    connection: sqlite3.Connection,
    *,
    external_consumer_findings: Iterable[Any] | Any | None = None,
    sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Build a bounded, fail-closed retirement audit on a caller-owned connection."""
    if isinstance(sample_limit, bool) or not isinstance(sample_limit, int):
        raise TypeError("sample_limit must be an integer")
    if sample_limit < 0 or sample_limit > _MAX_SAMPLE_LIMIT:
        raise ValueError(f"sample_limit must be between 0 and {_MAX_SAMPLE_LIMIT}")

    legacy_nodes = _NodeState()
    canonical_nodes = _NodeState()
    legacy_edges = _EdgeState()
    claim_edges = _EdgeState()
    page_edges = _EdgeState()
    consumers: dict[str, Callable[[Mapping[str, Any], int], None] | None] = {
        "wiki_nodes": _consume_legacy_node(legacy_nodes),
        "wiki_edges": _consume_edge(
            legacy_edges,
            source_column="source",
            target_column="target",
            relation_column=None,
        ),
        "entities": _consume_entity(canonical_nodes),
        "claim_graph_nodes": None,
        "claim_graph_edges": _consume_edge(
            claim_edges,
            source_column="source_id",
            target_column="target_id",
            relation_column="relation",
        ),
        "page_graph_edges": _consume_edge(
            page_edges,
            source_column="source_id",
            target_column="target_id",
            relation_column="relation",
        ),
    }
    snapshots = {
        table_name: _read_table_snapshot(
            connection,
            table_name,
            selected_columns,
            consumers[table_name],
        )
        for table_name, selected_columns in _TABLE_COLUMNS.items()
    }
    schema_issues = _schema_issues(snapshots)
    node_coverage, node_facts = _node_report(
        legacy_nodes,
        canonical_nodes,
        sample_limit,
    )
    edge_report, edge_facts = _edge_report(
        legacy_edges,
        claim_edges,
        page_edges,
        legacy_node_keys=legacy_nodes.keys,
        canonical_page_keys=canonical_nodes.keys,
        sample_limit=sample_limit,
    )
    external_findings = _normalise_external_findings(external_consumer_findings)
    external_evidence = _evidence(
        "external-consumer-findings", external_findings, sample_limit
    )

    blockers = list(schema_issues)
    if external_evidence["count"]:
        blockers.append(
            f"external_consumers_detected:{external_evidence['count']}"
        )
    node_blocker_fields = {
        "legacy_only_keys": "legacy_node_coverage_gap",
        "payload_mismatch_keys": "legacy_node_payload_mismatch",
        "duplicate_legacy_keys": "duplicate_legacy_node_keys",
        "duplicate_canonical_page_keys": "duplicate_canonical_page_keys",
        "invalid_legacy_metadata": "invalid_legacy_node_metadata",
        "invalid_entity_data": "invalid_canonical_entity_data",
        "missing_legacy_keys": "missing_legacy_node_key",
        "missing_entity_page_keys": "missing_canonical_page_key",
        "legacy_denormalized_mismatch_keys": "legacy_node_column_mismatch",
    }
    for field_name, blocker_code in node_blocker_fields.items():
        count = len(node_facts[field_name])
        if count:
            blockers.append(f"{blocker_code}:{count}")

    for fact_name, blocker_code in (
        ("missing_from_legacy_nodes", "legacy_edge_endpoint_missing_from_legacy_nodes"),
        (
            "missing_from_canonical_page_keys",
            "legacy_edge_endpoint_missing_from_canonical_pages",
        ),
        ("legacy_only_pairs", "legacy_relation_pair_gap"),
        ("weight_mismatch_pairs", "legacy_relation_weight_mismatch"),
    ):
        count = len(edge_facts[fact_name])
        if count:
            blockers.append(f"{blocker_code}:{count}")
    if edge_facts["claim_only_semantic_rows"] or edge_facts["page_only_semantic_rows"]:
        blockers.append("current_relation_graph_dual_write_divergence")
    for table_name, state in (
        ("wiki_edges", legacy_edges),
        ("claim_graph_edges", claim_edges),
        ("page_graph_edges", page_edges),
    ):
        if state.invalid_rows:
            blockers.append(f"invalid_graph_edge_rows:{table_name}:{len(state.invalid_rows)}")
    if legacy_edges.self_loop_pairs:
        blockers.append(
            f"legacy_self_loop_semantic_gap:{len(legacy_edges.self_loop_pairs)}"
        )

    legacy_edge_rows = snapshots["wiki_edges"].row_count or 0
    graph_models_equivalent = legacy_edge_rows == 0
    if not graph_models_equivalent:
        blockers.append("graph_semantics_not_equivalent")

    blockers = list(dict.fromkeys(blockers))
    node_semantic_equivalent = not any(
        node_facts[field_name] for field_name in node_blocker_fields
    )
    pair_projection_equivalent = not any(
        (
            edge_facts["missing_from_legacy_nodes"],
            edge_facts["missing_from_canonical_page_keys"],
            edge_facts["legacy_only_pairs"],
            edge_facts["weight_mismatch_pairs"],
            edge_facts["claim_only_semantic_rows"],
            edge_facts["page_only_semantic_rows"],
            legacy_edges.invalid_rows,
            claim_edges.invalid_rows,
            page_edges.invalid_rows,
            legacy_edges.self_loop_pairs,
        )
    )
    semantic_equivalent = (
        not schema_issues
        and node_semantic_equivalent
        and pair_projection_equivalent
        and graph_models_equivalent
    )

    table_report = {
        table_name: _table_public(snapshot)
        for table_name, snapshot in snapshots.items()
    }
    baseline_basis = {
        "contract": "legacy-graph-read-only-audit-v1",
        "tables": {
            name: {
                "row_count": value["row_count"],
                "stable_hash": value["stable_hash"],
                "columns": value["columns"],
                "hashed_columns": value["hashed_columns"],
            }
            for name, value in table_report.items()
        },
        "node_hashes": {
            name: value["stable_hash"]
            for name, value in node_coverage.items()
            if isinstance(value, dict) and "stable_hash" in value
        },
        "edge_hashes": {
            "endpoints": edge_report["legacy_edge_endpoints"]["all_keys"][
                "stable_hash"
            ],
            "legacy_pairs": edge_report["relation_graph_pair_overlap"][
                "legacy_pairs"
            ]["stable_hash"],
            "relation_pairs": edge_report["relation_graph_pair_overlap"][
                "relation_pairs"
            ]["stable_hash"],
        },
        "external_findings_hash": external_evidence["stable_hash"],
        "schema_issues": schema_issues,
        "deletion_blockers": blockers,
    }
    baseline_fingerprint = _evidence(
        "legacy-graph-baseline", [baseline_basis], 0
    )["stable_hash"]

    return {
        "contract": "legacy-graph-read-only-audit-v1",
        "read_only": True,
        "caller_owned_connection": True,
        "sample_limit": sample_limit,
        "hash_contract": _HASH_CONTRACT,
        "baseline_fingerprint": baseline_fingerprint,
        "tables": table_report,
        "node_coverage": node_coverage,
        **edge_report,
        "external_consumer_findings": external_evidence,
        "schema_issues": schema_issues,
        "semantic_equivalence": {
            "nodes": node_semantic_equivalent,
            "legacy_to_relation_pair_projection": pair_projection_equivalent,
            "graph_models": graph_models_equivalent,
            "overall": semantic_equivalent,
            "comparison_rules": [
                "legacy node JSON must exactly match one canonical entity payload",
                "legacy pair overlap uses undirected index-consumer semantics",
                "legacy strongest pair weights are compared only as diagnostic coverage",
                "weighted wiki_edges are not predicate-bearing relation graph rows",
                "claim_graph_edges and page_graph_edges must be semantically equal",
            ],
        },
        "deletion_blockers": blockers,
        "deletion_ready": not blockers,
    }
