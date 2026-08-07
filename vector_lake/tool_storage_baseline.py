"""Read-only structural baselines for the public FTS5 and vec0 surfaces.

The caller owns the SQLite connection.  This module does not initialize,
commit, roll back, close, or change connection pragmas.  All inspection uses
the public virtual-table names; implementation-owned shadow tables are never
queried.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from vector_lake.tokenizer_runtime import tokenize_for_fts


STORAGE_BASELINE_CONTRACT = "vector-lake-storage-baseline-v1"
WIKI_FTS_TARGET_DDL = """
CREATE VIRTUAL TABLE wiki_search_index USING fts5(
    node_key, title, summary, text,
    tokenize='unicode61 remove_diacritics 1'
)
"""
VEC0_TARGET_DDL = """
CREATE VIRTUAL TABLE vec_embeddings USING vec0(
    entity_id TEXT PRIMARY KEY,
    embedding float[3072]
)
"""

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKENIZER_CLAUSE = re.compile(
    r"\btokenize\s*=\s*(?:'((?:''|[^'])*)'|\"((?:\"\"|[^\"])*)\")",
    re.IGNORECASE,
)
_TOKENIZER_METADATA_FIELDS = (
    "engine",
    "package_version",
    "segmentation_recipe",
)
_PROJECTION_AUTHORITY_FIELDS = (
    "status",
    "contract",
    "generation",
    "canonical_generation_token",
    "manifest_sha256",
    "index_sha256",
    "claim_graph_sha256",
    "expected_node_keyset_sha256",
    "expected_fts_corpus_sha256",
)
_PROJECTION_HASH_FIELDS = (
    "canonical_generation_token",
    "manifest_sha256",
    "index_sha256",
    "claim_graph_sha256",
    "expected_node_keyset_sha256",
    "expected_fts_corpus_sha256",
)
_PROJECTION_AUTHORITY_CONTRACT = "index-claim-graph-sidecar@1"
_PROJECTION_SIDECAR_CONTRACT = "index-claim-graph-sidecar"
_PROJECTION_PAIR_CONTRACT = "index-claim-graph-pair"
_CANONICAL_GENERATION_ALGORITHM = "runtime-generations-sha256-v2"
_PROJECTION_ARTIFACT_NAMES = (
    "projection_pair_manifest.json",
    "index.json",
    "claim_graph.json",
)
_CANONICAL_PROJECTION_SURFACES = (
    "entities",
    "claims",
    "sources",
    "page_graph_edges",
    "claim_graph_edges",
)
_HEX_GENERATION = re.compile(r"^[0-9a-f]{32}$")
_VECTOR_DIMENSION = re.compile(
    r"\bembedding\s+float\s*\[\s*(\d+)\s*\]",
    re.IGNORECASE,
)
_MAX_QUERY_PROBES = 128
_MAX_QUERY_LENGTH = 4096
_MAX_EXPECTED_IDS_PER_PROBE = 256
_MAX_IDENTIFIER_UTF8_BYTES = 1024
_MAX_PROJECTION_METADATA_FIELDS = len(_PROJECTION_AUTHORITY_FIELDS)
_MAX_PROJECTION_FIELD_NAME_BYTES = 128
_MAX_PROJECTION_METADATA_VALUE_BYTES = 256
_MAX_PROJECTION_ARTIFACT_BYTES = 128 * 1024 * 1024
_MAX_PROJECTION_ARTIFACT_TOTAL_BYTES = 192 * 1024 * 1024
_MAX_PROJECTION_JSON_DEPTH = 64
_MAX_PROJECTION_JSON_ITEMS = 2_000_000
_MAX_TOKENIZER_METADATA_VALUE_BYTES = 256
_MAX_TOKENIZER_RECIPE_DEPTH = 16
_MAX_TOKENIZER_RECIPE_ITEMS = 512
_MAX_TOKENIZER_RECIPE_TEXT_BYTES = 16 * 1024
_MAX_VECTOR_METADATA_DEPTH = 8
_MAX_VECTOR_METADATA_ITEMS = 1_000_000
_MAX_VECTOR_METADATA_TEXT_BYTES = 64 * 1024 * 1024


def _validated_identifier(value: Any, *, context: str) -> str:
    identifier = str(value)
    if len(identifier.encode("utf-8")) > _MAX_IDENTIFIER_UTF8_BYTES:
        raise ValueError(
            f"{context} exceeds {_MAX_IDENTIFIER_UTF8_BYTES} UTF-8 bytes"
        )
    return identifier


def _reported_identifier(value: Any) -> str:
    identifier = str(value)
    byte_length = len(identifier.encode("utf-8"))
    if byte_length <= _MAX_IDENTIFIER_UTF8_BYTES:
        return identifier
    return (
        f"<oversize-id bytes={byte_length} "
        f"sha256={_sha256_text(identifier)}>"
    )


def _normalize_ddl(value: str | None) -> str:
    if not value:
        return ""
    normalized = re.sub(
        r"\bif\s+not\s+exists\b",
        "",
        str(value),
        flags=re.IGNORECASE,
    )
    normalized = " ".join(normalized.split()).casefold()
    normalized = re.sub(r"\s*([(),\[\]])\s*", r"\1", normalized)
    return normalized.strip().rstrip(";")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    """Return a deterministic, strict-JSON-compatible representation."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_safe(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    return str(value)


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256_text(payload)


def _validate_bounded_structure(
    value: Any,
    *,
    context: str,
    max_depth: int,
    max_items: int,
    max_text_bytes: int,
    max_key_bytes: int,
) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    item_count = 0
    text_bytes = 0
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            raise ValueError(f"{context} exceeds depth {max_depth}")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen_containers:
                raise ValueError(f"{context} contains repeated or cyclic containers")
            seen_containers.add(identity)
            item_count += len(item)
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise TypeError(f"{context} keys must be strings")
                key_bytes = len(key.encode("utf-8"))
                if key_bytes > max_key_bytes:
                    raise ValueError(f"{context} contains an oversized key")
                text_bytes += key_bytes
                stack.append((nested, depth + 1))
        elif isinstance(item, (list, tuple, set, frozenset)):
            identity = id(item)
            if identity in seen_containers:
                raise ValueError(f"{context} contains repeated or cyclic containers")
            seen_containers.add(identity)
            item_count += len(item)
            stack.extend((nested, depth + 1) for nested in item)
        elif isinstance(item, str):
            text_bytes += len(item.encode("utf-8"))
        elif item is None or isinstance(item, (bool, int, float)):
            pass
        else:
            raise TypeError(f"{context} contains an unsupported value")
        if item_count > max_items:
            raise ValueError(f"{context} exceeds item budget {max_items}")
        if text_bytes > max_text_bytes:
            raise ValueError(f"{context} exceeds text budget {max_text_bytes}")


def _normalize_tokenizer_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise TypeError("tokenizer_metadata must be a mapping")
    if len(metadata) > len(_TOKENIZER_METADATA_FIELDS):
        raise ValueError("tokenizer_metadata has too many fields")
    for key in metadata:
        if not isinstance(key, str) or key not in _TOKENIZER_METADATA_FIELDS:
            raise ValueError("tokenizer_metadata contains an unsupported field")
    normalized = dict(metadata)
    for field in ("engine", "package_version"):
        value = normalized.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError(f"tokenizer_metadata.{field} must be a string")
        if len(value.encode("utf-8")) > _MAX_TOKENIZER_METADATA_VALUE_BYTES:
            raise ValueError(f"tokenizer_metadata.{field} is too large")
    recipe = normalized.get("segmentation_recipe")
    if recipe is not None:
        _validate_bounded_structure(
            recipe,
            context="tokenizer_metadata.segmentation_recipe",
            max_depth=_MAX_TOKENIZER_RECIPE_DEPTH,
            max_items=_MAX_TOKENIZER_RECIPE_ITEMS,
            max_text_bytes=_MAX_TOKENIZER_RECIPE_TEXT_BYTES,
            max_key_bytes=_MAX_PROJECTION_FIELD_NAME_BYTES,
        )
    return normalized


def _validate_vector_metadata(
    metadata: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, Mapping):
        raise TypeError("vector_metadata must be a mapping")
    for entity_id, record in metadata.items():
        if not isinstance(entity_id, str):
            raise TypeError("vector_metadata IDs must be strings")
        _validated_identifier(entity_id, context="vector metadata ID")
        if not isinstance(record, Mapping):
            raise TypeError(
                f"vector_metadata record for {entity_id!r} must be a mapping"
            )
    _validate_bounded_structure(
        metadata,
        context="vector_metadata",
        max_depth=_MAX_VECTOR_METADATA_DEPTH,
        max_items=_MAX_VECTOR_METADATA_ITEMS,
        max_text_bytes=_MAX_VECTOR_METADATA_TEXT_BYTES,
        max_key_bytes=_MAX_IDENTIFIER_UTF8_BYTES,
    )


def _tokenizer_metadata_baseline(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = dict(metadata or {})
    normalized: dict[str, Any] = {}
    missing: list[str] = []
    for field in _TOKENIZER_METADATA_FIELDS:
        value = source.get(field)
        if field in {"engine", "package_version"}:
            value = str(value or "").strip()
        else:
            value = _json_safe(value)
        if value in (None, "", (), [], {}):
            missing.append(field)
        else:
            normalized[field] = value
    return {
        "required": list(_TOKENIZER_METADATA_FIELDS),
        "provided": metadata is not None,
        "engine": normalized.get("engine"),
        "package_version": normalized.get("package_version"),
        "segmentation_recipe": normalized.get("segmentation_recipe"),
        "stable_sha256": _canonical_json_sha256(normalized),
        "missing_fields": missing,
        "complete": not missing,
    }


def _normalize_projection_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, str]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("projection_metadata must be a mapping")
    if len(metadata) > _MAX_PROJECTION_METADATA_FIELDS:
        raise ValueError("projection_metadata has too many fields")
    for key in metadata:
        if (
            not isinstance(key, str)
            or len(key.encode("utf-8")) > _MAX_PROJECTION_FIELD_NAME_BYTES
            or key not in _PROJECTION_AUTHORITY_FIELDS
        ):
            raise ValueError("projection_metadata contains an unsupported field")
    normalized: dict[str, str] = {}
    for field in _PROJECTION_AUTHORITY_FIELDS:
        value = metadata.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError(f"projection_metadata.{field} must be a string")
        if len(value.encode("utf-8")) > _MAX_PROJECTION_METADATA_VALUE_BYTES:
            raise ValueError(
                f"projection_metadata.{field} exceeds "
                f"{_MAX_PROJECTION_METADATA_VALUE_BYTES} UTF-8 bytes"
            )
        normalized[field] = value
    return normalized


def _normalize_projection_artifacts(
    artifacts: Mapping[str, bytes | bytearray | memoryview] | None,
) -> dict[str, bytes]:
    if artifacts is None:
        return {}
    if not isinstance(artifacts, Mapping):
        raise TypeError("projection_artifacts must be a mapping")
    if len(artifacts) > len(_PROJECTION_ARTIFACT_NAMES):
        raise ValueError("projection_artifacts has too many entries")
    for key in artifacts:
        if (
            not isinstance(key, str)
            or len(key.encode("utf-8")) > _MAX_PROJECTION_FIELD_NAME_BYTES
            or key not in _PROJECTION_ARTIFACT_NAMES
        ):
            raise ValueError("projection_artifacts contains an unsupported name")
    views: dict[str, memoryview] = {}
    total_bytes = 0
    for name in _PROJECTION_ARTIFACT_NAMES:
        if name not in artifacts:
            continue
        payload = artifacts[name]
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError(f"projection_artifacts[{name!r}] must contain bytes")
        view = memoryview(payload)
        payload_size = view.nbytes
        if payload_size > _MAX_PROJECTION_ARTIFACT_BYTES:
            raise ValueError(
                f"projection artifact {name} exceeds "
                f"{_MAX_PROJECTION_ARTIFACT_BYTES} bytes"
            )
        total_bytes += payload_size
        if total_bytes > _MAX_PROJECTION_ARTIFACT_TOTAL_BYTES:
            raise ValueError(
                "projection artifacts exceed the aggregate byte budget"
            )
        views[name] = view
    return {name: view.tobytes() for name, view in views.items()}


def _preflight_json_budget(payload: bytes, *, context: str) -> None:
    depth = 0
    structural_items = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            structural_items += 1
            if depth > _MAX_PROJECTION_JSON_DEPTH:
                raise ValueError(
                    f"{context} exceeds JSON depth {_MAX_PROJECTION_JSON_DEPTH}"
                )
        elif byte in (0x7D, 0x5D):
            depth -= 1
        elif byte in (0x2C, 0x3A):
            structural_items += 1
        if structural_items > _MAX_PROJECTION_JSON_ITEMS:
            raise ValueError(
                f"{context} exceeds JSON structural budget "
                f"{_MAX_PROJECTION_JSON_ITEMS}"
            )


def _bounded_json_object(payload: bytes, *, context: str) -> dict[str, Any]:
    _preflight_json_budget(payload, context=context)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{context} is not valid bounded JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{context} must contain a JSON object")
    return decoded


def _canonical_generation_token(snapshot: Mapping[str, int]) -> str:
    payload = json.dumps(
        dict(snapshot),
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


def _projection_rows_from_index(index_data: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    nodes = index_data.get("nodes")
    if not isinstance(nodes, Mapping):
        raise ValueError("index.json nodes must be a mapping")
    rows: list[tuple[str, str, str, str]] = []
    for raw_node_key, raw_node in nodes.items():
        node_key = _validated_identifier(
            raw_node_key,
            context="projection index node ID",
        )
        if not isinstance(raw_node, Mapping):
            raise ValueError(f"index.json node {node_key!r} must be an object")
        aliases = raw_node.get("aliases") or []
        aliases_text = (
            " ".join(str(item) for item in aliases)
            if isinstance(aliases, list)
            else ""
        )
        text = f"{aliases_text} {raw_node.get('raw_text', '')}"
        rows.append(
            (
                node_key,
                tokenize_for_fts(str(raw_node.get("title") or "")),
                tokenize_for_fts(str(raw_node.get("summary") or "")),
                tokenize_for_fts(str(text)),
            )
        )
    return sorted(rows)


def _runtime_generation_snapshot(connection: sqlite3.Connection) -> dict[str, int]:
    placeholders = ", ".join("?" for _ in _CANONICAL_PROJECTION_SURFACES)
    rows = connection.execute(
        "SELECT surface, generation FROM main.runtime_generations "
        f"WHERE surface IN ({placeholders}) ORDER BY surface",
        _CANONICAL_PROJECTION_SURFACES,
    ).fetchall()
    snapshot = {str(row[0]): int(row[1]) for row in rows}
    if set(snapshot) != set(_CANONICAL_PROJECTION_SURFACES):
        raise ValueError("runtime generation coverage is incomplete")
    if any(value < 0 for value in snapshot.values()):
        raise ValueError("runtime generation values must be non-negative")
    return snapshot


def _caller_transaction_active(connection: sqlite3.Connection) -> bool:
    state = getattr(connection, "in_transaction", None)
    if state is None:
        wrapped = getattr(connection, "connection", None)
        state = getattr(wrapped, "in_transaction", None)
    return state is True


def _projection_artifact_claims(
    artifacts: Mapping[str, bytes],
    *,
    expected_node_keyset_sha256: str,
    expected_fts_corpus_sha256: str,
) -> tuple[
    dict[str, str],
    dict[str, dict[str, Any]],
    list[str],
    dict[str, int] | None,
]:
    evidence = {
        name: {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(artifacts.items())
    }
    if set(artifacts) != set(_PROJECTION_ARTIFACT_NAMES):
        return {}, evidence, [], None

    sidecar = _bounded_json_object(
        artifacts["projection_pair_manifest.json"],
        context="projection_pair_manifest.json",
    )
    index_data = _bounded_json_object(artifacts["index.json"], context="index.json")
    claim_graph_data = _bounded_json_object(
        artifacts["claim_graph.json"],
        context="claim_graph.json",
    )
    issues: list[str] = []
    if (
        sidecar.get("contract") != _PROJECTION_SIDECAR_CONTRACT
        or sidecar.get("version") != 1
    ):
        issues.append("sidecar_contract_invalid")
    manifest = sidecar.get("projection_manifest")
    if not isinstance(manifest, Mapping):
        issues.append("projection_manifest_missing")
        manifest = {}
    generation = str(manifest.get("generation") or "").casefold()
    if (
        manifest.get("contract") != _PROJECTION_PAIR_CONTRACT
        or manifest.get("version") != 1
        or _HEX_GENERATION.fullmatch(generation) is None
        or not isinstance(manifest.get("published_at"), str)
        or not manifest.get("published_at")
    ):
        issues.append("projection_manifest_invalid")
    if index_data.get("projection_manifest") != manifest:
        issues.append("index_projection_manifest_mismatch")
    if claim_graph_data.get("projection_manifest") != manifest:
        issues.append("claim_graph_projection_manifest_mismatch")

    binding = manifest.get("canonical_generation")
    canonical_token = ""
    canonical_snapshot: dict[str, int] = {}
    if not isinstance(binding, Mapping):
        issues.append("canonical_generation_binding_missing")
    else:
        raw_snapshot = binding.get("runtime_generations")
        if (
            binding.get("status") != "verified"
            or binding.get("algorithm") != _CANONICAL_GENERATION_ALGORITHM
            or not isinstance(raw_snapshot, Mapping)
            or set(raw_snapshot) != set(_CANONICAL_PROJECTION_SURFACES)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in raw_snapshot.values()
            )
        ):
            issues.append("canonical_generation_binding_invalid")
        else:
            canonical_snapshot = {
                surface: int(raw_snapshot[surface])
                for surface in _CANONICAL_PROJECTION_SURFACES
            }
            canonical_token = _canonical_generation_token(canonical_snapshot)
            if binding.get("token") != canonical_token:
                issues.append("canonical_generation_token_mismatch")

    descriptors = sidecar.get("artifacts")
    if not isinstance(descriptors, Mapping) or set(descriptors) != {
        "index.json",
        "claim_graph.json",
    }:
        issues.append("sidecar_artifacts_invalid")
        descriptors = {}
    index_nodes = index_data.get("nodes")
    index_edges = index_data.get("weighted_edges")
    graph_nodes = claim_graph_data.get("nodes")
    graph_edges = claim_graph_data.get("edges")
    if (
        not isinstance(index_nodes, Mapping)
        or not isinstance(index_edges, list)
        or not isinstance(graph_nodes, list)
        or not isinstance(graph_edges, list)
    ):
        issues.append("projection_artifact_structure_invalid")
    expected_counts = {
        "index.json": {
            "node_count": len(index_nodes) if isinstance(index_nodes, Mapping) else None,
            "edge_count": len(index_edges) if isinstance(index_edges, list) else None,
        },
        "claim_graph.json": {
            "node_count": len(graph_nodes) if isinstance(graph_nodes, list) else None,
            "edge_count": len(graph_edges) if isinstance(graph_edges, list) else None,
        },
    }
    for name in ("index.json", "claim_graph.json"):
        descriptor = descriptors.get(name)
        if not isinstance(descriptor, Mapping):
            issues.append(f"{name}_descriptor_missing")
            continue
        declared_sha256 = descriptor.get("sha256")
        declared_bytes = descriptor.get("bytes")
        if (
            not isinstance(declared_sha256, str)
            or _HEX_SHA256.fullmatch(declared_sha256) is None
            or isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or declared_bytes < 0
        ):
            issues.append(f"{name}_descriptor_invalid")
            continue
        if declared_sha256 != evidence[name]["sha256"]:
            issues.append(f"{name}_sha256_mismatch")
        if declared_bytes != evidence[name]["bytes"]:
            issues.append(f"{name}_size_mismatch")
        for count_field, expected_count in expected_counts[name].items():
            declared_count = descriptor.get(count_field)
            if (
                isinstance(declared_count, bool)
                or not isinstance(declared_count, int)
                or declared_count < 0
            ):
                issues.append(f"{name}_{count_field}_invalid")
            elif expected_count is not None and declared_count != expected_count:
                issues.append(f"{name}_{count_field}_mismatch")

    try:
        projection_rows = _projection_rows_from_index(index_data)
    except (TypeError, ValueError):
        issues.append("index_projection_rows_invalid")
        projection_rows = []
    projection_keyset_sha256 = _keyset_sha256(row[0] for row in projection_rows)
    projection_corpus_digest = hashlib.sha256()
    for row in projection_rows:
        projection_corpus_digest.update(_stable_row_bytes(row))
    projection_corpus_sha256 = projection_corpus_digest.hexdigest()
    if projection_keyset_sha256 != expected_node_keyset_sha256:
        issues.append("artifact_expected_node_keyset_mismatch")
    if projection_corpus_sha256 != expected_fts_corpus_sha256:
        issues.append("artifact_expected_fts_corpus_mismatch")

    claims = {
        "status": "verified",
        "contract": _PROJECTION_AUTHORITY_CONTRACT,
        "generation": generation,
        "canonical_generation_token": canonical_token,
        "manifest_sha256": evidence["projection_pair_manifest.json"]["sha256"],
        "index_sha256": evidence["index.json"]["sha256"],
        "claim_graph_sha256": evidence["claim_graph.json"]["sha256"],
        "expected_node_keyset_sha256": projection_keyset_sha256,
        "expected_fts_corpus_sha256": projection_corpus_sha256,
    }
    return claims, evidence, issues, canonical_snapshot or None


def _projection_authority_baseline(
    connection: sqlite3.Connection,
    metadata: Mapping[str, str],
    artifacts: Mapping[str, bytes],
    *,
    expected_node_keyset_sha256: str,
    expected_fts_corpus_sha256: str,
) -> dict[str, Any]:
    missing = [field for field in _PROJECTION_AUTHORITY_FIELDS if not metadata.get(field)]
    missing_artifacts = sorted(set(_PROJECTION_ARTIFACT_NAMES) - set(artifacts))
    invalid: list[str] = []
    if metadata.get("status") not in (None, "verified"):
        invalid.append("status_not_verified")
    if metadata.get("contract") not in (None, _PROJECTION_AUTHORITY_CONTRACT):
        invalid.append("contract_unsupported")
    generation = str(metadata.get("generation") or "").casefold()
    if generation and _HEX_GENERATION.fullmatch(generation) is None:
        invalid.append("generation_invalid")
    for field in _PROJECTION_HASH_FIELDS:
        value = str(metadata.get(field) or "").casefold()
        if value and _HEX_SHA256.fullmatch(value) is None:
            invalid.append(f"{field}_invalid")

    claims, evidence, artifact_issues, canonical_snapshot = _projection_artifact_claims(
        artifacts,
        expected_node_keyset_sha256=expected_node_keyset_sha256,
        expected_fts_corpus_sha256=expected_fts_corpus_sha256,
    )
    if not missing_artifacts:
        for field in _PROJECTION_AUTHORITY_FIELDS:
            declared = str(metadata.get(field) or "").casefold()
            observed = str(claims.get(field) or "").casefold()
            if declared and observed and declared != observed:
                invalid.append(f"{field}_mismatch")
    invalid.extend(artifact_issues)
    if artifact_issues and not missing_artifacts:
        raise ValueError(
            "projection artifact validation failed: "
            + ", ".join(sorted(set(artifact_issues)))
        )
    verified_snapshot = None
    if not missing and not missing_artifacts and not invalid:
        if not _caller_transaction_active(connection):
            invalid.append("caller_transaction_required")
        elif not isinstance(canonical_snapshot, Mapping):
            invalid.append("canonical_generation_snapshot_missing")
        else:
            try:
                live_snapshot = _runtime_generation_snapshot(connection)
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                invalid.append("runtime_generation_unavailable")
            else:
                if live_snapshot != canonical_snapshot:
                    invalid.append("runtime_generation_mismatch")
                else:
                    verified_snapshot = dict(canonical_snapshot)
    report = {
        "required": list(_PROJECTION_AUTHORITY_FIELDS),
        "required_artifacts": list(_PROJECTION_ARTIFACT_NAMES),
        "provided": bool(metadata),
        "artifacts_provided": sorted(artifacts),
        "status": str(metadata.get("status") or "missing"),
        "contract": str(metadata.get("contract") or "") or None,
        "generation": str(metadata.get("generation") or "") or None,
        "canonical_generation_token": (
            str(metadata.get("canonical_generation_token") or "") or None
        ),
        "expected_node_keyset_sha256": expected_node_keyset_sha256,
        "expected_fts_corpus_sha256": expected_fts_corpus_sha256,
        "declared_expected_node_keyset_sha256": str(
            metadata.get("expected_node_keyset_sha256") or ""
        ).casefold()
        or None,
        "declared_expected_fts_corpus_sha256": str(
            metadata.get("expected_fts_corpus_sha256") or ""
        ).casefold()
        or None,
        "manifest_sha256": str(metadata.get("manifest_sha256") or "").casefold()
        or None,
        "index_sha256": str(metadata.get("index_sha256") or "").casefold()
        or None,
        "claim_graph_sha256": str(
            metadata.get("claim_graph_sha256") or ""
        ).casefold()
        or None,
        "artifact_evidence": evidence,
        "caller_transaction_active": _caller_transaction_active(connection),
        "runtime_generation_snapshot": verified_snapshot,
        "runtime_generation_rechecked": False,
        "missing_fields": missing,
        "missing_artifacts": missing_artifacts,
        "invalid_fields": sorted(set(invalid)),
        "complete": not missing and not missing_artifacts and not invalid,
    }
    report["stable_sha256"] = _canonical_json_sha256(report)
    return report


def _revalidate_projection_authority(
    connection: sqlite3.Connection,
    authority: dict[str, Any],
) -> None:
    if not authority.get("complete"):
        return
    invalid = list(authority.get("invalid_fields") or [])
    expected_snapshot = authority.get("runtime_generation_snapshot")
    if not _caller_transaction_active(connection):
        invalid.append("caller_transaction_lost")
    elif not isinstance(expected_snapshot, Mapping):
        invalid.append("runtime_generation_snapshot_missing")
    else:
        try:
            observed_snapshot = _runtime_generation_snapshot(connection)
        except (sqlite3.Error, TypeError, ValueError, OverflowError):
            invalid.append("runtime_generation_recheck_unavailable")
        else:
            if dict(expected_snapshot) != observed_snapshot:
                invalid.append("runtime_generation_changed_during_scan")
    still_active = _caller_transaction_active(connection)
    if not still_active:
        invalid.append("caller_transaction_lost")
    authority["runtime_generation_rechecked"] = True
    authority["caller_transaction_active"] = still_active
    authority["invalid_fields"] = sorted(set(invalid))
    authority["complete"] = not authority["invalid_fields"]
    authority.pop("stable_sha256", None)
    authority["stable_sha256"] = _canonical_json_sha256(authority)


def _stable_row_bytes(row: Sequence[str]) -> bytes:
    return (
        json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _row_sha256(row: Sequence[str]) -> str:
    return hashlib.sha256(_stable_row_bytes(row)).hexdigest()


def _keyset_sha256(keys: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for key in sorted(set(keys)):
        digest.update(_stable_row_bytes((key,)))
    return digest.hexdigest()


def _extract_tokenizer(ddl: str | None) -> str | None:
    match = _TOKENIZER_CLAUSE.search(str(ddl or ""))
    if match is None:
        return None
    return (match.group(1) or match.group(2) or "").replace("''", "'")


def _table_ddl(connection: sqlite3.Connection, table_name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return None if row is None else str(row[0] or "")


def _schema_baseline(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    target_ddl: str,
) -> dict[str, Any]:
    actual_ddl = _table_ddl(connection, table_name)
    actual_normalized = _normalize_ddl(actual_ddl)
    target_normalized = _normalize_ddl(target_ddl)
    return {
        "table": table_name,
        "actual_ddl": actual_ddl,
        "target_ddl": target_ddl.strip(),
        "actual_ddl_sha256": (
            _sha256_text(actual_normalized) if actual_normalized else None
        ),
        "target_ddl_sha256": _sha256_text(target_normalized),
        "schema_matches_target": bool(actual_normalized)
        and actual_normalized == target_normalized,
    }


def _coerce_fts_row(value: Any, *, default_key: str | None = None) -> tuple[str, str, str, str]:
    if isinstance(value, Mapping):
        key = value.get("node_key", default_key)
        if key is None:
            raise ValueError("expected FTS mapping row is missing node_key")
        return (
            _validated_identifier(key, context="expected FTS node_key"),
            str(value.get("title") or ""),
            str(value.get("summary") or ""),
            str(value.get("text") or ""),
        )
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError("expected FTS row must not be a scalar string")
    fields = tuple(value)
    if default_key is not None and len(fields) == 3:
        return (
            _validated_identifier(default_key, context="expected FTS node_key"),
            *(str(item or "") for item in fields),
        )
    if len(fields) != 4:
        raise ValueError("expected FTS row must contain node_key, title, summary, text")
    if default_key is not None and str(fields[0]) != str(default_key):
        raise ValueError("expected FTS mapping key does not match row node_key")
    return (
        _validated_identifier(fields[0], context="expected FTS node_key"),
        *(str(item or "") for item in fields[1:]),
    )


def _normalize_expected_fts_rows(expected_fts_rows: Any) -> list[tuple[str, str, str, str]]:
    if isinstance(expected_fts_rows, Mapping):
        rows = [
            _coerce_fts_row(value, default_key=str(key))
            for key, value in expected_fts_rows.items()
        ]
    else:
        candidate = expected_fts_rows
        if (
            isinstance(candidate, tuple)
            and len(candidate) == 4
            and isinstance(candidate[0], str)
            and all(not isinstance(item, (Mapping, list, tuple)) for item in candidate)
        ):
            candidate = [candidate]
        rows = [_coerce_fts_row(row) for row in candidate]
    return sorted(rows)


def _summarize_key_drift(
    expected_fingerprints: Mapping[str, list[str]],
    actual_fingerprints: Mapping[str, list[str]],
    *,
    sample_size: int,
) -> dict[str, Any]:
    expected_keys = set(expected_fingerprints)
    actual_keys = set(actual_fingerprints)
    duplicates = {
        key: len(values)
        for key, values in actual_fingerprints.items()
        if len(values) > 1
    }
    expected_duplicates = {
        key: len(values)
        for key, values in expected_fingerprints.items()
        if len(values) > 1
    }
    mismatched = sorted(
        key
        for key in expected_keys & actual_keys
        if sorted(expected_fingerprints[key]) != sorted(actual_fingerprints[key])
    )
    missing = sorted(expected_keys - actual_keys)
    orphan = sorted(actual_keys - expected_keys)
    return {
        "duplicate_key_count": len(duplicates),
        "duplicate_row_count": sum(count - 1 for count in duplicates.values()),
        "duplicate_keys": dict(list(sorted(duplicates.items()))[:sample_size]),
        "expected_duplicate_key_count": len(expected_duplicates),
        "missing_key_count": len(missing),
        "missing_keys": missing[:sample_size],
        "orphan_key_count": len(orphan),
        "orphan_keys": orphan[:sample_size],
        "content_mismatch_key_count": len(mismatched),
        "content_mismatch_keys": mismatched[:sample_size],
    }


def _normalize_query_probes(query_probes: Any) -> list[tuple[str, tuple[str, ...]]]:
    source = query_probes.items() if isinstance(query_probes, Mapping) else query_probes
    probes = []
    for item in source or ():
        if len(probes) >= _MAX_QUERY_PROBES:
            raise ValueError(f"query_probes exceeds {_MAX_QUERY_PROBES} items")
        if isinstance(item, str):
            query = item
            expected = ()
        else:
            query, expected = item
        query_text = str(query)
        if len(query_text) > _MAX_QUERY_LENGTH:
            raise ValueError(f"query probe exceeds {_MAX_QUERY_LENGTH} characters")
        if isinstance(expected, (str, bytes, bytearray)):
            raise ValueError("query probe expected IDs must be an iterable of IDs")
        expected_ids = []
        for value in expected or ():
            if len(expected_ids) >= _MAX_EXPECTED_IDS_PER_PROBE:
                raise ValueError(
                    "query probe expected IDs exceed "
                    f"{_MAX_EXPECTED_IDS_PER_PROBE} items"
                )
            expected_ids.append(
                _validated_identifier(value, context="query probe expected ID")
            )
        probes.append((query_text, tuple(expected_ids)))
    return probes


def _run_fts_query_probes(
    connection: sqlite3.Connection,
    query_probes: list[tuple[str, tuple[str, ...]]],
    *,
    top_k: int,
    sample_size: int,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    results: list[dict[str, Any]] = []
    complete = True
    normalized = query_probes
    for query, expected_ids in normalized[:sample_size]:
        expected_id_list = list(expected_ids)
        expected_id_hash = _keyset_sha256(expected_id_list)
        limit = min(100, max(top_k, len(expected_ids), 1))
        query_text = query if len(query) <= 512 else query[:509] + "..."
        probe: dict[str, Any] = {
            "query": query_text,
            "query_length": len(query),
            "query_sha256": _sha256_text(query),
            "query_truncated": len(query_text) != len(query),
            "expected_id_count": len(expected_id_list),
            "expected_ids_sha256": expected_id_hash,
            "expected_ids": expected_id_list[:sample_size],
            "limit": limit,
        }
        try:
            rows = connection.execute(
                "SELECT node_key, bm25(wiki_search_index) AS rank "
                "FROM wiki_search_index WHERE wiki_search_index MATCH ? "
                "ORDER BY rank, node_key LIMIT ?",
                (query, limit),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            ranks = [float(row[1]) for row in rows]
            missing_expected = sorted(set(expected_ids) - set(ids))
            unique = len(ids) == len(set(ids))
            finite = all(math.isfinite(rank) for rank in ranks)
            ordered = all(
                ranks[index] <= ranks[index + 1]
                for index in range(len(ranks) - 1)
            )
            passed = not missing_expected and unique and finite and ordered
            probe.update(
                {
                    "result_ids": [
                        _reported_identifier(item) for item in ids[:sample_size]
                    ],
                    "result_count": len(ids),
                    "result_ids_sha256": _keyset_sha256(ids),
                    "ranks": ranks[:sample_size],
                    "missing_expected_id_count": len(missing_expected),
                    "missing_expected_ids_sha256": _keyset_sha256(missing_expected),
                    "missing_expected_ids": [
                        _reported_identifier(item)
                        for item in missing_expected[:sample_size]
                    ],
                    "unique_ids": unique,
                    "finite_ranks": finite,
                    "ordered": ordered,
                    "passed": passed,
                    "error": None,
                }
            )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            passed = False
            probe.update({"passed": False, "error": str(exc)})
        complete = complete and passed
        results.append(probe)
    truncated = len(normalized) > len(results)
    complete = complete and not truncated
    return results, complete, {
        "input_count": len(normalized),
        "executed_count": len(results),
        "truncated": truncated,
        "sample_limit": sample_size,
        "stable_sha256": _canonical_json_sha256(normalized),
    }


def _inspect_fts(
    connection: sqlite3.Connection,
    *,
    expected_fts_rows: Any,
    query_probes: list[tuple[str, tuple[str, ...]]],
    tokenizer_metadata: Mapping[str, Any] | None,
    target_ddl: str,
    top_k: int,
    sample_size: int,
) -> dict[str, Any]:
    schema = _schema_baseline(
        connection,
        table_name="wiki_search_index",
        target_ddl=target_ddl,
    )
    expected_rows = _normalize_expected_fts_rows(expected_fts_rows)
    expected_digest = hashlib.sha256()
    expected_fingerprints: dict[str, list[str]] = defaultdict(list)
    for row in expected_rows:
        expected_digest.update(_stable_row_bytes(row))
        expected_fingerprints[row[0]].append(_row_sha256(row))

    actual_digest = hashlib.sha256()
    actual_fingerprints: dict[str, list[str]] = defaultdict(list)
    actual_count = 0
    scan_error = None
    if schema["actual_ddl"] is not None and scan_error is None:
        try:
            cursor = connection.execute(
                "SELECT node_key, title, summary, text FROM wiki_search_index "
                "ORDER BY node_key, title, summary, text"
            )
            while True:
                batch = cursor.fetchmany(256)
                if not batch:
                    break
                for source in batch:
                    row = (
                        _validated_identifier(
                            source[0],
                            context="actual FTS node_key",
                        ),
                        *(str(item or "") for item in source[1:]),
                    )
                    actual_digest.update(_stable_row_bytes(row))
                    actual_fingerprints[row[0]].append(_row_sha256(row))
                    actual_count += 1
        except (sqlite3.Error, TypeError, ValueError) as exc:
            scan_error = str(exc)

    drift = _summarize_key_drift(
        expected_fingerprints,
        actual_fingerprints,
        sample_size=sample_size,
    )
    actual_corpus_sha256 = actual_digest.hexdigest() if scan_error is None else None
    expected_corpus_sha256 = expected_digest.hexdigest()
    corpus_matches = (
        scan_error is None
        and actual_count == len(expected_rows)
        and actual_corpus_sha256 == expected_corpus_sha256
    )
    if schema["actual_ddl"] is not None:
        probes, probes_complete, probe_summary = _run_fts_query_probes(
            connection,
            query_probes,
            top_k=top_k,
            sample_size=sample_size,
        )
    else:
        normalized_probe_count = len(query_probes)
        probes = []
        probes_complete = normalized_probe_count == 0 and scan_error is None
        probe_summary = {
            "input_count": normalized_probe_count,
            "executed_count": 0,
            "truncated": normalized_probe_count > 0,
            "sample_limit": sample_size,
            "stable_sha256": _canonical_json_sha256(query_probes),
        }

    rebuild_required = (
        not schema["schema_matches_target"]
        or not corpus_matches
        or bool(drift["duplicate_key_count"])
        or bool(drift["missing_key_count"])
        or bool(drift["orphan_key_count"])
        or bool(drift["content_mismatch_key_count"])
        or not probes_complete
    )
    return {
        **schema,
        "actual_tokenizer": _extract_tokenizer(schema["actual_ddl"]),
        "target_tokenizer": _extract_tokenizer(target_ddl),
        "tokenizer_metadata": _tokenizer_metadata_baseline(tokenizer_metadata),
        "actual_row_count": actual_count,
        "expected_row_count": len(expected_rows),
        "actual_key_count": len(actual_fingerprints),
        "expected_key_count": len(expected_fingerprints),
        "actual_keyset_sha256": _keyset_sha256(actual_fingerprints),
        "expected_keyset_sha256": _keyset_sha256(expected_fingerprints),
        "actual_corpus_sha256": actual_corpus_sha256,
        "expected_corpus_sha256": expected_corpus_sha256,
        "corpus_matches_expected": corpus_matches,
        **drift,
        "query_probes": probes,
        "query_probe_summary": probe_summary,
        "query_probes_complete": probes_complete,
        "scan_error": scan_error,
        "rebuild_required": rebuild_required,
    }


def _normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _sample_candidate(
    samples: list[tuple[str, str, bytes, str]],
    *,
    entity_id: str,
    blob: bytes,
    blob_sha256: str,
    sample_size: int,
) -> None:
    if sample_size <= 0:
        return
    rank = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()
    candidate = (rank, entity_id, blob, blob_sha256)
    if len(samples) < sample_size:
        samples.append(candidate)
        return
    largest_index = max(range(len(samples)), key=lambda index: samples[index][:2])
    if candidate[:2] < samples[largest_index][:2]:
        samples[largest_index] = candidate


def _run_vec_top_k_probes(
    connection: sqlite3.Connection,
    *,
    samples: list[tuple[str, str, bytes, str]],
    known_ids: set[str],
    top_k: int,
) -> tuple[list[dict[str, Any]], bool]:
    probes: list[dict[str, Any]] = []
    complete = True
    limit = min(max(1, int(top_k)), max(1, len(known_ids)))
    statement = (
        "SELECT entity_id, distance FROM vec_embeddings "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
    )
    for _, entity_id, blob, blob_sha256 in sorted(samples):
        probe: dict[str, Any] = {
            "sample_id": entity_id,
            "sample_blob_sha256": blob_sha256,
            "limit": limit,
        }
        try:
            first_rows = connection.execute(statement, (blob, limit)).fetchall()
            second_rows = connection.execute(statement, (blob, limit)).fetchall()
            first = [(str(row[0]), float(row[1])) for row in first_rows]
            second = [(str(row[0]), float(row[1])) for row in second_rows]
            result_ids = [item[0] for item in first]
            distances = [item[1] for item in first]
            deterministic = first == second
            unique = len(result_ids) == len(set(result_ids))
            known = set(result_ids) <= known_ids
            finite = all(math.isfinite(distance) for distance in distances)
            ordered = all(
                distances[index] <= distances[index + 1]
                for index in range(len(distances) - 1)
            )
            expected_count = min(limit, len(known_ids))
            count_matches = len(first) == expected_count
            self_rank = (
                result_ids.index(entity_id) + 1 if entity_id in result_ids else None
            )
            self_first = self_rank == 1
            zero_distance_present = bool(distances) and abs(distances[0]) <= 1e-6
            passed = all(
                (
                    deterministic,
                    unique,
                    known,
                    finite,
                    ordered,
                    count_matches,
                    self_first,
                    zero_distance_present,
                )
            )
            probe.update(
                {
                    "result_ids": [
                        _reported_identifier(result_id) for result_id in result_ids
                    ],
                    "distances": distances,
                    "deterministic": deterministic,
                    "unique_ids": unique,
                    "known_ids": known,
                    "finite_distances": finite,
                    "ordered": ordered,
                    "expected_count": expected_count,
                    "count_matches": count_matches,
                    "self_rank": self_rank,
                    "self_first": self_first,
                    "zero_distance_present": zero_distance_present,
                    "passed": passed,
                    "error": None,
                }
            )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            passed = False
            probe.update({"passed": False, "error": str(exc)})
        complete = complete and passed
        probes.append(probe)
    return probes, complete


def _metadata_baseline(
    expected_ids: set[str],
    metadata: Mapping[str, Mapping[str, Any]] | None,
    *,
    expected_dimension: int,
    blob_hashes: Mapping[str, str],
    sample_size: int,
) -> dict[str, Any]:
    metadata = metadata or {}
    normalized_metadata = {
        str(entity_id): _json_safe(record)
        for entity_id, record in sorted(
            metadata.items(),
            key=lambda item: str(item[0]),
        )
    }
    missing = sorted(expected_ids - set(metadata))
    orphan = sorted(set(metadata) - expected_ids)
    invalid: dict[str, list[str]] = {}
    for entity_id in sorted(expected_ids & set(metadata)):
        record = metadata[entity_id]
        issues = []
        if not str(record.get("model") or "").strip():
            issues.append("model_missing")
        content_hash = str(record.get("content_hash") or "").casefold()
        if _HEX_SHA256.fullmatch(content_hash) is None:
            issues.append("content_hash_invalid")
        recipe = record.get("content_recipe")
        recipe_version = str(record.get("recipe_version") or "").strip()
        recipe_hash = str(record.get("recipe_hash") or "").casefold()
        if recipe in (None, "", (), [], {}) and not recipe_version and not recipe_hash:
            issues.append("content_recipe_missing")
        if recipe_hash and _HEX_SHA256.fullmatch(recipe_hash) is None:
            issues.append("recipe_hash_invalid")
        try:
            dimension = int(record.get("dimension"))
        except (TypeError, ValueError):
            dimension = -1
        if dimension != expected_dimension:
            issues.append("dimension_mismatch")
        declared_vector_hash = str(record.get("vector_hash") or "").casefold()
        if declared_vector_hash:
            if _HEX_SHA256.fullmatch(declared_vector_hash) is None:
                issues.append("vector_hash_invalid")
            elif entity_id in blob_hashes and declared_vector_hash != blob_hashes[entity_id]:
                issues.append("vector_hash_mismatch")
        if issues:
            invalid[entity_id] = issues
    complete = not missing and not orphan and not invalid
    return {
        "required": ["model", "content_hash", "content_recipe", "dimension"],
        "record_count": len(metadata),
        "stable_sha256": _canonical_json_sha256(normalized_metadata),
        "missing_count": len(missing),
        "missing_ids": missing[:sample_size],
        "orphan_count": len(orphan),
        "orphan_ids": orphan[:sample_size],
        "invalid_count": len(invalid),
        "invalid": dict(list(invalid.items())[:sample_size]),
        "complete": complete,
    }


def _inspect_vec0(
    connection: sqlite3.Connection,
    *,
    expected_nodes: set[str],
    metadata: Mapping[str, Mapping[str, Any]] | None,
    target_ddl: str,
    expected_dimension: int,
    top_k: int,
    sample_size: int,
) -> dict[str, Any]:
    schema = _schema_baseline(
        connection,
        table_name="vec_embeddings",
        target_ddl=target_ddl,
    )
    actual_dimension_match = _VECTOR_DIMENSION.search(str(schema["actual_ddl"] or ""))
    target_dimension_match = _VECTOR_DIMENSION.search(str(target_ddl or ""))
    actual_declared_dimension = (
        int(actual_dimension_match.group(1)) if actual_dimension_match else None
    )
    target_declared_dimension = (
        int(target_dimension_match.group(1)) if target_dimension_match else None
    )
    target_dimension_matches_expected = target_declared_dimension == expected_dimension
    try:
        row = connection.execute("SELECT vec_version()").fetchone()
        vec_version = str(row[0]) if row is not None else None
        extension_error = None
    except sqlite3.Error as exc:
        vec_version = None
        extension_error = str(exc)

    row_count = 0
    ids: list[str] = []
    id_counts: Counter[str] = Counter()
    normalized_ids: dict[str, set[str]] = defaultdict(set)
    dimension_counts: Counter[int] = Counter()
    malformed: Counter[str] = Counter()
    malformed_samples: dict[str, list[str]] = defaultdict(list)
    blob_hashes: dict[str, str] = {}
    aggregate = hashlib.sha256()
    samples: list[tuple[str, str, bytes, str]] = []
    scan_error = None

    def mark(issue: str, entity_id: str) -> None:
        malformed[issue] += 1
        if len(malformed_samples[issue]) < sample_size:
            malformed_samples[issue].append(_reported_identifier(entity_id))

    if schema["actual_ddl"] is not None and extension_error is None:
        try:
            cursor = connection.execute(
                "SELECT entity_id, embedding, vec_length(embedding) AS dimension "
                "FROM vec_embeddings ORDER BY entity_id"
            )
            while True:
                batch = cursor.fetchmany(64)
                if not batch:
                    break
                for source in batch:
                    entity_id = str(source[0] or "")
                    blob = source[1]
                    try:
                        dimension = int(source[2])
                    except (TypeError, ValueError):
                        dimension = -1
                    row_count += 1
                    ids.append(entity_id)
                    id_counts[entity_id] += 1
                    normalized_ids[_normalized_identity(entity_id)].add(entity_id)
                    dimension_counts[dimension] += 1
                    if (
                        len(entity_id.encode("utf-8"))
                        > _MAX_IDENTIFIER_UTF8_BYTES
                    ):
                        mark("id_too_long", entity_id)
                        continue
                    if not entity_id:
                        mark("blank_id", entity_id)
                    if not isinstance(blob, bytes):
                        mark("non_blob", entity_id)
                        continue
                    blob_sha256 = hashlib.sha256(blob).hexdigest()
                    blob_hashes[entity_id] = blob_sha256
                    aggregate.update(_stable_row_bytes((entity_id, blob_sha256)))
                    if dimension != expected_dimension:
                        mark("dimension_mismatch", entity_id)
                    if len(blob) != expected_dimension * 4:
                        mark("blob_size_mismatch", entity_id)
                        continue
                    try:
                        values = memoryview(blob).cast("f")
                    except (TypeError, ValueError):
                        mark("blob_decode_error", entity_id)
                        continue
                    finite = all(math.isfinite(float(value)) for value in values)
                    if not finite:
                        mark("non_finite", entity_id)
                        continue
                    norm = math.sqrt(math.fsum(float(value) ** 2 for value in values))
                    if norm <= 1e-12:
                        mark("zero_norm", entity_id)
                    elif abs(norm - 1.0) > 1e-3:
                        mark("non_unit_norm", entity_id)
                    _sample_candidate(
                        samples,
                        entity_id=entity_id,
                        blob=blob,
                        blob_sha256=blob_sha256,
                        sample_size=sample_size,
                    )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            scan_error = str(exc)

    duplicate_ids = sorted(key for key, count in id_counts.items() if count > 1)
    for entity_id in duplicate_ids:
        mark("duplicate_id", entity_id)
    normalized_collisions = sorted(
        sorted(values)
        for values in normalized_ids.values()
        if len(values) > 1
    )
    for values in normalized_collisions:
        mark("unicode_case_collision", " | ".join(values))

    actual_ids = set(ids)
    missing_ids = sorted(expected_nodes - actual_ids)
    orphan_ids = sorted(actual_ids - expected_nodes)
    hash_complete = (
        scan_error is None
        and row_count == len(blob_hashes)
        and all(blob_hashes.get(entity_id) for entity_id in actual_ids)
    )
    probes, probes_complete = _run_vec_top_k_probes(
        connection,
        samples=samples,
        known_ids=actual_ids,
        top_k=top_k,
    ) if schema["schema_matches_target"] and not malformed and actual_ids else ([], False)
    metadata_status = _metadata_baseline(
        expected_nodes,
        metadata,
        expected_dimension=expected_dimension,
        blob_hashes=blob_hashes,
        sample_size=sample_size,
    )
    repack_ready = all(
        (
            schema["schema_matches_target"],
            extension_error is None,
            scan_error is None,
            not malformed,
            not missing_ids,
            not orphan_ids,
            hash_complete,
            probes_complete if actual_ids else not expected_nodes,
        )
    )
    regenerate_ready = all(
        (
            extension_error is None,
            metadata_status["complete"],
            target_dimension_matches_expected,
        )
    )
    return {
        **schema,
        "actual_declared_dimension": actual_declared_dimension,
        "target_declared_dimension": target_declared_dimension,
        "target_dimension_matches_expected": target_dimension_matches_expected,
        "vec_version": vec_version,
        "extension_error": extension_error,
        "expected_dimension": expected_dimension,
        "row_count": row_count,
        "distinct_id_count": len(actual_ids),
        "expected_id_count": len(expected_nodes),
        "dimension_counts": {
            str(key): value for key, value in sorted(dimension_counts.items())
        },
        "id_set_sha256": _keyset_sha256(actual_ids),
        "id_blob_sha256": aggregate.hexdigest() if hash_complete else None,
        "hash_complete": hash_complete,
        "missing_id_count": len(missing_ids),
        "missing_ids": [
            _reported_identifier(item) for item in missing_ids[:sample_size]
        ],
        "orphan_id_count": len(orphan_ids),
        "orphan_ids": [
            _reported_identifier(item) for item in orphan_ids[:sample_size]
        ],
        "malformed_count": sum(malformed.values()),
        "malformed_counts": dict(sorted(malformed.items())),
        "malformed_samples": dict(sorted(malformed_samples.items())),
        "top_k_probes": probes,
        "top_k_probes_complete": probes_complete,
        "metadata": metadata_status,
        "scan_error": scan_error,
        "repack_ready": repack_ready,
        "regenerate_ready": regenerate_ready,
    }


def inspect_storage_baseline(
    connection: sqlite3.Connection,
    *,
    expected_nodes: Iterable[str],
    expected_fts_rows: Any,
    query_probes: Any = (),
    target_wiki_fts_ddl: str = WIKI_FTS_TARGET_DDL,
    target_vec_ddl: str = VEC0_TARGET_DDL,
    expected_dimension: int = 3072,
    top_k: int = 5,
    sample_size: int = 8,
    tokenizer_metadata: Mapping[str, Any] | None = None,
    vector_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    projection_metadata: Mapping[str, Any] | None = None,
    projection_artifacts: (
        Mapping[str, bytes | bytearray | memoryview] | None
    ) = None,
) -> dict[str, Any]:
    """Inspect FTS5 and vec0 without taking ownership of ``connection``.

    ``expected_fts_rows`` accepts either ``{node_key: (title, summary, text)}``
    mappings or iterable four-tuples.  Query probes are literal, parameterized
    FTS5 MATCH expressions.  Tokenizer metadata binds the engine, package
    version, and segmentation recipe needed to reproduce the indexed corpus.
    Vector metadata is supplied by the caller because the current vec0 table
    has no model/content-recipe columns. Projection readiness requires both
    bounded declarations, an active caller-owned transaction, and the raw
    sidecar/index/claim-graph bytes. Their digests, embedded pair manifest,
    expected corpus, and live canonical runtime generation are verified before
    the larger FTS/vec0 scans, then generation is rechecked before readiness.
    """
    if connection is None:
        raise TypeError("connection is required")
    expected_dimension = int(expected_dimension)
    if expected_dimension < 1:
        raise ValueError("expected_dimension must be positive")
    top_k = max(1, min(100, int(top_k)))
    sample_size = max(0, min(32, int(sample_size)))
    normalized_query_probes = _normalize_query_probes(query_probes)
    normalized_expected_fts_rows = _normalize_expected_fts_rows(expected_fts_rows)
    normalized_tokenizer_metadata = _normalize_tokenizer_metadata(
        tokenizer_metadata
    )
    normalized_projection_metadata = _normalize_projection_metadata(
        projection_metadata
    )
    normalized_projection_artifacts = _normalize_projection_artifacts(
        projection_artifacts
    )
    expected_node_set = {
        _validated_identifier(item, context="expected vector node ID")
        for item in expected_nodes
    }
    for entity_id in (vector_metadata or {}):
        _validated_identifier(entity_id, context="vector metadata ID")
    _validate_vector_metadata(vector_metadata)
    if not _caller_transaction_active(connection):
        raise ValueError("an active caller-owned SQLite transaction is required")

    expected_fts_digest = hashlib.sha256()
    for row in normalized_expected_fts_rows:
        expected_fts_digest.update(_stable_row_bytes(row))
    projection_authority = _projection_authority_baseline(
        connection,
        normalized_projection_metadata,
        normalized_projection_artifacts,
        expected_node_keyset_sha256=_keyset_sha256(expected_node_set),
        expected_fts_corpus_sha256=expected_fts_digest.hexdigest(),
    )

    fts = _inspect_fts(
        connection,
        expected_fts_rows=normalized_expected_fts_rows,
        query_probes=normalized_query_probes,
        tokenizer_metadata=normalized_tokenizer_metadata,
        target_ddl=target_wiki_fts_ddl,
        top_k=top_k,
        sample_size=sample_size,
    )
    vec0 = _inspect_vec0(
        connection,
        expected_nodes=expected_node_set,
        metadata=vector_metadata,
        target_ddl=target_vec_ddl,
        expected_dimension=expected_dimension,
        top_k=top_k,
        sample_size=sample_size,
    )
    _revalidate_projection_authority(connection, projection_authority)
    vec0["surface_repack_ready"] = bool(vec0["repack_ready"])
    vec0["surface_regenerate_ready"] = bool(vec0["regenerate_ready"])
    vec0["repack_ready"] = bool(
        vec0["surface_repack_ready"] and projection_authority["complete"]
    )
    vec0["regenerate_ready"] = bool(
        vec0["surface_regenerate_ready"] and projection_authority["complete"]
    )
    fts["rebuild_ready"] = bool(
        projection_authority["complete"]
        and fts["tokenizer_metadata"]["complete"]
        and fts["target_tokenizer"]
        and fts["scan_error"] is None
    )
    errors = [
        f"fts:{fts['scan_error']}" if fts.get("scan_error") else "",
        f"vec0:{vec0['scan_error']}" if vec0.get("scan_error") else "",
        (
            f"vec0_extension:{vec0['extension_error']}"
            if vec0.get("extension_error")
            else ""
        ),
    ]
    rebuild_required = {
        "fts_index": bool(fts["rebuild_required"]),
        "fts_tokenizer_metadata_incomplete": not bool(
            fts["tokenizer_metadata"]["complete"]
        ),
        "projection_authority_incomplete": not bool(
            projection_authority["complete"]
        ),
        "vec_exact_repack_blocked": not bool(vec0["repack_ready"]),
        "vec_regeneration_blocked": not bool(vec0["regenerate_ready"]),
    }
    rebuild_required["any"] = any(rebuild_required.values())
    report = {
        "contract": STORAGE_BASELINE_CONTRACT,
        "read_only": True,
        "connection_owned_by_caller": True,
        "projection_authority": projection_authority,
        "fts": fts,
        "vec0": vec0,
        "rebuild_required": rebuild_required,
        "repack_ready": bool(vec0["repack_ready"]),
        "regenerate_ready": bool(vec0["regenerate_ready"]),
        "errors": [error for error in errors if error],
    }
    report["baseline_fingerprint"] = _canonical_json_sha256(report)
    return report


__all__ = [
    "STORAGE_BASELINE_CONTRACT",
    "VEC0_TARGET_DDL",
    "WIKI_FTS_TARGET_DDL",
    "inspect_storage_baseline",
]
