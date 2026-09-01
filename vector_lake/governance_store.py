import copy
import heapq
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import time
import unicodedata
import uuid
import zlib
from datetime import datetime, timezone

from filelock import FileLock

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - minimal installations use stdlib JSON
    _orjson = None

from vector_lake.claim_extractor import classify_non_claim_text, extract_page_objects
from vector_lake.db_store import (
    OperationalMemorySearchIntegrityLimitExceeded,
    certify_operational_memory_search_integrity,
    get_connection,
    init_db,
    invalidate_operational_memory_search_proof,
    mark_operational_memory_search_rebuild_required,
    mcp_readonly_surface_enabled,
    operational_memory_search_source_sha256,
    peek_db_path,
    require_current_schema_for_read,
    transaction,
    verify_operational_memory_search_integrity,
)
from vector_lake.evidence_foundation import version_family_id
from vector_lake.wiki_utils import (
    get_meta_dir,
    get_wiki_dir,
    iter_markdown_files,
    normalize_semantic_text,
    read_markdown_file,
    split_frontmatter,
)


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("vector-lake-governance-store")

SCHEMA_VERSION = "8.0"
OPERATIONAL_MEMORY_TYPES = {"fact", "preference", "decision", "task_state"}
MEMORY_TTL_DAYS = {
    "fact": 365,
    "preference": 365,
    "decision": 730,
    "task_state": 45,
}
VALIDITY_FACTORS = {
    "active": 1.0,
    "expiring-soon": 0.82,
    "review-due": 0.72,
    "needs-review": 0.62,
    "provisional": 0.58,
    "unsupported": 0.42,
    "conflicted": 0.18,
    "superseded": 0.08,
    "expired": 0.0,
    "archived": 0.0,
}


class OperationalMemoryNotReady(RuntimeError):
    """A read-only memory query cannot safely use the current projection."""

    def __init__(self, reason: str, *, retry_after_seconds: int = 5):
        self.reason = str(reason)
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(
            "Operational-memory projection unavailable: "
            f"{self.reason}; retry_after_seconds={self.retry_after_seconds}"
        )


class CanonicalStoreNotReady(RuntimeError):
    """A read-only caller cannot bootstrap an empty canonical store."""


class CanonicalIdOwnershipError(ValueError):
    """Reject reuse or relocation of a globally unique canonical identifier."""


class ChangeSetIdempotencyConflict(RuntimeError):
    """Reject an ambiguous legacy idempotency key without choosing an owner."""


class ChangeSetPayloadTooLarge(ValueError):
    """Reject one change set whose canonical delta exceeds its hard ceiling."""


class ChangeSetBatchTooLarge(ValueError):
    """Reject an atomic change-set batch that exceeds count or byte ceilings."""


class ChangeSetPayloadCorrupt(RuntimeError):
    """Reject a missing, malformed, or digest-mismatched content-addressed delta."""


_CHANGE_SET_PAYLOAD_SECTIONS = (
    "proposed_entities",
    "proposed_claims",
    "proposed_evidence",
    "proposed_source_updates",
    "proposed_source_artifacts",
    "proposed_extraction_runs",
    "proposed_edges",
)
_CHANGE_SET_TERMINAL_STATUSES = frozenset(
    {"applied", "cancelled", "failed", "published", "rejected", "superseded"}
)
_CHANGE_SET_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_CHANGE_SET_MAX_STORED_BYTES = 4 * 1024 * 1024 + 64 * 1024
_CHANGE_SET_BATCH_MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
_CHANGE_SET_MAX_BATCH_ITEMS = 200
_CHANGE_SET_MAX_PAGES = 200
_CHANGE_SET_MAX_AFFECTED_IDS = 20_000
_CHANGE_SET_BATCH_MAX_AFFECTED_IDS = 50_000
_CHANGE_SET_MAX_MANIFEST_BYTES = 64 * 1024
_CHANGE_SET_ID_PREVIEW_LIMIT = 32
_CHANGE_SET_PAGE_PREVIEW_LIMIT = _CHANGE_SET_MAX_PAGES
_CHANGE_SET_MANIFEST_VERSION = 2
_CHANGE_SET_DELTA_KIND = "page_replace_v1"
_CHANGE_SET_PAYLOAD_CODEC = "zlib-json-v1"
_CHANGE_SET_DETACHED_LEGACY_CODEC = "detached-legacy-json-sha256-v1"
_CHANGE_SET_LOAD_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_CHANGE_SET_COMPACTION_MAX_SCAN_ROWS = 5000
_CHANGE_SET_COMPACTION_MAX_INPUT_BYTES = 128 * 1024 * 1024
_CHANGE_SET_COMPACTION_MAX_CURSOR_BYTES = 1024


_PURPOSE_VECTORS_CACHE = None
_PURPOSE_VECTORS_MTIME = 0


def get_purpose_vectors() -> dict:
    global _PURPOSE_VECTORS_CACHE, _PURPOSE_VECTORS_MTIME
    path = get_meta_dir() / "purpose_vectors.json"

    current_mtime = 0
    if path.exists():
        current_mtime = path.stat().st_mtime

    if _PURPOSE_VECTORS_CACHE is not None and _PURPOSE_VECTORS_MTIME == current_mtime:
        return _PURPOSE_VECTORS_CACHE

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                _PURPOSE_VECTORS_CACHE = json.load(f)
        except Exception:
            _PURPOSE_VECTORS_CACHE = {"keywords": [], "weight_boost": 0.0}
    else:
        _PURPOSE_VECTORS_CACHE = {"keywords": [], "weight_boost": 0.0}

    _PURPOSE_VECTORS_MTIME = current_mtime
    return _PURPOSE_VECTORS_CACHE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(path) -> FileLock:
    return FileLock(str(path) + ".lock", timeout=10)


def _default_map_store(key_name: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "items": {},
        "key_name": key_name,
    }


def _default_queue_store() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "items": [],
    }


ALLOWED_TABLES = {
    "entities",
    "claims",
    "evidence",
    "sources",
    "change_sets",
    "governance_queue",
    "wiki_search_index",
    "alias_registry",
    "operational_memory",
    "claim_graph_nodes",
    "claim_graph_edges",
    "page_graph_edges",
    "timeline_events",
    "processed_files",
    "mutation_outbox",
}


def _validate_table_name(table_name: str):
    """🛡️ Sentinel: Prevent SQL injection by validating table names against a strict whitelist."""
    if table_name not in ALLOWED_TABLES:
        raise ValueError(
            f"Security error: Invalid table name '{table_name}'. Expected one of {ALLOWED_TABLES}."
        )


def initialize_meta_store():
    if mcp_readonly_surface_enabled():
        require_current_schema_for_read()
        return
    init_db()


def _count_wiki_pages() -> int:
    excluded = {"index.md", "log.md", "overview.md"}
    return sum(
        1
        for path in iter_markdown_files(get_wiki_dir())
        if path.name.casefold() not in excluded
    )


def _load_db_map(table_name: str, pk_col: str):
    _validate_table_name(table_name)
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store(pk_col)
    rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
    for row in rows:
        store["items"][row[pk_col]] = json.loads(row["data_json"])
    return store


def _load_db_queue(table_name: str, pk_col: str):
    _validate_table_name(table_name)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pk_col) is None:
        raise ValueError(f"Security error: Invalid primary key column '{pk_col}'")
    initialize_meta_store()
    conn = get_connection()
    store = _default_queue_store()
    rows = conn.execute(
        f"SELECT data_json FROM {table_name} ORDER BY updated_at ASC, {pk_col} ASC"
    ).fetchall()
    for row in rows:
        store["items"].append(json.loads(row["data_json"]))
    return store


def _save_db_map(table_name: str, pk_col: str, data: dict, extra_cols: list = None):
    if table_name in {"claims", "evidence"}:
        raise CanonicalIdOwnershipError(
            f"Full-map writes to {table_name} are disabled; "
            "use an atomic canonical change set."
        )
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    if extra_cols is None:
        extra_cols = []

    with transaction():
        existing_rows = conn.execute(
            f"SELECT {pk_col}, data_json FROM {table_name}"
        ).fetchall()
        existing_map = {row[0]: row["data_json"] for row in existing_rows}
        new_keys = set(data.get("items", {}).keys())
        keys_to_delete = set(existing_map.keys()) - new_keys

        if keys_to_delete:
            conn.executemany(
                f"DELETE FROM {table_name} WHERE {pk_col} = ?",
                [(k,) for k in keys_to_delete],
            )

        if data.get("items"):
            cols = [pk_col] + [c[0] for c in extra_cols] + ["data_json", "updated_at"]
            placeholders = ["?"] * len(cols)

            all_vals = []
            for key, item in data.get("items", {}).items():
                new_json = json.dumps(item, ensure_ascii=False)
                # Skip SQLite I/O if the row hasn't changed at all
                if key in existing_map and existing_map[key] == new_json:
                    continue

                params = [key]
                for c_name, c_key, c_type in extra_cols:
                    val = item.get(c_key)
                    if c_type is float:
                        params.append(float(val or 0.0))
                    elif c_type is int:
                        params.append(int(val or 0))
                    else:
                        params.append(str(val or ""))
                params.append(new_json)
                params.append(now)
                all_vals.append(tuple(params))

            if all_vals:
                conn.executemany(
                    f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                    all_vals,
                )


def _save_db_queue(table_name: str, pk_col: str, data: dict):
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    with transaction():
        # V10.1 Diff-based synchronization (Avoid full table wipe)
        for item in data.get("items", []):
            if not item.get(pk_col):
                item[pk_col] = uuid.uuid4().hex

        existing_keys_query = conn.execute(
            f"SELECT {pk_col} FROM {table_name}"
        ).fetchall()
        existing_keys = {row[0] for row in existing_keys_query}
        new_keys = {item[pk_col] for item in data.get("items", [])}
        keys_to_delete = existing_keys - new_keys

        if keys_to_delete:
            conn.executemany(
                f"DELETE FROM {table_name} WHERE {pk_col} = ?",
                [(k,) for k in keys_to_delete],
            )

        if data.get("items"):
            all_vals = []
            for item in data.get("items", []):
                k = item.get(pk_col)
                all_vals.append((k, json.dumps(item, ensure_ascii=False), now))
            conn.executemany(
                f"INSERT OR REPLACE INTO {table_name} ({pk_col}, data_json, updated_at) VALUES (?, ?, ?)",
                all_vals,
            )


def load_entities():
    return _load_db_map("entities", "entity_id")


def query_entities(
    filters: dict = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict:
    """Query entities, reusing a caller-owned read-only snapshot when supplied."""
    if connection is None:
        initialize_meta_store()
        connection = get_connection()
    store = _default_map_store("entity_id")
    query = "SELECT data_json FROM entities"
    params = []
    if filters:
        clauses = []
        for k, v in filters.items():
            if not re.match(r"^[a-zA-Z0-9_]+(!=)?$", k):
                raise ValueError(f"Security error: Invalid filter key '{k}'.")
            if k.endswith("!="):
                clauses.append(f"{k[:-2]} != ?")
            else:
                clauses.append(f"{k} = ?")
            params.append(v)
        query += " WHERE " + " AND ".join(clauses)
    rows = connection.execute(query, tuple(params)).fetchall()
    for row in rows:
        data = json.loads(row["data_json"])
        store["items"][data["entity_id"]] = data
    return store


def load_claims():
    return _load_db_map("claims", "claim_id")


def load_evidence():
    return _load_db_map("evidence", "evidence_id")


def load_sources():
    return _load_db_map("sources", "source_id")


def canonical_store_counts() -> dict[str, int]:
    """Return canonical row counts without materializing domain objects."""
    initialize_meta_store()
    row = (
        get_connection()
        .execute(
            "SELECT "
            "(SELECT COUNT(*) FROM entities) AS entities, "
            "(SELECT COUNT(*) FROM claims) AS claims, "
            "(SELECT COUNT(*) FROM sources) AS sources"
        )
        .fetchone()
    )
    return {
        "entities": int(row["entities"]),
        "claims": int(row["claims"]),
        "sources": int(row["sources"]),
    }


def _needs_unicode_trace_fallback(tokens: list[str]) -> bool:
    return any(
        ord(character) > 127
        and character.isalpha()
        and character.lower() != character.upper()
        for token in tokens
        for character in str(token)
    )


def _select_trace_claims_streaming(
    tokens: list[str],
    relevant_pages: set[str],
    top_k: int,
) -> list[dict]:
    """Rank indexed page claims, then use one bounded compatibility scan."""
    normalized_tokens = [str(token).lower() for token in tokens]
    normalized_pages = {str(page) for page in relevant_pages}
    retained: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    conn = get_connection()
    candidate_rows = []
    if normalized_pages:
        page_limit = max(64, min(2048, top_k * 64))
        per_page_limit = max(16, page_limit // len(normalized_pages))
        for page_key in sorted(normalized_pages):
            candidate_rows.extend(
                conn.execute(
                    "SELECT claim_id AS source_key, claim_text, "
                    "COALESCE(json_extract(data_json, '$.source_page'), '') "
                    "AS source_page, COALESCE(json_extract(data_json, "
                    "'$.locator.page_key'), '') AS locator_page, data_json "
                    "FROM claims WHERE "
                    "json_extract(data_json, '$.locator.page_key') = ? "
                    "ORDER BY claim_id ASC LIMIT ?",
                    (page_key, per_page_limit),
                )
            )
    if len(candidate_rows) < top_k:
        fallback_limit = max(256, min(4096, top_k * 256))
        candidate_rows.extend(
            conn.execute(
                "SELECT claim_id AS source_key, claim_text, "
                "COALESCE(json_extract(data_json, '$.source_page'), '') "
                "AS source_page, COALESCE(json_extract(data_json, "
                "'$.locator.page_key'), '') AS locator_page, data_json "
                "FROM claims "
                "ORDER BY claim_id ASC LIMIT ?",
                (fallback_limit,),
            )
        )
    for row in candidate_rows:
        source_key = str(row["source_key"])
        if source_key in seen:
            continue
        seen.add(source_key)
        source_page = str(row["source_page"] or "")
        page_key = str(row["locator_page"] or "")
        haystack = f"{row['claim_text'] or ''} {source_page}".lower()
        score = (5 if page_key in normalized_pages else 0) + sum(
            1 for token in normalized_tokens if token in haystack
        )
        if score <= 0:
            continue
        candidate = (score, source_key, str(row["data_json"]))
        if len(retained) < top_k:
            retained.append(candidate)
            continue
        worst_index = max(
            range(len(retained)),
            key=lambda index: (-retained[index][0], retained[index][1]),
        )
        worst = retained[worst_index]
        if score > worst[0] or (score == worst[0] and source_key < worst[1]):
            retained[worst_index] = candidate
    retained.sort(key=lambda item: (-item[0], item[1]))
    return [json.loads(data_json) for _, _, data_json in retained]


def select_trace_claims(
    tokens: list[str],
    relevant_pages: set[str],
    top_k: int,
) -> list[dict]:
    """Select only the highest-scoring trace claims with bounded result memory."""
    if top_k < 0:
        raise ValueError("top_k must be non-negative")
    if top_k == 0 or (not tokens and not relevant_pages):
        return []
    # Trace is read-only and must not pay the first-process migration path.
    # Validate the existing schema ledger, then query the established store.
    conn = require_current_schema_for_read("claims")
    if relevant_pages or _needs_unicode_trace_fallback(tokens):
        return _select_trace_claims_streaming(tokens, relevant_pages, top_k)
    rows = conn.execute(
        "WITH query_terms(term) AS ("
        "SELECT CAST(value AS TEXT) FROM json_each(?)"
        "), relevant_pages(page_key) AS ("
        "SELECT CAST(value AS TEXT) FROM json_each(?)"
        "), scored AS ("
        "SELECT claims.claim_id AS source_key, claims.data_json AS data_json, "
        "(CASE WHEN EXISTS ("
        "SELECT 1 FROM relevant_pages WHERE page_key = "
        "COALESCE(json_extract(claims.data_json, '$.source_page'), '')"
        ") THEN 5 ELSE 0 END) + ("
        "SELECT COUNT(*) FROM query_terms WHERE instr("
        "lower(COALESCE(claims.claim_text, '') || ' ' || "
        "COALESCE(json_extract(claims.data_json, '$.source_page'), '')), "
        "term) > 0"
        ") AS trace_score FROM claims"
        ") SELECT data_json FROM scored WHERE trace_score > 0 "
        "ORDER BY trace_score DESC, source_key ASC LIMIT ?",
        (
            json.dumps([str(token).lower() for token in tokens], ensure_ascii=False),
            json.dumps(
                sorted(str(page) for page in relevant_pages), ensure_ascii=False
            ),
            int(top_k),
        ),
    ).fetchall()
    return [json.loads(row["data_json"]) for row in rows]


def load_trace_labels(
    entity_ids: set[str],
    source_ids: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Load labels only for entities and sources referenced by selected claims."""
    conn = require_current_schema_for_read("entities", "sources")
    entity_names: dict[str, str] = {}
    source_pages: dict[str, str] = {}

    ordered_entities = sorted(str(value) for value in entity_ids if value)
    for offset in range(0, len(ordered_entities), 500):
        batch = ordered_entities[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT entity_id, canonical_name, data_json FROM entities "
            f"WHERE entity_id IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        for row in rows:
            record = json.loads(row["data_json"])
            entity_names[str(row["entity_id"])] = str(
                record.get("canonical_name") or row["canonical_name"] or ""
            )

    ordered_sources = sorted(str(value) for value in source_ids if value)
    for offset in range(0, len(ordered_sources), 500):
        batch = ordered_sources[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT source_id, data_json FROM sources "
            f"WHERE source_id IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        for row in rows:
            record = json.loads(row["data_json"])
            source_pages[str(row["source_id"])] = str(
                record.get("canonical_source_page") or ""
            )
    return entity_names, source_pages


def load_alias_registry():
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store("alias")
    rows = conn.execute("SELECT key, value FROM alias_registry").fetchall()
    for row in rows:
        store["items"][row["key"]] = row["value"]
    return store


def get_alias(key: str) -> str | None:
    """Return one alias target without loading the complete registry."""
    initialize_meta_store()
    row = (
        get_connection()
        .execute(
            "SELECT value FROM alias_registry WHERE key = ?",
            (key,),
        )
        .fetchone()
    )
    return row["value"] if row else None


def upsert_alias(key: str, value: str) -> None:
    """Persist one alias mapping and participate in any surrounding transaction."""
    now = _utc_now()
    with transaction():
        get_connection().execute(
            "INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )


def load_memory_objects():
    return _load_db_map("operational_memory", "memory_id")


def query_memory_objects(filters: dict = None) -> dict:
    initialize_meta_store()
    conn = get_connection()
    store = _default_map_store("memory_id")
    query = "SELECT data_json FROM operational_memory"
    params = []
    if filters:
        clauses = []
        for k, v in filters.items():
            if not re.match(r"^[a-zA-Z0-9_]+(!=)?$", k):
                raise ValueError(f"Security error: Invalid filter key '{k}'.")
            if k.endswith("!="):
                clauses.append(f"{k[:-2]} != ?")
            else:
                clauses.append(f"{k} = ?")
            params.append(v)
        query += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(query, tuple(params)).fetchall()
    for row in rows:
        data = json.loads(row["data_json"])
        store["items"][data["memory_id"]] = data
    return store


def load_change_sets(limit: int = 1000):
    """Load bounded manifests only; payload blobs are never hydrated here."""
    initialize_meta_store()
    normalized_limit = int(limit)
    if normalized_limit < 1 or normalized_limit > 5000:
        raise ValueError("change-set manifest limit must be between 1 and 5000")
    conn = get_connection()
    rows = conn.execute(
        "SELECT change_sets.change_set_id, lifecycle.status, lifecycle.created_at, "
        "lifecycle.terminal_at, length(CAST(change_sets.data_json AS BLOB)) "
        "AS data_bytes, CASE WHEN json_valid(change_sets.data_json) "
        "THEN json_extract(change_sets.data_json, '$.manifest_version') END "
        "AS manifest_version FROM change_sets "
        "JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "ORDER BY COALESCE(lifecycle.terminal_at, lifecycle.created_at) DESC, "
        "change_sets.change_set_id DESC LIMIT ?",
        (normalized_limit,),
    ).fetchall()
    items = []
    loaded_bytes = 0
    truncated_by_bytes = False
    for row in rows:
        data_bytes = int(row["data_bytes"] or 0)
        if int(row["manifest_version"] or 0) != _CHANGE_SET_MANIFEST_VERSION:
            items.append(
                {
                    "change_set_id": str(row["change_set_id"]),
                    "manifest_version": 0,
                    "status": str(row["status"]),
                    "created_at": row["created_at"],
                    "terminal_at": row["terminal_at"],
                    "legacy_inline": True,
                    "compaction_required": True,
                    "payload": {
                        "available": False,
                        "codec": "legacy-inline-json-v1",
                        "raw_bytes": data_bytes,
                    },
                }
            )
            continue
        if data_bytes < 0 or data_bytes > _CHANGE_SET_MAX_MANIFEST_BYTES:
            raise ChangeSetPayloadCorrupt(
                "Stored change-set manifest exceeds hard limit: "
                f"{row['change_set_id']} ({data_bytes} bytes)"
            )
        if loaded_bytes + data_bytes > _CHANGE_SET_LOAD_MAX_TOTAL_BYTES:
            truncated_by_bytes = True
            break
        manifest_row = conn.execute(
            "SELECT data_json FROM change_sets WHERE change_set_id = ? "
            "AND length(CAST(data_json AS BLOB)) = ? "
            "AND length(CAST(data_json AS BLOB)) <= ?",
            (
                row["change_set_id"],
                data_bytes,
                _CHANGE_SET_MAX_MANIFEST_BYTES,
            ),
        ).fetchone()
        if manifest_row is None:
            raise ChangeSetPayloadCorrupt(
                f"Change-set manifest changed during bounded load: {row['change_set_id']}"
            )
        manifest = _history_json_object(manifest_row["data_json"] or "")
        _validate_loaded_change_set_manifest(
            manifest,
            change_set_id=str(row["change_set_id"]),
            lifecycle_status=str(row["status"]),
            lifecycle_terminal_at=row["terminal_at"],
        )
        items.append(manifest)
        loaded_bytes += data_bytes
    store = _default_queue_store()
    store["items"] = items
    store["bounded"] = True
    store["limit"] = normalized_limit
    store["loaded_manifest_bytes"] = loaded_bytes
    store["max_manifest_bytes"] = _CHANGE_SET_MAX_MANIFEST_BYTES
    store["max_total_manifest_bytes"] = _CHANGE_SET_LOAD_MAX_TOTAL_BYTES
    store["truncated_by_bytes"] = truncated_by_bytes
    return store


def load_governance_queue():
    return _load_db_queue("governance_queue", "item_id")


def save_entities(data):
    _save_db_map(
        "entities",
        "entity_id",
        data,
        [
            ("canonical_name", "canonical_name", str),
            ("type", "type", str),
            ("status", "status", str),
            ("ttl", "ttl", float),
            ("decay_weight", "decay_weight", float),
        ],
    )


def save_claims(_data):
    raise CanonicalIdOwnershipError(
        "Full-map claim writes are disabled; use an atomic canonical change set."
    )


def save_evidence(_data):
    raise CanonicalIdOwnershipError(
        "Full-map evidence writes are disabled; use an atomic canonical change set."
    )


def save_sources(data):
    _save_db_map("sources", "source_id", data)


def save_graph_edges(edges: list[dict]):
    if not edges:
        return
    conn = get_connection()
    with transaction():
        for edge in edges:
            params = (
                edge["source_id"],
                edge["target_id"],
                edge["relation"],
                edge.get("weight", 1.0),
                edge.get("updated_at", _utc_now()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
                params,
            )
            conn.execute(
                "INSERT OR REPLACE INTO page_graph_edges (source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
                params,
            )


def save_alias_registry(data):
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    with transaction():
        existing_keys = {
            row["key"] for row in conn.execute("SELECT key FROM alias_registry")
        }
        new_keys = set(data.get("items", {}))
        stale_keys = existing_keys - new_keys
        if stale_keys:
            conn.executemany(
                "DELETE FROM alias_registry WHERE key = ?",
                [(key,) for key in stale_keys],
            )
        for k, v in data.get("items", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
                (k, v, now),
            )


def save_memory_objects(data):
    _save_db_map(
        "operational_memory",
        "memory_id",
        data,
        [
            ("memory_type", "memory_type", str),
            ("score", "memory_score", float),
            ("status", "status", str),
            ("ttl", "ttl", float),
        ],
    )


def save_change_sets(_data):
    raise RuntimeError(
        "Full-history change-set replacement is disabled; use "
        "record_prepared_change_sets or confirmed history maintenance"
    )


def save_governance_queue(data):
    """Compatibility writer that upserts the supplied rows without deleting peers."""
    init_db()
    now = _utc_now()
    data["updated_at"] = now
    values = []
    for item in data.get("items", []):
        item = normalize_governance_item(item)
        if not item.get("item_id"):
            item["item_id"] = uuid.uuid4().hex
        values.append((item["item_id"], json.dumps(item, ensure_ascii=False), now))
    if not values:
        return
    with transaction():
        get_connection().executemany(
            "INSERT OR REPLACE INTO governance_queue (item_id, data_json, updated_at) VALUES (?, ?, ?)",
            values,
        )


def get_governance_item(item_id: str) -> dict | None:
    init_db()
    row = (
        get_connection()
        .execute(
            "SELECT data_json FROM governance_queue WHERE item_id = ?",
            (str(item_id),),
        )
        .fetchone()
    )
    return json.loads(row["data_json"]) if row else None


def upsert_governance_item(item: dict, insert_only: bool = False) -> bool:
    """Persist one governance item without replacing unrelated queue rows."""
    item = normalize_governance_item(item)
    item_id = str(item.get("item_id") or "")
    if not item_id:
        raise ValueError("Governance items require item_id.")
    init_db()
    now = _utc_now()
    statement = "INSERT OR IGNORE" if insert_only else "INSERT OR REPLACE"
    with transaction():
        result = get_connection().execute(
            f"{statement} INTO governance_queue (item_id, data_json, updated_at) VALUES (?, ?, ?)",
            (item_id, json.dumps(item, ensure_ascii=False), now),
        )
    return bool(result.rowcount)


def update_governance_item(
    item_id: str,
    updates: dict,
    expected_statuses: set[str] | None = None,
) -> dict | None:
    """Apply a serialized read-modify-write to one governance row."""
    init_db()
    with transaction():
        row = (
            get_connection()
            .execute(
                "SELECT data_json FROM governance_queue WHERE item_id = ?",
                (str(item_id),),
            )
            .fetchone()
        )
        if row is None:
            return None
        item = json.loads(row["data_json"])
        if (
            expected_statuses is not None
            and str(item.get("status")) not in expected_statuses
        ):
            return None
        item.update(copy.deepcopy(updates))
        get_connection().execute(
            "UPDATE governance_queue SET data_json = ?, updated_at = ? WHERE item_id = ?",
            (json.dumps(item, ensure_ascii=False), _utc_now(), str(item_id)),
        )
        return item


_GOVERNANCE_DEDUP_FIELDS = {
    "pair_key",
    "title",
    "change_set_id",
    "merge_source",
    "merge_target",
}

_GOVERNANCE_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_GOVERNANCE_DEFAULT_PRIORITY = {
    "contradiction": "P1",
    "evidence-gap": "P1",
    "publish-candidate": "P1",
    "merge": "P2",
    "duplicate": "P2",
    "missing-page": "P2",
    "missing-link-target": "P2",
    "orphan-source": "P2",
    "suggestion": "P3",
    "community_naming": "P3",
}


def normalize_governance_item(item: dict) -> dict:
    """Attach deterministic priority metadata without guessing from prose."""
    normalized = copy.deepcopy(item)
    raw_refs = normalized.get("critical_decision_refs")
    refs = []
    if isinstance(raw_refs, list):
        refs = list(
            dict.fromkeys(
                str(reference).strip()
                for reference in raw_refs
                if str(reference).strip()
            )
        )
    explicit_priority = str(normalized.get("priority") or "").upper()
    from vector_lake.decision_registry import verified_decision_refs

    verified_refs = verified_decision_refs(refs)
    if explicit_priority in _GOVERNANCE_PRIORITY_ORDER:
        priority = explicit_priority
    elif verified_refs:
        priority = "P0"
    else:
        priority = _GOVERNANCE_DEFAULT_PRIORITY.get(
            str(normalized.get("type") or ""), "P2"
        )
    normalized["priority"] = priority
    normalized["priority_score"] = (4 - _GOVERNANCE_PRIORITY_ORDER[priority]) * 100
    normalized["critical_decision_refs"] = refs
    normalized["verified_critical_decision_refs"] = verified_refs
    normalized["unverified_critical_decision_refs"] = [
        reference for reference in refs if reference not in verified_refs
    ]
    normalized["decision_relevance"] = (
        "critical" if verified_refs else ("unverified" if refs else "unscored")
    )
    return normalized


def governance_priority_sort_key(item: dict) -> tuple:
    normalized = normalize_governance_item(item)
    return (
        _GOVERNANCE_PRIORITY_ORDER[normalized["priority"]],
        str(normalized.get("created_at") or normalized.get("created") or ""),
        str(normalized.get("item_id") or ""),
    )


def insert_governance_item_if_absent(
    item: dict,
    dedup_fields: tuple[str, ...] = (),
) -> bool:
    """Atomically insert one item unless a row with the same business key exists."""
    invalid = set(dedup_fields) - _GOVERNANCE_DEDUP_FIELDS
    if invalid:
        raise ValueError(f"Unsupported governance dedup fields: {sorted(invalid)}")
    item = normalize_governance_item(item)
    item_id = str(item.get("item_id") or "")
    if not item_id:
        raise ValueError("Governance items require item_id.")
    init_db()
    with transaction():
        if dedup_fields and all(item.get(field) is not None for field in dedup_fields):
            clauses = [
                f"json_extract(data_json, '$.{field}') = ?" for field in dedup_fields
            ]
            values = [item[field] for field in dedup_fields]
            existing = (
                get_connection()
                .execute(
                    "SELECT 1 FROM governance_queue WHERE "
                    + " AND ".join(clauses)
                    + " LIMIT 1",
                    values,
                )
                .fetchone()
            )
            if existing:
                return False
        result = get_connection().execute(
            "INSERT OR IGNORE INTO governance_queue (item_id, data_json, updated_at) VALUES (?, ?, ?)",
            (item_id, json.dumps(item, ensure_ascii=False), _utc_now()),
        )
        return bool(result.rowcount)


def update_governance_items_by_field(field: str, value: str, updates: dict) -> int:
    if field not in _GOVERNANCE_DEDUP_FIELDS:
        raise ValueError(f"Unsupported governance selector: {field}")
    init_db()
    updated = 0
    with transaction():
        rows = (
            get_connection()
            .execute(
                f"SELECT item_id, data_json FROM governance_queue WHERE json_extract(data_json, '$.{field}') = ?",
                (value,),
            )
            .fetchall()
        )
        for row in rows:
            item = json.loads(row["data_json"])
            item.update(copy.deepcopy(updates))
            get_connection().execute(
                "UPDATE governance_queue SET data_json = ?, updated_at = ? WHERE item_id = ?",
                (json.dumps(item, ensure_ascii=False), _utc_now(), row["item_id"]),
            )
            updated += 1
    return updated


# =============================================================================
# V10.1 TARGETED ATOMIC CRUD (Replaces load_all -> save_all pattern)
# =============================================================================
def get_entity(entity_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT data_json FROM entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row:
        return json.loads(row["data_json"])
    return None


def _canonical_entity_records_version(page_records: list[tuple[str, dict]]) -> str:
    normalized_rows = []
    for entity_id, record in page_records:
        data = dict(record)
        # extract_page_objects supplies wall-clock time when legacy pages omit `created`.
        # That fallback is storage metadata, not page state, so it cannot participate in CAS.
        data.pop("created_at", None)
        if isinstance(data.get("raw_text"), str):
            data["raw_text"] = normalize_semantic_text(data["raw_text"])
        normalized = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized_rows.append((entity_id, normalized))
    serialized = "\x1e".join(
        f"{entity_id}\x1f{raw}" for entity_id, raw in sorted(normalized_rows)
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _canonical_entity_rows_version(page_rows: list[tuple[str, str]]) -> str:
    return _canonical_entity_records_version(
        [(entity_id, json.loads(raw)) for entity_id, raw in page_rows]
    )


def canonical_page_version_from_content(filename: str, content: str) -> str:
    """Calculate the canonical entity version that full Markdown would produce."""
    frontmatter, body = split_frontmatter(content)
    extracted = extract_page_objects(filename, frontmatter, body, entity_only=True)
    records = [
        (str(record["entity_id"]), record) for record in extracted.get("entities", [])
    ]
    if not records:
        return ""
    return _canonical_entity_records_version(records)


def canonical_page_versions(
    page_keys: set[str] | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, str]:
    """Return deterministic version tokens for the current canonical page state."""
    if connection is None:
        init_db()
        connection = get_connection()
    requested = set(page_keys) if page_keys is not None else None
    records_by_page: dict[str, list[tuple[str, dict]]] = {}
    if requested:
        rows = []
        ordered = sorted(requested)
        for offset in range(0, len(ordered), 500):
            batch = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                connection.execute(
                    "SELECT entity_id, data_json FROM entities "
                    f"WHERE json_extract(data_json, '$.page_key') IN ({placeholders}) "
                    "ORDER BY entity_id",
                    tuple(batch),
                ).fetchall()
            )
    else:
        rows = connection.execute(
            "SELECT entity_id, data_json FROM entities ORDER BY entity_id"
        ).fetchall()
    for row in rows:
        raw = str(row["data_json"])
        try:
            record = json.loads(raw)
            page_key = str(record.get("page_key") or "")
        except (TypeError, ValueError):
            continue
        if not page_key or (requested is not None and page_key not in requested):
            continue
        records_by_page.setdefault(page_key, []).append((str(row["entity_id"]), record))

    return {
        page_key: _canonical_entity_records_version(page_records)
        for page_key, page_records in records_by_page.items()
    }


def upsert_entity(entity_id: str, data: dict):
    conn = get_connection()
    now = _utc_now()
    cols = [
        "entity_id",
        "canonical_name",
        "type",
        "status",
        "ttl",
        "decay_weight",
        "data_json",
        "updated_at",
    ]
    placeholders = ["?"] * len(cols)
    params = [
        entity_id,
        str(
            data.get("canonical_name")
            or data.get("title")
            or data.get("page_key")
            or entity_id
        ),
        str(data.get("type", "")),
        str(data.get("status", "Active")),
        float(data.get("ttl") or 0.0),
        float(data.get("decay_weight") or 0.0),
        json.dumps(data, ensure_ascii=False),
        now,
    ]
    with transaction():
        conn.execute(
            f"INSERT OR REPLACE INTO entities ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
            params,
        )


def delete_entity(entity_id: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))


# =============================================================================


def _upsert_map_records(store: dict, records: list, key_name: str):
    for record in records:
        key = record[key_name]
        store["items"][key] = record


def _merge_reingested_provenance_record(
    current: dict,
    proposed: dict,
    *,
    preserve_ingested_at: bool = False,
) -> dict:
    """Refresh derived fields without erasing durable review/provenance metadata."""
    merged = copy.deepcopy(current)
    merged.update(copy.deepcopy(proposed))
    if preserve_ingested_at and current.get("ingested_at"):
        merged["ingested_at"] = current["ingested_at"]
    for field in ("classification", "retention_policy"):
        current_value = current.get(field)
        if current_value not in (None, "", "unspecified"):
            merged[field] = copy.deepcopy(current_value)
    if current.get("legal_hold") is True:
        merged["legal_hold"] = True
    parent_refs = list(
        dict.fromkeys(
            [
                *list(current.get("generation_parent_refs") or []),
                *list(proposed.get("generation_parent_refs") or []),
            ]
        )
    )
    if parent_refs:
        merged["generation_parent_refs"] = parent_refs
    return merged


def _merge_reingested_source_records(
    conn,
    key_name: str,
    records: list[dict],
) -> list[dict]:
    record_ids = sorted({str(record[key_name]) for record in records})
    placeholders = ",".join("?" for _ in record_ids)
    existing = {
        str(row[key_name]): json.loads(row["data_json"] or "{}")
        for row in conn.execute(
            f"SELECT {key_name}, data_json FROM sources WHERE {key_name} IN ({placeholders})",
            record_ids,
        ).fetchall()
    }
    return [
        _merge_reingested_provenance_record(
            existing[str(record[key_name])],
            record,
            preserve_ingested_at=True,
        )
        if str(record[key_name]) in existing
        else copy.deepcopy(record)
        for record in records
    ]


def _upsert_canonical_records(table_name: str, key_name: str, records: list[dict]):
    """Upsert only the records in one page-scoped canonical delta."""
    if not records:
        return
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    if table_name == "entities":
        conn.executemany(
            "INSERT INTO entities "
            "(entity_id, canonical_name, type, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "canonical_name = excluded.canonical_name, type = excluded.type, "
            "status = excluded.status, data_json = excluded.data_json, "
            "updated_at = excluded.updated_at "
            "WHERE entities.canonical_name IS NOT excluded.canonical_name "
            "OR entities.type IS NOT excluded.type "
            "OR entities.status IS NOT excluded.status "
            "OR entities.data_json IS NOT excluded.data_json",
            [
                (
                    record[key_name],
                    str(
                        record.get("canonical_name")
                        or record.get("title")
                        or record.get("page_key")
                        or record[key_name]
                    ),
                    str(record.get("type", "")),
                    str(record.get("status", "Active")),
                    json.dumps(record, ensure_ascii=False),
                    now,
                )
                for record in records
            ],
        )
        return
    if table_name == "claims":
        conn.executemany(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(claim_id) DO UPDATE SET "
            "claim_text = excluded.claim_text, status = excluded.status, "
            "data_json = excluded.data_json, updated_at = excluded.updated_at "
            "WHERE claims.claim_text IS NOT excluded.claim_text "
            "OR claims.status IS NOT excluded.status "
            "OR claims.data_json IS NOT excluded.data_json",
            [
                (
                    record[key_name],
                    str(record.get("claim_text", "")),
                    str(record.get("status", "Active")),
                    json.dumps(record, ensure_ascii=False),
                    now,
                )
                for record in records
            ],
        )
        return
    if table_name == "sources":
        records = _merge_reingested_source_records(conn, key_name, records)
    conn.executemany(
        f"INSERT INTO {table_name} ({key_name}, data_json, updated_at) VALUES (?, ?, ?) "
        f"ON CONFLICT({key_name}) DO UPDATE SET "
        "data_json = excluded.data_json, updated_at = excluded.updated_at "
        f"WHERE {table_name}.data_json IS NOT excluded.data_json",
        [
            (record[key_name], json.dumps(record, ensure_ascii=False), now)
            for record in records
        ],
    )


def _canonical_record_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_family(
    record: dict, family_field: str, family_prefix: str
) -> tuple[str, str]:
    locator = dict(record.get("locator") or record.get("projection_locator") or {})
    page_key = str(locator.get("page_key") or "")
    family_id = str(record.get(family_field) or "")
    if not family_id:
        if page_key:
            if record.get("source_id"):
                locator["source_id"] = record.get("source_id")
            if family_field == "evidence_family_id":
                locator["kind"] = record.get("evidence_type")
            family_id = version_family_id(family_prefix, page_key, locator)
        else:
            family_id = _stable_id(
                family_prefix, str(record.get("claim_id") or record.get("evidence_id"))
            )
    return family_id, page_key


def _append_version_records(
    table_name: str,
    id_field: str,
    family_field: str,
    family_prefix: str,
    version_prefix: str,
    records: list[dict],
) -> int:
    """Append distinct content versions without rewriting earlier observations."""
    if table_name not in {"claim_versions", "evidence_versions"}:
        raise ValueError(f"Unsupported version table: {table_name}")
    conn = get_connection()
    prepared: list[tuple[str, str, str, str, str, str]] = []
    seen_version_ids: set[str] = set()
    for record in records:
        record_id = str(record.get(id_field) or "")
        if not record_id:
            continue
        family_id, page_key = _record_family(record, family_field, family_prefix)
        serialized = _canonical_record_json(record)
        record_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        version_id = _stable_id(version_prefix, f"{family_id}:{record_hash}")
        if version_id in seen_version_ids:
            continue
        seen_version_ids.add(version_id)
        prepared.append(
            (version_id, record_id, family_id, page_key, record_hash, serialized)
        )
    if not prepared:
        return 0

    existing_version_ids: set[str] = set()
    ordered_version_ids = [item[0] for item in prepared]
    for offset in range(0, len(ordered_version_ids), 500):
        batch = ordered_version_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        existing_version_ids.update(
            str(row[0])
            for row in conn.execute(
                f"SELECT {version_prefix}_id FROM {table_name} "
                f"WHERE {version_prefix}_id IN ({placeholders})",
                tuple(batch),
            )
        )
    pending = [item for item in prepared if item[0] not in existing_version_ids]
    if not pending:
        return 0

    family_ids = sorted({item[2] for item in pending})
    next_versions = {family_id: 1 for family_id in family_ids}
    for offset in range(0, len(family_ids), 500):
        batch = family_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT {family_field}, COALESCE(MAX(version_no), 0) AS max_version "
            f"FROM {table_name} WHERE {family_field} IN ({placeholders}) "
            f"GROUP BY {family_field}",
            tuple(batch),
        ):
            next_versions[str(row[0])] = int(row[1]) + 1

    recorded_at = _utc_now()
    rows_to_insert = []
    for version_id, record_id, family_id, page_key, record_hash, serialized in pending:
        version_no = next_versions[family_id]
        next_versions[family_id] = version_no + 1
        rows_to_insert.append(
            (
                version_id,
                record_id,
                family_id,
                page_key,
                version_no,
                record_hash,
                serialized,
                recorded_at,
            )
        )
    before_changes = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO {table_name} "
        f"({version_prefix}_id, {id_field}, {family_field}, page_key, version_no, "
        "record_hash, data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows_to_insert,
    )
    return int(conn.total_changes - before_changes)


def _upsert_foundation_records(
    entities: list[dict],
    source_artifacts: list[dict],
    extraction_runs: list[dict],
) -> None:
    conn = get_connection()
    now = _utc_now()
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "")
        page_key = str(entity.get("page_key") or "")
        if not entity_id or not page_key:
            continue
        identity_origin = str(entity.get("identity_origin") or "").strip()
        if not identity_origin:
            identity_origin = (
                "legacy_page_key"
                if entity_id == _stable_id("entity", page_key)
                else "explicit"
            )
        conn.execute(
            "INSERT INTO entity_identities "
            "(entity_id, page_key, canonical_name, identity_origin, data_json, recorded_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET page_key = excluded.page_key, "
            "canonical_name = excluded.canonical_name, data_json = excluded.data_json, "
            "updated_at = excluded.updated_at",
            (
                entity_id,
                page_key,
                str(entity.get("canonical_name") or entity.get("title") or page_key),
                identity_origin,
                _canonical_record_json(entity),
                now,
                now,
            ),
        )
    for artifact in source_artifacts:
        existing_row = conn.execute(
            "SELECT data_json FROM source_artifacts WHERE artifact_id = ?",
            (artifact["artifact_id"],),
        ).fetchone()
        durable_artifact = (
            _merge_reingested_provenance_record(
                json.loads(existing_row["data_json"] or "{}"),
                artifact,
            )
            if existing_row is not None
            else copy.deepcopy(artifact)
        )
        conn.execute(
            "INSERT INTO source_artifacts "
            "(artifact_id, source_id, sha256, byte_size, mime_type, storage_uri, "
            "integrity_status, data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(artifact_id) DO UPDATE SET source_id = excluded.source_id, "
            "sha256 = excluded.sha256, byte_size = excluded.byte_size, "
            "mime_type = excluded.mime_type, storage_uri = excluded.storage_uri, "
            "integrity_status = excluded.integrity_status, data_json = excluded.data_json",
            (
                durable_artifact["artifact_id"],
                durable_artifact["source_id"],
                durable_artifact.get("sha256"),
                durable_artifact.get("byte_size"),
                durable_artifact.get("mime_type"),
                durable_artifact.get("storage_uri"),
                durable_artifact.get("integrity_status", "unverified"),
                _canonical_record_json(durable_artifact),
                now,
            ),
        )
    for run in extraction_runs:
        conn.execute(
            "INSERT OR IGNORE INTO extraction_runs "
            "(run_id, page_key, input_fingerprint, extractor_name, extractor_version, "
            "data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run["run_id"],
                run["page_key"],
                run["input_fingerprint"],
                run["extractor_name"],
                run["extractor_version"],
                _canonical_record_json(run),
                str(run.get("recorded_at") or now),
            ),
        )


_CLAIM_FOUNDATION_FIELDS = (
    "claim_family_id",
    "confidence_kind",
    "calibrated_probability",
    "assessment_status",
    "extractor_name",
    "extractor_version",
    "extraction_run_id",
)
_EVIDENCE_FOUNDATION_FIELDS = (
    "evidence_family_id",
    "artifact_id",
    "projection_locator",
    "source_locator",
    "extraction_run_id",
    "independence_status",
    "lineage_safe",
)
_SOURCE_FOUNDATION_FIELDS = (
    "artifact_id",
    "content_hash",
    "hash_algorithm",
    "byte_size",
    "mime_type",
    "storage_uri",
    "integrity_status",
    "classification",
    "retention_policy",
    "legal_hold",
    "lineage_id",
    "generation_parent_refs",
)


def _merge_missing_record_fields(
    current: dict, proposed: dict, fields: tuple[str, ...]
) -> bool:
    """Add absent foundation fields without overwriting reviewed canonical values."""
    changed = False
    for field in fields:
        if field in current or field not in proposed:
            continue
        current[field] = copy.deepcopy(proposed[field])
        changed = True
    return changed


def _merge_source_foundation_fields(current: dict, proposed: dict) -> bool:
    changed = _merge_missing_record_fields(current, proposed, _SOURCE_FOUNDATION_FIELDS)
    proposed_hash = str(proposed.get("content_hash") or "")
    current_hash = str(current.get("content_hash") or "")
    if (
        proposed.get("integrity_status") == "verified"
        and len(proposed_hash) == 64
        and (
            current.get("integrity_status") != "verified"
            or current_hash != proposed_hash
            or current.get("artifact_id") != proposed.get("artifact_id")
        )
    ):
        if (
            current_hash
            and current_hash != proposed_hash
            and "legacy_content_hash" not in current
        ):
            current["legacy_content_hash"] = current_hash
        for field in (
            "artifact_id",
            "content_hash",
            "hash_algorithm",
            "byte_size",
            "mime_type",
            "storage_uri",
            "integrity_status",
            "lineage_id",
        ):
            if field in proposed:
                current[field] = copy.deepcopy(proposed[field])
        changed = True
    return changed


def _merge_evidence_foundation_fields(current: dict, proposed: dict) -> bool:
    """Upgrade conservative placeholders without replacing reviewed locators."""
    changed = _merge_missing_record_fields(
        current, proposed, _EVIDENCE_FOUNDATION_FIELDS
    )
    current_locator = current.get("source_locator")
    proposed_locator = proposed.get("source_locator")
    current_kind = (
        str(current_locator.get("kind") or "unresolved")
        if isinstance(current_locator, dict)
        else "unresolved"
    )
    proposed_kind = (
        str(proposed_locator.get("kind") or "unresolved")
        if isinstance(proposed_locator, dict)
        else "unresolved"
    )
    if current_kind == "unresolved" and proposed_kind != "unresolved":
        current["source_locator"] = copy.deepcopy(proposed_locator)
        changed = True

    current_independence = str(current.get("independence_status") or "")
    proposed_independence = str(proposed.get("independence_status") or "")
    if (
        current_independence in {"", "unknown_missing_source"}
        and proposed_independence
        and proposed_independence != current_independence
    ):
        current["independence_status"] = proposed_independence
        changed = True
    if (
        current.get("lineage_safe") is not True
        and proposed.get("lineage_safe") is True
        and current_independence != "projection_self_reference"
    ):
        current["lineage_safe"] = True
        changed = True
    return changed


def merge_foundation_record_fields(
    table_name: str,
    current: dict,
    proposed: dict,
) -> bool:
    """Apply the shared merge-only foundation upgrade contract."""
    if table_name == "sources":
        return _merge_source_foundation_fields(current, proposed)
    if table_name == "evidence":
        return _merge_evidence_foundation_fields(current, proposed)
    if table_name == "claims":
        return _merge_missing_record_fields(current, proposed, _CLAIM_FOUNDATION_FIELDS)
    raise ValueError(f"Unsupported foundation table: {table_name}")


def backfill_evidence_foundation_records(extracted: dict) -> dict:
    """Merge one extracted page's foundation metadata into existing canonical rows.

    The caller owns the transaction.  Missing canonical claim/evidence/source IDs
    abort the page so an extraction run can never mark a partial backfill complete.
    """
    conn = get_connection()
    page_key = str(extracted.get("page_key") or "")
    runs = list(extracted.get("extraction_runs") or [])
    if not page_key or len(runs) != 1:
        raise ValueError(
            "Evidence-foundation backfill requires one page and one extraction run."
        )

    record_specs = (
        (
            "claims",
            "claim_id",
            _CLAIM_FOUNDATION_FIELDS,
            list(extracted.get("claims") or []),
        ),
        (
            "evidence",
            "evidence_id",
            _EVIDENCE_FOUNDATION_FIELDS,
            list(extracted.get("evidence") or []),
        ),
        (
            "sources",
            "source_id",
            _SOURCE_FOUNDATION_FIELDS,
            list(extracted.get("sources") or []),
        ),
    )
    merged_by_table: dict[str, list[dict]] = {
        "claims": [],
        "evidence": [],
        "sources": [],
    }
    changed_by_table = {"claims": 0, "evidence": 0, "sources": 0}
    for table_name, key_field, _fields, proposed_records in record_specs:
        for proposed in proposed_records:
            record_id = str(proposed.get(key_field) or "")
            row = conn.execute(
                f"SELECT data_json FROM {table_name} WHERE {key_field} = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Cannot backfill {page_key}: extracted {table_name} ID {record_id!r} "
                    "is absent from canonical state."
                )
            current = json.loads(row["data_json"])
            if merge_foundation_record_fields(table_name, current, proposed):
                merged_by_table[table_name].append(current)
                changed_by_table[table_name] += 1

    changed_claims = merged_by_table["claims"]
    changed_evidence = merged_by_table["evidence"]
    affected_page_keys = {_normalized_owner_page(page_key)}
    claim_owners = _proposed_id_owners(
        changed_claims,
        record_kind="claim",
        id_field="claim_id",
        affected_page_keys=affected_page_keys,
    )
    evidence_owners = _proposed_id_owners(
        changed_evidence,
        record_kind="evidence",
        id_field="evidence_id",
        affected_page_keys=affected_page_keys,
    )
    _validate_locator_id_ownership(
        conn,
        owners=claim_owners,
        record_kind="claim",
    )
    _validate_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    _register_locator_id_ownership(
        conn,
        owners=claim_owners,
        record_kind="claim",
    )
    _register_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    if changed_claims:
        current_claim_ids = tuple(record["claim_id"] for record in changed_claims)
        placeholders = ",".join("?" for _ in current_claim_ids)
        old_claims = [
            json.loads(row["data_json"])
            for row in conn.execute(
                f"SELECT data_json FROM claims WHERE claim_id IN ({placeholders})",
                current_claim_ids,
            )
        ]
        _append_version_records(
            "claim_versions",
            "claim_id",
            "claim_family_id",
            "claimfamily",
            "claim_version",
            old_claims,
        )
    if changed_evidence:
        current_evidence_ids = tuple(
            record["evidence_id"] for record in changed_evidence
        )
        placeholders = ",".join("?" for _ in current_evidence_ids)
        old_evidence = [
            json.loads(row["data_json"])
            for row in conn.execute(
                f"SELECT data_json FROM evidence WHERE evidence_id IN ({placeholders})",
                current_evidence_ids,
            )
        ]
        _append_version_records(
            "evidence_versions",
            "evidence_id",
            "evidence_family_id",
            "evidencefamily",
            "evidence_version",
            old_evidence,
        )

    for table_name, key_field, _, _ in record_specs:
        _upsert_canonical_records(table_name, key_field, merged_by_table[table_name])
    _upsert_foundation_records(
        list(extracted.get("entities") or []),
        list(extracted.get("source_artifacts") or []),
        runs,
    )
    _append_version_records(
        "claim_versions",
        "claim_id",
        "claim_family_id",
        "claimfamily",
        "claim_version",
        changed_claims,
    )
    _append_version_records(
        "evidence_versions",
        "evidence_id",
        "evidence_family_id",
        "evidencefamily",
        "evidence_version",
        changed_evidence,
    )
    return {
        "page_key": page_key,
        "run_id": runs[0]["run_id"],
        "updated_claims": changed_by_table["claims"],
        "updated_evidence": changed_by_table["evidence"],
        "updated_sources": changed_by_table["sources"],
        "source_artifacts": len(extracted.get("source_artifacts") or []),
    }


def _foundation_rows_by_id(
    conn,
    table_name: str,
    key_field: str,
    record_ids: set[str],
) -> dict[str, dict]:
    records: dict[str, dict] = {}
    ordered = sorted(record_ids)
    for offset in range(0, len(ordered), 500):
        batch = ordered[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT {key_field}, data_json FROM {table_name} "
            f"WHERE {key_field} IN ({placeholders})",
            tuple(batch),
        ):
            records[str(row[key_field])] = json.loads(row["data_json"])
    return records


def _dedupe_records(records: list[dict], key_field: str) -> list[dict]:
    deduped: dict[str, dict] = {}
    for record in records:
        record_id = str(record.get(key_field) or "")
        if record_id:
            deduped[record_id] = record
    return list(deduped.values())


def backfill_evidence_foundation_batch(extracted_pages: list[dict]) -> dict:
    """Merge a page batch with bounded SQLite round trips.

    The caller owns the transaction. All current canonical rows are prefetched
    before any mutation, and each changed record is versioned exactly once.
    """
    if not extracted_pages:
        return {
            "pages": 0,
            "updated_claims": 0,
            "updated_evidence": 0,
            "updated_sources": 0,
            "source_artifacts": 0,
        }
    conn = get_connection()
    record_specs = (
        ("claims", "claim_id", "claims"),
        ("evidence", "evidence_id", "evidence"),
        ("sources", "source_id", "sources"),
    )
    page_keys: list[str] = []
    proposed_by_table: dict[str, list[tuple[str, dict]]] = {
        "claims": [],
        "evidence": [],
        "sources": [],
    }
    all_entities: list[dict] = []
    all_artifacts: list[dict] = []
    all_runs: list[dict] = []
    for extracted in extracted_pages:
        page_key = str(extracted.get("page_key") or "")
        runs = list(extracted.get("extraction_runs") or [])
        if not page_key or len(runs) > 1:
            raise ValueError(
                "Evidence-foundation batch allows at most one extraction run per page."
            )
        page_keys.append(page_key)
        for table_name, _key_field, payload_key in record_specs:
            proposed_by_table[table_name].extend(
                (page_key, record) for record in list(extracted.get(payload_key) or [])
            )
        all_entities.extend(list(extracted.get("entities") or []))
        all_artifacts.extend(list(extracted.get("source_artifacts") or []))
        all_runs.extend(runs)

    current_by_table: dict[str, dict[str, dict]] = {}
    for table_name, key_field, _payload_key in record_specs:
        record_ids = {
            str(record.get(key_field) or "")
            for _page_key, record in proposed_by_table[table_name]
            if str(record.get(key_field) or "")
        }
        current_by_table[table_name] = _foundation_rows_by_id(
            conn,
            table_name,
            key_field,
            record_ids,
        )

    changed_by_table: dict[str, dict[str, dict]] = {
        "claims": {},
        "evidence": {},
        "sources": {},
    }
    old_by_table: dict[str, dict[str, dict]] = {
        "claims": {},
        "evidence": {},
        "sources": {},
    }
    for table_name, key_field, _payload_key in record_specs:
        current_map = current_by_table[table_name]
        for page_key, proposed in proposed_by_table[table_name]:
            record_id = str(proposed.get(key_field) or "")
            current = current_map.get(record_id)
            if current is None:
                raise ValueError(
                    f"Cannot backfill {page_key}: extracted {table_name} ID "
                    f"{record_id!r} is absent from canonical state."
                )
            if record_id not in old_by_table[table_name]:
                old_by_table[table_name][record_id] = copy.deepcopy(current)
            if merge_foundation_record_fields(table_name, current, proposed):
                changed_by_table[table_name][record_id] = current

    changed_claims = list(changed_by_table["claims"].values())
    changed_evidence = list(changed_by_table["evidence"].values())
    affected_page_keys = {_normalized_owner_page(page_key) for page_key in page_keys}
    claim_owners = _proposed_id_owners(
        changed_claims,
        record_kind="claim",
        id_field="claim_id",
        affected_page_keys=affected_page_keys,
    )
    evidence_owners = _proposed_id_owners(
        changed_evidence,
        record_kind="evidence",
        id_field="evidence_id",
        affected_page_keys=affected_page_keys,
    )
    _validate_locator_id_ownership(conn, owners=claim_owners, record_kind="claim")
    _validate_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    _register_locator_id_ownership(conn, owners=claim_owners, record_kind="claim")
    _register_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    _append_version_records(
        "claim_versions",
        "claim_id",
        "claim_family_id",
        "claimfamily",
        "claim_version",
        [old_by_table["claims"][record["claim_id"]] for record in changed_claims],
    )
    _append_version_records(
        "evidence_versions",
        "evidence_id",
        "evidence_family_id",
        "evidencefamily",
        "evidence_version",
        [
            old_by_table["evidence"][record["evidence_id"]]
            for record in changed_evidence
        ],
    )
    for table_name, key_field, _payload_key in record_specs:
        _upsert_canonical_records(
            table_name,
            key_field,
            list(changed_by_table[table_name].values()),
        )
    unique_artifacts = _dedupe_records(all_artifacts, "artifact_id")
    _upsert_foundation_records(
        _dedupe_records(all_entities, "entity_id"),
        unique_artifacts,
        _dedupe_records(all_runs, "run_id"),
    )
    _append_version_records(
        "claim_versions",
        "claim_id",
        "claim_family_id",
        "claimfamily",
        "claim_version",
        changed_claims,
    )
    _append_version_records(
        "evidence_versions",
        "evidence_id",
        "evidence_family_id",
        "evidencefamily",
        "evidence_version",
        changed_evidence,
    )
    return {
        "pages": len(extracted_pages),
        "updated_claims": len(changed_by_table["claims"]),
        "updated_evidence": len(changed_by_table["evidence"]),
        "updated_sources": len(changed_by_table["sources"]),
        "source_artifacts": len(unique_artifacts),
    }


def _upsert_operational_memory_records(records: list[dict]):
    if not records:
        return
    conn = get_connection()
    now = _utc_now()
    conn.executemany(
        "INSERT OR REPLACE INTO operational_memory "
        "(memory_id, memory_type, score, status, ttl, data_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record["memory_id"],
                str(record.get("memory_type", "fact")),
                float(record.get("memory_score", 0.0) or 0.0),
                str(record.get("status", "Active")),
                float(record.get("ttl_days", 0.0) or 0.0),
                json.dumps(record, ensure_ascii=False),
                now,
            )
            for record in records
        ],
    )


def _refresh_operational_memory_delta(
    old_claim_ids: set[str], proposed_claims: list[dict]
):
    """Rebuild memory only for changed claims and their direct conflict peers."""
    from vector_lake import governance_metrics

    conn = get_connection()
    proposed_claim_ids = {record["claim_id"] for record in proposed_claims}
    changed_claim_ids = old_claim_ids | proposed_claim_ids
    old_memories = []
    archived_artifact_memories = []
    if changed_claim_ids:
        placeholders = ",".join("?" for _ in changed_claim_ids)
        old_memories = [
            json.loads(row["data_json"])
            for row in conn.execute(
                f"SELECT data_json FROM operational_memory "
                f"WHERE json_extract(data_json, '$.source_claim_id') IN ({placeholders})",
                tuple(sorted(changed_claim_ids)),
            ).fetchall()
        ]
        archived_artifact_memories = [
            memory
            for memory in old_memories
            if str(memory.get("validity_state") or "").lower() == "archived"
            and any(
                str(reason).startswith("infrastructure_artifact:")
                for reason in (memory.get("validity_reasons") or [])
            )
        ]
        conn.execute(
            f"DELETE FROM operational_memory "
            f"WHERE json_extract(data_json, '$.source_claim_id') IN ({placeholders})",
            tuple(sorted(changed_claim_ids)),
        )

    new_memories = [
        _memory_object_from_claim(governance_metrics.annotate_claim_validity(record))
        for record in proposed_claims
    ]
    _upsert_operational_memory_records(new_memories)
    # Archived infrastructure observations are forensic history. A page rewrite
    # or merge must not silently erase them when their legacy claim disappears.
    _upsert_operational_memory_records(archived_artifact_memories)

    related_claim_ids = set(changed_claim_ids)
    impacted_keys = set()
    for memory in [*old_memories, *new_memories]:
        related_claim_ids.update(memory.get("contradicts_claim_ids", []))
        if memory.get("memory_type") != "fact":
            impacted_keys.add((memory.get("memory_type"), memory.get("memory_key")))

    peer_rows = []
    if related_claim_ids:
        placeholders = ",".join("?" for _ in related_claim_ids)
        peer_rows.extend(
            conn.execute(
                f"SELECT data_json FROM operational_memory "
                f"WHERE json_extract(data_json, '$.source_claim_id') IN ({placeholders})",
                tuple(sorted(related_claim_ids)),
            ).fetchall()
        )
    for memory_type, memory_key in sorted(impacted_keys):
        peer_rows.extend(
            conn.execute(
                "SELECT data_json FROM operational_memory "
                "WHERE memory_type = ? AND json_extract(data_json, '$.memory_key') = ?",
                (memory_type, memory_key),
            ).fetchall()
        )

    peer_store = _default_map_store("memory_id")
    for row in peer_rows:
        memory = _decode_operational_memory_json(row["data_json"])
        if (memory.get("conflict_resolution") or {}).get("state") == "superseded":
            memory["validity_state"] = "active"
            memory.pop("superseded_by", None)
            memory.pop("conflict_resolution", None)
            memory.update(score_memory_object(memory))
        peer_store["items"][memory["memory_id"]] = memory
    if peer_store["items"]:
        _resolve_memory_conflicts(peer_store)
        _upsert_operational_memory_records(list(peer_store["items"].values()))


def _refresh_alias_delta(old_entity_ids: set[str], proposed_entities: list[dict]):
    conn = get_connection()
    proposed_entity_ids = {record["entity_id"] for record in proposed_entities}
    affected_entity_ids = old_entity_ids | proposed_entity_ids
    # Merge redirects use a deleted entity ID as the alias key. A later update
    # to the surviving target must not erase those durable identity redirects.
    # Preserve them only while their target entity survives this page delta;
    # true entity deletion still removes every alias that points at it.
    identity_redirects = []
    if proposed_entity_ids:
        placeholders = ",".join("?" for _ in proposed_entity_ids)
        identity_redirects = [
            (str(row["key"]), str(row["value"]), str(row["updated_at"] or _utc_now()))
            for row in conn.execute(
                f"SELECT key, value, updated_at FROM alias_registry "
                f"WHERE value IN ({placeholders})",
                tuple(sorted(proposed_entity_ids)),
            ).fetchall()
            if str(row["key"]).startswith("entity_")
        ]
    if affected_entity_ids:
        placeholders = ",".join("?" for _ in affected_entity_ids)
        conn.execute(
            f"DELETE FROM alias_registry WHERE value IN ({placeholders})",
            tuple(sorted(affected_entity_ids)),
        )
    now = _utc_now()
    aliases = []
    for entity in proposed_entities:
        entity_id = entity["entity_id"]
        aliases.append(
            (
                str(entity.get("canonical_name") or entity.get("title") or entity_id),
                entity_id,
                now,
            )
        )
        aliases.extend(
            (str(alias), entity_id, now) for alias in entity.get("aliases", []) if alias
        )
    if aliases:
        conn.executemany(
            "INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
            aliases,
        )
    if identity_redirects:
        conn.executemany(
            "INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
            identity_redirects,
        )


def rebuild_alias_registry():
    entities = load_entities()
    alias_registry = _default_map_store("alias")
    for entity in entities["items"].values():
        entity_id = entity["entity_id"]
        alias_registry["items"][entity["canonical_name"]] = entity_id
        for alias in entity.get("aliases", []):
            alias_registry["items"][alias] = entity_id
    save_alias_registry(alias_registry)
    return alias_registry


def annotated_claims() -> list[dict]:
    from vector_lake import governance_metrics

    return [
        governance_metrics.annotate_claim_validity(claim)
        for claim in load_claims()["items"].values()
    ]


def _compact_claim_text(text: str, limit: int = 240) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=12).hexdigest()
    return f"{prefix}_{digest}"


def _coerce_float(
    value, default: float, minimum: float = 0.0, maximum: float = 1.0
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dt_rank(value) -> float:
    parsed = _parse_dt(value)
    if not parsed:
        return 0.0
    return parsed.timestamp()


def _normalize_memory_key(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:96] or "general"


def _unicode_search_runs(value: str) -> list[str]:
    """Return case-folded Unicode letter/number runs without script truncation."""
    runs: list[str] = []
    current: list[str] = []
    for char in str(value or "").casefold():
        category = unicodedata.category(char)
        if category.startswith(("L", "N")) or (current and category.startswith("M")):
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _is_cjk_ideograph(char: str) -> bool:
    name = unicodedata.name(char, "")
    return name.startswith("CJK UNIFIED IDEOGRAPH-") or name.startswith(
        "CJK COMPATIBILITY IDEOGRAPH-"
    )


def _query_terms(query: str) -> list[str]:
    runs = _unicode_search_runs(query)
    terms = set(runs)
    cjk_chars = [char for run in runs for char in run if _is_cjk_ideograph(char)]
    for index in range(len(cjk_chars) - 1):
        terms.add(cjk_chars[index] + cjk_chars[index + 1])
    terms.update(cjk_chars)
    return sorted(terms)


def infer_memory_type(claim: dict) -> str:
    explicit = str(claim.get("memory_type") or "").strip().lower().replace("-", "_")
    if explicit in OPERATIONAL_MEMORY_TYPES:
        return explicit

    claim_type = str(claim.get("claim_type") or "").lower().replace("-", "_")
    if claim_type in OPERATIONAL_MEMORY_TYPES:
        return claim_type

    text = f"{claim.get('claim_text', '')} {claim.get('source_page', '')}".lower()
    if any(
        token in text
        for token in (
            "preference",
            "preferred",
            "用户偏好",
            "偏好",
            "首选",
            "不要",
            "倾向",
        )
    ):
        return "preference"
    if any(
        token in text
        for token in (
            "decision",
            "decided",
            "approved",
            "决策",
            "决定",
            "方案",
            "采用",
            "选型",
        )
    ):
        return "decision"
    if any(
        token in text
        for token in (
            "task",
            "todo",
            "pending",
            "blocked",
            "open item",
            "待办",
            "未完成",
            "阻塞",
            "状态",
        )
    ):
        return "task_state"
    return "fact"


def _infer_memory_key(claim: dict, memory_type: str) -> str:
    explicit = (
        claim.get("memory_key")
        or claim.get("preference_key")
        or claim.get("decision_key")
        or claim.get("task_key")
    )
    if explicit:
        return _normalize_memory_key(explicit)

    locator = claim.get("locator") or {}
    heading = locator.get("heading") or claim.get("source_page") or "general"
    text = str(claim.get("claim_text") or "")
    match = re.match(r"^(.{2,80}?)[：:]\s+.+$", text)
    if match:
        heading = match.group(1)

    if memory_type == "fact":
        return _normalize_memory_key(claim.get("claim_id") or text[:96])
    return _normalize_memory_key(f"{memory_type}:{heading}")


def _freshness_score(record: dict, now=None) -> float:
    now = now or datetime.now(timezone.utc)
    valid_to = _parse_dt(record.get("valid_to"))
    if valid_to and valid_to < now:
        return 0.0

    updated_at = _parse_dt(record.get("updated_at")) or _parse_dt(
        record.get("created_at")
    )
    if not updated_at:
        return 0.55

    age_days = max(0, (now - updated_at).days)
    ttl_days = record.get("ttl_days") or MEMORY_TTL_DAYS.get(
        record.get("memory_type", "fact"), 365
    )
    try:
        ttl_days = max(1.0, float(ttl_days))
    except (TypeError, ValueError):
        ttl_days = MEMORY_TTL_DAYS.get(record.get("memory_type", "fact"), 365)
    return round(0.5 ** (age_days / ttl_days), 4)


def score_memory_object(memory: dict, now=None) -> dict:
    confidence_score = _coerce_float(memory.get("confidence"), 0.72)
    authority_score = _coerce_float(memory.get("authority_score"), 0.65)
    importance_score = _coerce_float(memory.get("importance_score"), 0.55)

    purpose_vectors = get_purpose_vectors()
    intent_weight = 0.0
    if purpose_vectors.get("keywords"):
        text = (memory.get("text") or "").lower()
        key = (memory.get("memory_key") or "").lower()
        for kw in purpose_vectors["keywords"]:
            if kw.lower() in text or kw.lower() in key:
                intent_weight = float(purpose_vectors.get("weight_boost", 0.20))
                break

    importance_score = min(1.0, importance_score + intent_weight)

    freshness_score = _freshness_score(memory, now=now)
    reinforcement_count = int(memory.get("reinforcement_count") or 0)
    reinforcement_score = min(
        1.0, math.log1p(max(0, reinforcement_count)) / math.log(8)
    )
    validity_factor = VALIDITY_FACTORS.get(
        str(memory.get("validity_state", "active")).lower(), 0.5
    )
    memory_score = (
        0.30 * confidence_score
        + 0.25 * freshness_score
        + 0.20 * authority_score
        + 0.15 * importance_score
        + 0.10 * reinforcement_score
    ) * validity_factor
    return {
        "confidence_score": round(confidence_score, 4),
        "freshness_score": round(freshness_score, 4),
        "authority_score": round(authority_score, 4),
        "importance_score": round(importance_score, 4),
        "reinforcement_score": round(reinforcement_score, 4),
        "validity_factor": round(validity_factor, 4),
        "memory_score": round(memory_score, 4),
    }


def _memory_object_from_claim(claim: dict) -> dict:
    memory_type = infer_memory_type(claim)
    memory_key = _infer_memory_key(claim, memory_type)
    memory_id = _stable_id("mem", f"{claim.get('claim_id')}:{memory_type}:{memory_key}")
    source_ids = list(claim.get("source_ids", []))
    evidence_ids = list(claim.get("evidence_ids", []))
    memory = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": memory_key,
        "text": claim.get("claim_text", ""),
        "value": claim.get("memory_value") or claim.get("claim_text", ""),
        "source_claim_id": claim.get("claim_id"),
        "source_page": claim.get("source_page"),
        "locator": claim.get("locator", {}),
        "subject_entity_ids": list(claim.get("subject_entity_ids", [])),
        "evidence_ids": evidence_ids,
        "source_ids": source_ids,
        "source_count": len(source_ids),
        "status": claim.get("status", "Active"),
        "validity_state": claim.get("validity_state", "active"),
        "validity_reasons": claim.get("validity_reasons", []),
        "temporal_anchor": claim.get("temporal_anchor"),
        "valid_from": claim.get("valid_from"),
        "valid_to": claim.get("valid_to"),
        "review_after": claim.get("review_after"),
        "created_at": claim.get("created_at"),
        "updated_at": claim.get("updated_at"),
        "confidence": claim.get("confidence", 0.72),
        "authority_score": claim.get("authority_score", 0.72 if source_ids else 0.48),
        "importance_score": claim.get("importance_score", 0.55),
        "reinforcement_count": claim.get("reinforcement_count", len(evidence_ids)),
        "ttl_days": claim.get("ttl_days") or MEMORY_TTL_DAYS.get(memory_type, 365),
        "contradicts_claim_ids": list(claim.get("contradicts", [])),
    }
    memory.update(score_memory_object(memory))
    return memory


def _rank_memory_for_conflict(
    memory: dict, explicit_contradiction: bool = False
) -> tuple:
    if explicit_contradiction:
        return (
            memory.get("authority_score", 0),
            memory.get("confidence_score", 0),
            _dt_rank(memory.get("updated_at")),
            memory.get("memory_score", 0),
        )
    return (
        _dt_rank(memory.get("updated_at")),
        memory.get("authority_score", 0),
        memory.get("confidence_score", 0),
        memory.get("memory_score", 0),
    )


def _mark_superseded(loser: dict, winner: dict, reason: str):
    loser["validity_state"] = "superseded"
    loser["superseded_by"] = winner["memory_id"]
    loser["conflict_resolution"] = {
        "state": "superseded",
        "winner": winner["memory_id"],
        "rule": reason,
        "resolved_at": _utc_now(),
    }
    loser.update(score_memory_object(loser))


def _resolve_memory_conflicts(store: dict) -> dict:
    items = store.get("items", {})
    by_claim_id = {
        memory.get("source_claim_id"): memory
        for memory in items.values()
        if memory.get("source_claim_id")
    }
    conflict_events = []

    for memory in list(items.values()):
        for right_claim_id in memory.get("contradicts_claim_ids", []):
            other = by_claim_id.get(right_claim_id)
            if not other or other["memory_id"] == memory["memory_id"]:
                continue
            left_rank = _rank_memory_for_conflict(memory, explicit_contradiction=True)
            right_rank = _rank_memory_for_conflict(other, explicit_contradiction=True)
            if left_rank == right_rank:
                memory["validity_state"] = "conflicted"
                other["validity_state"] = "conflicted"
                memory.update(score_memory_object(memory))
                other.update(score_memory_object(other))
                conflict_events.append(
                    {
                        "type": "unresolved-explicit-contradiction",
                        "memory_ids": sorted([memory["memory_id"], other["memory_id"]]),
                    }
                )
            elif left_rank > right_rank:
                _mark_superseded(
                    other, memory, "explicit-contradiction:authority-confidence-recency"
                )
            else:
                _mark_superseded(
                    memory, other, "explicit-contradiction:authority-confidence-recency"
                )

    grouped = {}
    for memory in items.values():
        if memory.get("memory_type") == "fact":
            continue
        if str(memory.get("validity_state", "")).lower() in {"expired", "archived"}:
            continue
        grouped.setdefault(
            (memory.get("memory_type"), memory.get("memory_key")), []
        ).append(memory)

    for (memory_type, memory_key), candidates in grouped.items():
        # Reset validity state so superseded ones can compete again
        for c in candidates:
            if c.get("validity_state") == "superseded":
                c["validity_state"] = "active"
                c.pop("superseded_by", None)
                c.pop("conflict_resolution", None)

        if len(candidates) <= 1:
            continue
        ordered = sorted(candidates, key=_rank_memory_for_conflict, reverse=True)
        winner = ordered[0]
        winner["conflict_resolution"] = {
            "state": "winner",
            "rule": f"{memory_type}:newer-authority-confidence",
            "resolved_at": _utc_now(),
            "competing_memory_ids": [item["memory_id"] for item in ordered[1:]],
        }
        for loser in ordered[1:]:
            _mark_superseded(loser, winner, f"{memory_type}:newer-authority-confidence")
        conflict_events.append(
            {
                "type": "typed-memory-supersession",
                "memory_type": memory_type,
                "memory_key": memory_key,
                "winner": winner["memory_id"],
                "losers": [item["memory_id"] for item in ordered[1:]],
            }
        )

    store["conflict_events"] = conflict_events
    store["memory_type_counts"] = {}
    for memory in items.values():
        memory_type = memory.get("memory_type", "fact")
        store["memory_type_counts"][memory_type] = (
            store["memory_type_counts"].get(memory_type, 0) + 1
        )
    return store


def rebuild_operational_memory() -> dict:
    claims = annotated_claims()
    store = _default_map_store("memory_id")
    existing = load_memory_objects()
    for memory_id, memory in existing.get("items", {}).items():
        reasons = memory.get("validity_reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        if str(memory.get("validity_state") or "").lower() == "archived" and any(
            str(reason).startswith("infrastructure_artifact:") for reason in reasons
        ):
            store["items"][memory_id] = memory
    for claim in claims:
        if classify_non_claim_text(str(claim.get("claim_text") or "")):
            continue
        memory = _memory_object_from_claim(claim)
        store["items"][memory["memory_id"]] = memory
    store = _resolve_memory_conflicts(store)
    save_memory_objects(store)
    return store


def _decode_operational_memory_json(payload: str) -> dict:
    """Use the available fast decoder while preserving stdlib compatibility."""
    if _orjson is not None:
        try:
            return _orjson.loads(payload)
        except _orjson.JSONDecodeError:
            pass
    return json.loads(payload)


class _ExactTermMatcher:
    """Match all exact Unicode substrings in one regex-engine scan per field."""

    __slots__ = (
        "_pattern",
        "_closure",
        "_term_masks",
        "_direct_masks",
        "_empty_matches",
        "_all_matches",
    )

    def __init__(self, terms: list[str]):
        term_masks: dict[str, int] = {}
        empty_matches = 0
        for term_index, term in enumerate(terms):
            term_mask = 1 << term_index
            if not term:
                empty_matches |= term_mask
                continue
            term_masks[term] = term_masks.get(term, 0) | term_mask

        ordered_terms = sorted(term_masks, key=lambda term: (-len(term), term))
        closure: dict[str, int] = {}
        for matched_term in ordered_terms:
            matched_mask = 0
            for term, term_mask in term_masks.items():
                if term in matched_term:
                    matched_mask |= term_mask
            closure[matched_term] = matched_mask

        direct_masks = (
            term_masks if any(len(term) <= 2 for term in term_masks) else None
        )
        self._pattern = (
            re.compile(
                "(?=(" + "|".join(re.escape(term) for term in ordered_terms) + "))"
            )
            if ordered_terms and direct_masks is None
            else None
        )
        self._closure = closure
        self._term_masks = term_masks
        self._empty_matches = empty_matches
        self._direct_masks = direct_masks
        self._all_matches = (1 << len(terms)) - 1

    def matched_term_count(self, text: str) -> int:
        matched = self._empty_matches
        if self._direct_masks is not None:
            for term, term_mask in self._direct_masks.items():
                if term in text:
                    matched |= term_mask
            return matched.bit_count()

        if self._pattern is None or matched == self._all_matches:
            return matched.bit_count()
        stalled_matches = 0
        for match in self._pattern.finditer(text):
            previous = matched
            matched |= self._closure[match.group(1)]
            if matched == self._all_matches:
                break
            stalled_matches = stalled_matches + 1 if matched == previous else 0
            if stalled_matches >= 32:
                for term, term_mask in self._term_masks.items():
                    if matched & term_mask == term_mask:
                        continue
                    if term in text:
                        matched |= term_mask
                break
        return matched.bit_count()


def _memory_relevance(
    memory: dict,
    terms: list[str],
    matcher: _ExactTermMatcher | None = None,
) -> float:
    if not terms:
        return 0.0
    haystacks = {
        "key": str(memory.get("memory_key", "")).lower(),
        "text": str(memory.get("text", "")).lower(),
        "page": str(memory.get("source_page", "")).lower(),
        "type": str(memory.get("memory_type", "")).lower(),
    }
    if matcher is not None:
        return (
            4.0 * matcher.matched_term_count(haystacks["key"])
            + 3.0 * matcher.matched_term_count(haystacks["text"])
            + matcher.matched_term_count(haystacks["page"])
            + matcher.matched_term_count(haystacks["type"])
        )

    score = 0.0
    for term in terms:
        if term in haystacks["key"]:
            score += 4.0
        if term in haystacks["text"]:
            score += 3.0
        if term in haystacks["page"]:
            score += 1.0
        if term in haystacks["type"]:
            score += 1.0
    return score


_MEMORY_HIDDEN_STATES = frozenset({"archived", "expired", "superseded"})
_MEMORY_QUERY_CHAR_LIMIT = 16_384
_MEMORY_QUERY_TERM_LIMIT = 128
_MEMORY_QUERY_TERM_CHAR_LIMIT = 512
_MEMORY_MATCHER_PATTERN_CHAR_LIMIT = 8_192
_MEMORY_CANDIDATE_TERM_LIMIT = 12
_MEMORY_SEARCH_INDEX_DEFAULT_BATCH = 512
_MEMORY_SEARCH_INDEX_MAX_BATCH = 10_000
_MEMORY_SEARCH_INDEX_SCHEMA_VERSION = 7
_MEMORY_SEARCH_DEGRADED_ROW_LIMIT = 5_000
_MEMORY_SEARCH_DEGRADED_ROW_LIMIT_MAX = 50_000
_MEMORY_SEARCH_PROGRESS_STALL_SECONDS = 15 * 60
_MEMORY_SEARCH_INDEX_TABLES = frozenset(
    {
        "operational_memory_search_fts",
        "operational_memory_search_short_fts",
        "operational_memory_search_docs",
        "operational_memory_search_pending",
        "operational_memory_search_state",
        "operational_memory_search_revision",
    }
)


def _operational_memory_search_index_enabled() -> bool:
    value = os.environ.get("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _operational_memory_search_auto_maintenance_enabled() -> bool:
    value = os.environ.get("VECTOR_LAKE_OPERATIONAL_MEMORY_AUTO_MAINTAIN", "1")
    return _operational_memory_search_index_enabled() and str(
        value
    ).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _operational_memory_degraded_row_limit() -> int:
    raw = os.environ.get(
        "VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT",
        str(_MEMORY_SEARCH_DEGRADED_ROW_LIMIT),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _MEMORY_SEARCH_DEGRADED_ROW_LIMIT
    return max(1, min(_MEMORY_SEARCH_DEGRADED_ROW_LIMIT_MAX, value))


def _operational_memory_unbounded_fallback_enabled() -> bool:
    value = os.environ.get(
        "VECTOR_LAKE_OPERATIONAL_MEMORY_ALLOW_UNBOUNDED_FALLBACK",
        "0",
    )
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _operational_memory_retry_after_seconds() -> int:
    raw = os.environ.get("VECTOR_LAKE_OPERATIONAL_MEMORY_RETRY_AFTER_SECONDS", "5")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 5
    return max(1, min(300, value))


def _operational_memory_search_batch_size() -> int:
    raw = os.environ.get(
        "VECTOR_LAKE_OPERATIONAL_MEMORY_INDEX_BATCH",
        str(_MEMORY_SEARCH_INDEX_DEFAULT_BATCH),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _MEMORY_SEARCH_INDEX_DEFAULT_BATCH
    return max(0, min(_MEMORY_SEARCH_INDEX_MAX_BATCH, value))


def _memory_search_index_schema_available(conn: sqlite3.Connection) -> bool:
    placeholders = ", ".join("?" for _ in _MEMORY_SEARCH_INDEX_TABLES)
    rows = conn.execute(
        f"SELECT name FROM sqlite_master WHERE name IN ({placeholders})",
        tuple(sorted(_MEMORY_SEARCH_INDEX_TABLES)),
    ).fetchall()
    if {str(row[0]) for row in rows} != _MEMORY_SEARCH_INDEX_TABLES:
        return False
    docs_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(operational_memory_search_docs)")
    }
    state_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(operational_memory_search_state)")
    }
    revision_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(operational_memory_search_revision)")
    }
    return (
        {
            "doc_id",
            "memory_id",
            "source_updated_at",
            "source_sha256",
        }.issubset(docs_columns)
        and {
            "singleton",
            "backfill_cursor",
            "backfill_target",
            "schema_version",
            "proof_status",
            "proof_generation",
            "canonical_corpus_sha256",
            "docs_corpus_sha256",
            "trigram_corpus_sha256",
            "short_corpus_sha256",
            "updated_at",
        }.issubset(state_columns)
        and {
            "singleton",
            "revision",
            "updated_at",
        }.issubset(revision_columns)
    )


def _operational_memory_search_index_counts(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Return one-snapshot canonical and derived-row counts."""
    return {
        "canonical_documents": int(
            conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0]
        ),
        "indexed_documents": int(
            conn.execute(
                "SELECT COUNT(*) FROM operational_memory_search_docs"
            ).fetchone()[0]
        ),
        "trigram_indexed_documents": int(
            conn.execute(
                "SELECT COUNT(*) FROM operational_memory_search_fts"
            ).fetchone()[0]
        ),
        "short_indexed_documents": int(
            conn.execute(
                "SELECT COUNT(*) FROM operational_memory_search_short_fts"
            ).fetchone()[0]
        ),
    }


def _operational_memory_search_index_count_mismatch(
    counts: dict[str, int],
    *,
    backfill_complete: bool,
) -> bool:
    indexed = counts["indexed_documents"]
    return (
        (backfill_complete and indexed != counts["canonical_documents"])
        or counts["trigram_indexed_documents"] != indexed
        or counts["short_indexed_documents"] != indexed
    )


def _memory_search_document_ids(
    conn: sqlite3.Connection,
    memory_ids: list[str],
) -> dict[str, int]:
    documents: dict[str, int] = {}
    for offset in range(0, len(memory_ids), 900):
        chunk = memory_ids[offset : offset + 900]
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT memory_id, doc_id FROM operational_memory_search_docs "
            f"WHERE memory_id IN ({placeholders})",
            tuple(chunk),
        ):
            documents[str(row[0])] = int(row[1])
    return documents


def _memory_short_token_projection(*values: str) -> str:
    """Project exact one/two-character substrings into stable FTS tokens."""
    tokens: set[str] = set()
    for value in values:
        for run in _unicode_search_runs(value):
            tokens.update(run)
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return " ".join(sorted(tokens))


def _delete_memory_search_documents(
    conn: sqlite3.Connection,
    memory_ids: list[str],
) -> None:
    if not memory_ids:
        return
    documents = _memory_search_document_ids(conn, memory_ids)
    if not documents:
        return
    doc_ids = [(doc_id,) for doc_id in documents.values()]
    conn.executemany(
        "DELETE FROM operational_memory_search_fts WHERE rowid = ?",
        doc_ids,
    )
    conn.executemany(
        "DELETE FROM operational_memory_search_short_fts WHERE rowid = ?",
        doc_ids,
    )
    conn.executemany(
        "DELETE FROM operational_memory_search_docs WHERE doc_id = ?",
        doc_ids,
    )


def _upsert_memory_search_documents(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str | None]],
) -> None:
    if not rows:
        return
    decoded = []
    for memory_id, payload, updated_at in rows:
        memory = _decode_operational_memory_json(payload)
        decoded.append(
            (
                memory_id,
                updated_at,
                key_text := str(memory.get("memory_key", "")).lower(),
                memory_text := str(memory.get("text", "")).lower(),
                page_text := str(memory.get("source_page", "")).lower(),
                type_text := str(memory.get("memory_type", "fact")).lower(),
                _memory_short_token_projection(
                    key_text,
                    memory_text,
                    page_text,
                    type_text,
                ),
                operational_memory_search_source_sha256(
                    memory_id,
                    payload,
                    updated_at,
                ),
            )
        )

    memory_ids = [row[0] for row in decoded]
    existing = _memory_search_document_ids(conn, memory_ids)
    conn.executemany(
        "INSERT OR IGNORE INTO operational_memory_search_docs "
        "(memory_id, source_updated_at, source_sha256) VALUES (?, ?, ?)",
        [(row[0], row[1], row[7]) for row in decoded],
    )
    documents = _memory_search_document_ids(conn, memory_ids)
    if len(documents) != len(set(memory_ids)):
        raise sqlite3.IntegrityError(
            "operational-memory search document mapping is incomplete"
        )

    if existing:
        existing_doc_ids = [(doc_id,) for doc_id in existing.values()]
        conn.executemany(
            "DELETE FROM operational_memory_search_fts WHERE rowid = ?",
            existing_doc_ids,
        )
        conn.executemany(
            "DELETE FROM operational_memory_search_short_fts WHERE rowid = ?",
            existing_doc_ids,
        )
    conn.executemany(
        "UPDATE operational_memory_search_docs SET source_updated_at = ?, "
        "source_sha256 = ? "
        "WHERE doc_id = ?",
        [(row[1], row[7], documents[row[0]]) for row in decoded],
    )
    conn.executemany(
        "INSERT INTO operational_memory_search_fts "
        "(rowid, key_text, memory_text, page_text, type_text) "
        "VALUES (?, ?, ?, ?, ?)",
        [(documents[row[0]], row[2], row[3], row[4], row[5]) for row in decoded],
    )
    conn.executemany(
        "INSERT INTO operational_memory_search_short_fts (rowid, short_text) "
        "VALUES (?, ?)",
        [(documents[row[0]], row[6]) for row in decoded],
    )


def _operational_memory_search_certification_gap_hook() -> None:
    """Fault-injection seam for the post-commit certification fence."""


def _advance_operational_memory_search_index(
    conn: sqlite3.Connection,
    batch_size: int | None = None,
) -> tuple[str, str]:
    """Apply a bounded pending/backfill slice and return its durable cursor."""
    if batch_size is None:
        batch_size = _operational_memory_search_batch_size()
    else:
        batch_size = max(0, min(_MEMORY_SEARCH_INDEX_MAX_BATCH, int(batch_size)))
    should_certify = False
    certification_data_version = None
    with transaction(max_wait_seconds=0.1):
        state = conn.execute(
            "SELECT backfill_cursor, backfill_target, proof_status "
            "FROM operational_memory_search_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            raise sqlite3.OperationalError("operational-memory search state is missing")
        cursor = str(state[0] or "")
        target = str(state[1] or "")
        proof_status = str(state[2] or "")
        projection_changed = False

        pending_exists = (
            conn.execute(
                "SELECT 1 FROM operational_memory_search_pending LIMIT 1"
            ).fetchone()
            is not None
        )
        if batch_size and cursor >= target and proof_status != "ready":
            # A completed cursor without a ready proof is not a trustworthy
            # projection. Certification may have failed after the previous
            # replay, leaving physical FTS bytes exposed to equal-count drift.
            # Restart the bounded replay before any later certification can
            # adopt those existing bytes as a new baseline.
            mark_operational_memory_search_rebuild_required(conn)
            cursor = ""
            target = str(
                conn.execute(
                    "SELECT COALESCE(MAX(memory_id), '') FROM operational_memory"
                ).fetchone()[0]
                or ""
            )
            proof_status = "rebuild_required"
        if batch_size and cursor >= target and not pending_exists:
            counts = _operational_memory_search_index_counts(conn)
            count_mismatch = _operational_memory_search_index_count_mismatch(
                counts,
                backfill_complete=True,
            )
            if not count_mismatch and proof_status == "ready":
                integrity = verify_operational_memory_search_integrity(conn)
                if integrity.get("issue") in {
                    "operational_memory_search_integrity",
                    "operational_memory_search_integrity_state",
                }:
                    mark_operational_memory_search_rebuild_required(conn)
                    cursor = ""
                    target = str(
                        conn.execute(
                            "SELECT COALESCE(MAX(memory_id), '') "
                            "FROM operational_memory"
                        ).fetchone()[0]
                        or ""
                    )
                    proof_status = "rebuild_required"

        pending_rows = conn.execute(
            "SELECT p.memory_id, p.operation, om.data_json, om.updated_at "
            "FROM operational_memory_search_pending AS p "
            "LEFT JOIN operational_memory AS om ON om.memory_id = p.memory_id "
            "ORDER BY COALESCE(p.queued_at, ''), p.memory_id LIMIT ?",
            (batch_size,),
        ).fetchall()
        if pending_rows and proof_status == "ready":
            invalidate_operational_memory_search_proof(conn)
            proof_status = "rebuild_required"
        _delete_memory_search_documents(
            conn,
            [
                str(row[0])
                for row in pending_rows
                if str(row[1]) == "delete" or row[2] is None
            ],
        )
        _upsert_memory_search_documents(
            conn,
            [
                (str(row[0]), row[2], row[3])
                for row in pending_rows
                if str(row[1]) != "delete" and row[2] is not None
            ],
        )
        if pending_rows:
            projection_changed = True
            conn.executemany(
                "DELETE FROM operational_memory_search_pending WHERE memory_id = ?",
                [(str(row[0]),) for row in pending_rows],
            )

        remaining = max(0, batch_size - len(pending_rows))
        if remaining:
            orphan_memory_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT docs.memory_id "
                    "FROM operational_memory_search_docs AS docs "
                    "LEFT JOIN operational_memory AS om "
                    "ON om.memory_id = docs.memory_id "
                    "WHERE om.memory_id IS NULL "
                    "ORDER BY docs.memory_id LIMIT ?",
                    (remaining,),
                )
            ]
            if orphan_memory_ids and proof_status == "ready":
                invalidate_operational_memory_search_proof(conn)
                proof_status = "rebuild_required"
            _delete_memory_search_documents(conn, orphan_memory_ids)
            projection_changed = projection_changed or bool(orphan_memory_ids)
            remaining -= len(orphan_memory_ids)

        for table_name in (
            "operational_memory_search_fts",
            "operational_memory_search_short_fts",
        ):
            if not remaining:
                break
            orphan_doc_ids = [
                int(row[0])
                for row in conn.execute(
                    f"SELECT search.rowid FROM {table_name} AS search "
                    "LEFT JOIN operational_memory_search_docs AS docs "
                    "ON docs.doc_id = search.rowid "
                    "WHERE docs.doc_id IS NULL ORDER BY search.rowid LIMIT ?",
                    (remaining,),
                )
            ]
            if orphan_doc_ids:
                if proof_status == "ready":
                    invalidate_operational_memory_search_proof(conn)
                    proof_status = "rebuild_required"
                conn.executemany(
                    f"DELETE FROM {table_name} WHERE rowid = ?",
                    [(doc_id,) for doc_id in orphan_doc_ids],
                )
                projection_changed = True
                remaining -= len(orphan_doc_ids)

        if remaining and cursor >= target:
            counts = _operational_memory_search_index_counts(conn)
            if _operational_memory_search_index_count_mismatch(
                counts,
                backfill_complete=True,
            ):
                mark_operational_memory_search_rebuild_required(conn)
                cursor = ""
                target = str(
                    conn.execute(
                        "SELECT COALESCE(MAX(memory_id), '') FROM operational_memory"
                    ).fetchone()[0]
                    or ""
                )
                proof_status = "rebuild_required"

        backfill_rows = []
        if remaining and cursor < target:
            backfill_rows = conn.execute(
                "SELECT memory_id, data_json, updated_at "
                "FROM operational_memory WHERE memory_id > ? AND memory_id <= ? "
                "ORDER BY memory_id LIMIT ?",
                (cursor, target, remaining),
            ).fetchall()
            if backfill_rows and proof_status == "ready":
                invalidate_operational_memory_search_proof(conn)
                proof_status = "rebuild_required"
            _upsert_memory_search_documents(
                conn,
                [(str(row[0]), row[1], row[2]) for row in backfill_rows],
            )
            projection_changed = projection_changed or bool(backfill_rows)
            cursor = str(backfill_rows[-1][0]) if backfill_rows else target

        conn.execute(
            "UPDATE operational_memory_search_state SET "
            "backfill_cursor = ?, updated_at = ? WHERE singleton = 1",
            (cursor, _utc_now()),
        )
        pending_after = (
            conn.execute(
                "SELECT 1 FROM operational_memory_search_pending LIMIT 1"
            ).fetchone()
            is not None
        )
        counts_after = _operational_memory_search_index_counts(conn)
        complete = bool(
            cursor >= target
            and not pending_after
            and not _operational_memory_search_index_count_mismatch(
                counts_after,
                backfill_complete=True,
            )
        )
        should_certify = complete and (projection_changed or proof_status != "ready")
        if should_certify:
            certification_data_version = int(
                conn.execute("PRAGMA data_version").fetchone()[0] or 0
            )
    if should_certify:
        try:
            # FTS5 can finalize segment bytes at transaction commit. Certify in
            # a fresh write-locked snapshot so the stored physical digest is
            # the exact post-commit representation readers will observe.
            _operational_memory_search_certification_gap_hook()
            with transaction(max_wait_seconds=0.1):
                observed_data_version = int(
                    conn.execute("PRAGMA data_version").fetchone()[0] or 0
                )
                if observed_data_version != certification_data_version:
                    # An external commit in the only unlocked gap may have
                    # changed contentless FTS bytes. Never absorb those bytes as
                    # a new trusted baseline; force a full bounded replay.
                    mark_operational_memory_search_rebuild_required(conn)
                    cursor = ""
                    target = str(
                        conn.execute(
                            "SELECT COALESCE(MAX(memory_id), '') "
                            "FROM operational_memory"
                        ).fetchone()[0]
                        or ""
                    )
                else:
                    certify_operational_memory_search_integrity(conn)
        except (
            OperationalMemorySearchIntegrityLimitExceeded,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            log.warning(
                "Operational-memory search proof remains unready: %s",
                exc,
            )
    return cursor, target


def _memory_fts_expression(terms: list[str]) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _operational_memory_progress_age_seconds(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def operational_memory_search_index_status(
    *,
    connection: sqlite3.Connection | None = None,
    allow_integrity_scan: bool = True,
) -> dict:
    """Inspect derived-index progress without creating schema or changing state."""
    configured = _operational_memory_search_index_enabled()
    if connection is None and not peek_db_path().exists():
        warnings = ["operational_memory_search_database_missing"] if configured else []
        return {
            "configured": configured,
            "auto_maintenance_configured": (
                _operational_memory_search_auto_maintenance_enabled()
            ),
            "available": False,
            "ready": False,
            "status": "unavailable" if configured else "disabled",
            "warnings": warnings,
            "canonical_documents": 0,
            "indexed_documents": 0,
            "trigram_indexed_documents": 0,
            "short_indexed_documents": 0,
            "pending": 0,
            "degraded_row_limit": _operational_memory_degraded_row_limit(),
            "retry_after_seconds": _operational_memory_retry_after_seconds(),
        }
    conn = connection or get_connection()
    if not _memory_search_index_schema_available(conn):
        canonical_documents = 0
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'operational_memory'"
        ).fetchone():
            canonical_documents = int(
                conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0]
            )
        return {
            "configured": configured,
            "auto_maintenance_configured": (
                _operational_memory_search_auto_maintenance_enabled()
            ),
            "available": False,
            "ready": False,
            "status": "unavailable" if configured else "disabled",
            "warnings": (
                ["operational_memory_search_schema_unavailable"] if configured else []
            ),
            "canonical_documents": canonical_documents,
            "indexed_documents": 0,
            "trigram_indexed_documents": 0,
            "short_indexed_documents": 0,
            "pending": 0,
            "degraded_row_limit": _operational_memory_degraded_row_limit(),
            "retry_after_seconds": _operational_memory_retry_after_seconds(),
        }
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, schema_version, updated_at, "
        "proof_status, proof_generation "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone()
    cursor = str(state[0] or "") if state is not None else ""
    target = str(state[1] or "") if state is not None else ""
    progress_updated_at = str(state[3] or "") if state is not None else ""
    progress_age_seconds = _operational_memory_progress_age_seconds(progress_updated_at)
    pending = int(
        conn.execute(
            "SELECT COUNT(*) FROM operational_memory_search_pending"
        ).fetchone()[0]
    )
    counts = _operational_memory_search_index_counts(conn)
    indexed_documents = counts["indexed_documents"]
    trigram_indexed_documents = counts["trigram_indexed_documents"]
    short_indexed_documents = counts["short_indexed_documents"]
    canonical_documents = counts["canonical_documents"]
    schema_version = int(state[2]) if state is not None else None
    proof_status = str(state[4] or "") if state is not None else "missing"
    proof_generation = str(state[5] or "") if state is not None else ""
    warnings: list[str] = []
    if schema_version != _MEMORY_SEARCH_INDEX_SCHEMA_VERSION:
        warnings.append(
            "operational_memory_search_schema_version_mismatch:"
            f"{schema_version}!={_MEMORY_SEARCH_INDEX_SCHEMA_VERSION}"
        )
    if cursor < target:
        warnings.append("operational_memory_search_backfill_incomplete")
    if pending:
        warnings.append(f"operational_memory_search_pending:{pending}")
    if cursor >= target and indexed_documents != canonical_documents:
        warnings.append(
            "operational_memory_search_document_count_mismatch:"
            f"{indexed_documents}!={canonical_documents}"
        )
    if short_indexed_documents != indexed_documents:
        warnings.append(
            "operational_memory_search_short_index_count_mismatch:"
            f"{short_indexed_documents}!={indexed_documents}"
        )
    if trigram_indexed_documents != indexed_documents:
        warnings.append(
            "operational_memory_search_trigram_index_count_mismatch:"
            f"{trigram_indexed_documents}!={indexed_documents}"
        )
    integrity = None
    nominally_complete = bool(
        configured
        and schema_version == _MEMORY_SEARCH_INDEX_SCHEMA_VERSION
        and cursor >= target
        and not pending
        and not _operational_memory_search_index_count_mismatch(
            counts,
            backfill_complete=True,
        )
    )
    if nominally_complete:
        if proof_status != "ready":
            warnings.append("operational_memory_search_integrity_state")
        else:
            integrity = verify_operational_memory_search_integrity(
                conn,
                allow_full_scan=allow_integrity_scan,
            )
            if integrity.get("status") != "ready":
                warnings.append(
                    str(integrity.get("issue") or "operational_memory_search_integrity")
                )
    stalled = bool(
        configured
        and warnings
        and progress_age_seconds is not None
        and progress_age_seconds > _MEMORY_SEARCH_PROGRESS_STALL_SECONDS
    )
    if stalled:
        warnings.append(
            f"operational_memory_search_progress_stalled:{int(progress_age_seconds)}s"
        )
    ready = configured and not warnings
    return {
        "configured": configured,
        "auto_maintenance_configured": (
            _operational_memory_search_auto_maintenance_enabled()
        ),
        "available": True,
        "ready": ready,
        "status": "ready" if ready else ("backfilling" if configured else "disabled"),
        "warnings": warnings,
        "schema_version": schema_version,
        "backfill_cursor": cursor,
        "backfill_target": target,
        "canonical_documents": canonical_documents,
        "indexed_documents": indexed_documents,
        "trigram_indexed_documents": trigram_indexed_documents,
        "short_indexed_documents": short_indexed_documents,
        "pending": pending,
        "proof_status": proof_status,
        "proof_generation": proof_generation or None,
        "integrity_status": (
            str(integrity.get("status")) if integrity is not None else None
        ),
        "integrity_inspected_rows": (
            int(integrity.get("inspected_rows", 0)) if integrity is not None else None
        ),
        "integrity_inspected_bytes": (
            int(integrity.get("inspected_bytes", 0)) if integrity is not None else None
        ),
        "integrity_verification_kind": (
            str(integrity.get("verification_kind")) if integrity is not None else None
        ),
        "integrity_next_attestation_in_seconds": (
            float(integrity.get("next_attestation_in_seconds", 0.0))
            if integrity is not None
            else None
        ),
        "progress_updated_at": progress_updated_at or None,
        "progress_age_seconds": progress_age_seconds,
        "progress_stalled": stalled,
        "degraded_row_limit": _operational_memory_degraded_row_limit(),
        "retry_after_seconds": _operational_memory_retry_after_seconds(),
    }


def maintain_operational_memory_search_index(
    batch_size: int | None = None,
) -> dict:
    """Explicitly advance the derived search index without hiding writes in search."""
    initialize_meta_store()
    conn = get_connection()
    if (
        not _operational_memory_search_index_enabled()
        or not _memory_search_index_schema_available(conn)
    ):
        return {
            "available": False,
            "ready": False,
            "indexed_documents": 0,
            "pending": 0,
        }
    cursor, target = _advance_operational_memory_search_index(
        conn,
        batch_size=batch_size,
    )
    status = operational_memory_search_index_status()
    return {
        "available": True,
        "ready": bool(status["ready"]),
        "backfill_cursor": cursor,
        "backfill_target": target,
        "canonical_documents": int(status["canonical_documents"]),
        "indexed_documents": int(status["indexed_documents"]),
        "pending": int(status["pending"]),
    }


def maintain_operational_memory_search_index_budget(
    *,
    batch_size: int | None = None,
    max_batches: int = 4,
    wall_seconds: float = 2.0,
) -> dict:
    """Advance automatic maintenance within explicit work and wall bounds."""
    max_batches = max(1, min(100, int(max_batches)))
    wall_seconds = max(0.05, min(60.0, float(wall_seconds)))
    started = time.monotonic()
    batches = 0
    result = operational_memory_search_index_status()
    if (
        not result.get("configured")
        or not result.get("available")
        or result.get("ready")
    ):
        return {
            **result,
            "batches": 0,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }
    while batches < max_batches and time.monotonic() - started < wall_seconds:
        result = maintain_operational_memory_search_index(batch_size)
        batches += 1
        if result.get("ready"):
            break
    final_status = operational_memory_search_index_status()
    return {
        **final_status,
        "batches": batches,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def _memory_sql_term_filter(
    terms: list[str], alias: str = "om"
) -> tuple[str, list[str]]:
    search_text_sql = (
        f"lower(COALESCE(json_extract({alias}.data_json, '$.memory_key'), '') || ' ' || "
        f"COALESCE(json_extract({alias}.data_json, '$.text'), '') || ' ' || "
        f"COALESCE(json_extract({alias}.data_json, '$.source_page'), '') || ' ' || "
        f"COALESCE(json_extract({alias}.data_json, '$.memory_type'), ''))"
    )
    return (
        "(" + " OR ".join(f"instr({search_text_sql}, ?) > 0" for _ in terms) + ")",
        list(terms),
    )


def _indexed_operational_memory_query(
    terms: list[str],
    allowed_types: set[str] | None,
    *,
    cursor: str,
    target: str,
    include_pending: bool,
    candidate_limit: int,
) -> tuple[str, tuple[object, ...]]:
    """Build an indexed candidate union with only bounded canonical tails."""
    type_sql = ""
    type_params: list[object] = []
    if allowed_types:
        placeholders = ", ".join("?" for _ in allowed_types)
        type_sql = f" AND lower(COALESCE(om.memory_type, 'fact')) IN ({placeholders})"
        type_params = sorted(allowed_types)

    candidate_queries: list[str] = []
    candidate_params: list[object] = []
    long_terms = [term for term in terms if len(term) >= 3]
    short_terms = [term for term in terms if len(term) <= 2]
    if long_terms:
        candidate_queries.append(
            "SELECT source_key, data_json FROM ("
            "SELECT om.memory_id AS source_key, om.data_json AS data_json "
            "FROM operational_memory_search_fts "
            "JOIN operational_memory_search_docs AS docs "
            "ON docs.doc_id = operational_memory_search_fts.rowid "
            "JOIN operational_memory AS om ON om.memory_id = docs.memory_id "
            "WHERE operational_memory_search_fts MATCH ?" + type_sql + " "
            "ORDER BY bm25(operational_memory_search_fts) LIMIT ?)"
        )
        candidate_params.extend(
            (_memory_fts_expression(long_terms), *type_params, candidate_limit)
        )
    if short_terms:
        candidate_queries.append(
            "SELECT source_key, data_json FROM ("
            "SELECT om.memory_id AS source_key, om.data_json AS data_json "
            "FROM operational_memory_search_short_fts "
            "JOIN operational_memory_search_docs AS docs "
            "ON docs.doc_id = operational_memory_search_short_fts.rowid "
            "JOIN operational_memory AS om ON om.memory_id = docs.memory_id "
            "WHERE operational_memory_search_short_fts MATCH ?" + type_sql + " "
            "ORDER BY bm25(operational_memory_search_short_fts) LIMIT ?)"
        )
        candidate_params.extend(
            (_memory_fts_expression(short_terms), *type_params, candidate_limit)
        )

    residual_filter, residual_params = _memory_sql_term_filter(terms)
    if cursor < target:
        candidate_queries.append(
            "SELECT om.memory_id AS source_key, om.data_json AS data_json "
            "FROM operational_memory AS om WHERE om.memory_id > ? "
            "AND om.memory_id <= ? AND " + residual_filter + type_sql
        )
        candidate_params.extend(
            (
                cursor,
                target,
                *residual_params,
                *type_params,
            )
        )
    if include_pending:
        candidate_queries.append(
            "SELECT om.memory_id AS source_key, om.data_json AS data_json "
            "FROM operational_memory_search_pending AS pending "
            "CROSS JOIN operational_memory AS om "
            "ON om.memory_id = pending.memory_id "
            "WHERE pending.operation = 'upsert' AND " + residual_filter + type_sql
        )
        candidate_params.extend((*residual_params, *type_params))

    if not candidate_queries:
        raise ValueError("indexed operational-memory query requires at least one term")
    return (
        "SELECT data_json FROM (" + " UNION ".join(candidate_queries) + ") "
        "ORDER BY source_key",
        tuple(candidate_params),
    )


def _bounded_operational_memory_source_ids(
    conn: sqlite3.Connection,
    *,
    cursor: str | None = None,
    target: str | None = None,
    include_pending: bool = False,
) -> tuple[int, bool]:
    """Count at most one bounded degraded source window.

    This intentionally counts source rows before applying text predicates. A rare
    term must not turn an apparently bounded fallback into a canonical full scan.
    """
    limit = _operational_memory_degraded_row_limit()
    params: tuple[object, ...]
    if cursor is None or target is None:
        sql = "SELECT memory_id FROM operational_memory ORDER BY memory_id LIMIT ?"
        params = (limit + 1,)
    else:
        sql = (
            "SELECT memory_id FROM operational_memory WHERE memory_id > ? "
            "AND memory_id <= ? ORDER BY memory_id LIMIT ?"
        )
        params = (cursor, target, limit + 1)
    source_ids = {str(row[0]) for row in conn.execute(sql, params)}
    if len(source_ids) > limit:
        return limit + 1, True
    if include_pending:
        remaining = limit - len(source_ids)
        pending_rows = conn.execute(
            "SELECT memory_id FROM operational_memory_search_pending "
            "ORDER BY memory_id LIMIT ?",
            (remaining + 1,),
        )
        source_ids.update(str(row[0]) for row in pending_rows)
    return min(len(source_ids), limit + 1), len(source_ids) > limit


def _raise_if_unbounded_operational_memory_fallback(
    conn: sqlite3.Connection,
    *,
    reason: str,
    cursor: str | None = None,
    target: str | None = None,
    include_pending: bool = False,
) -> None:
    if _operational_memory_unbounded_fallback_enabled():
        return
    _observed, exceeded = _bounded_operational_memory_source_ids(
        conn,
        cursor=cursor,
        target=target,
        include_pending=include_pending,
    )
    if exceeded:
        raise OperationalMemoryNotReady(
            reason,
            retry_after_seconds=_operational_memory_retry_after_seconds(),
        )


def _indexed_operational_memory_rows(
    conn: sqlite3.Connection,
    terms: list[str],
    allowed_types: set[str] | None,
    *,
    candidate_limit: int,
):
    """Return candidates plus a ready-proof fence, or None without FTS."""
    if (
        not terms
        or not _operational_memory_search_index_enabled()
        or not _memory_search_index_schema_available(conn)
    ):
        return None

    try:
        state = conn.execute(
            "SELECT backfill_cursor, backfill_target, schema_version "
            "FROM operational_memory_search_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            log.warning(
                "Operational-memory FTS state missing; using compatibility prefilter"
            )
            return None
        cursor = str(state[0] or "")
        target = str(state[1] or "")
        schema_version = int(state[2] or 0)
        if schema_version != _MEMORY_SEARCH_INDEX_SCHEMA_VERSION:
            log.warning(
                "Operational-memory FTS schema v%s is not ready for v%s; "
                "using compatibility prefilter",
                schema_version,
                _MEMORY_SEARCH_INDEX_SCHEMA_VERSION,
            )
            return None
        include_pending = (
            conn.execute(
                "SELECT 1 FROM operational_memory_search_pending LIMIT 1"
            ).fetchone()
            is not None
        )
        if cursor >= target and not include_pending:
            # Retrieval is fenced by the durable corpus proof and the
            # operational-memory revision token. Do not COUNT every FTS
            # virtual table on each request: those scans are O(corpus) and
            # dominate latency once the memory projection reaches six figures.
            # Doctor/watchdog retain synchronous count and digest attestation.
            integrity = verify_operational_memory_search_integrity(
                conn,
                allow_full_scan=False,
                allow_durable_proof=True,
            )
            if integrity.get("status") != "ready" or not isinstance(
                integrity.get("signature"),
                tuple,
            ):
                issue = str(
                    integrity.get("issue") or "operational_memory_search_integrity"
                )
                reason = {
                    "operational_memory_search_integrity_limit": (
                        "search_index_integrity_limit"
                    ),
                    "operational_memory_search_integrity_race": (
                        "search_index_integrity_race"
                    ),
                    "operational_memory_search_integrity_state": (
                        "search_index_integrity_state"
                    ),
                }.get(issue, "search_index_integrity_mismatch")
                _raise_if_unbounded_operational_memory_fallback(
                    conn,
                    reason=reason,
                )
                log.warning(
                    "Operational-memory FTS proof failed (%s); using bounded "
                    "compatibility prefilter",
                    issue,
                )
                return None
            integrity_signature = integrity["signature"]
        else:
            integrity_signature = None
        if cursor < target or include_pending:
            _raise_if_unbounded_operational_memory_fallback(
                conn,
                reason="search_index_backfilling",
                cursor=cursor,
                target=target,
                include_pending=include_pending,
            )
            log.warning(
                "Operational-memory FTS is not ready; merging bounded backfill/pending rows"
            )
        sql, params = _indexed_operational_memory_query(
            terms,
            allowed_types,
            cursor=cursor,
            target=target,
            include_pending=include_pending,
            candidate_limit=candidate_limit,
        )
        return conn.execute(sql, params), integrity_signature
    except sqlite3.Error as exc:
        log.warning(
            "Operational-memory FTS unavailable; using compatibility prefilter: %s",
            exc,
        )
        return None


def _bounded_memory_query_terms(query: str) -> list[str]:
    text = str(query or "")
    if len(text) > _MEMORY_QUERY_CHAR_LIMIT:
        raise ValueError(
            f"operational-memory query exceeds {_MEMORY_QUERY_CHAR_LIMIT} characters"
        )
    terms = _query_terms(text)
    if any(len(term) > _MEMORY_QUERY_TERM_CHAR_LIMIT for term in terms):
        raise ValueError(
            "operational-memory query contains a term longer than "
            f"{_MEMORY_QUERY_TERM_CHAR_LIMIT} characters"
        )
    return terms[:_MEMORY_QUERY_TERM_LIMIT]


def _memory_term_matcher(terms: list[str]) -> _ExactTermMatcher | None:
    if (
        len(terms) > _MEMORY_CANDIDATE_TERM_LIMIT
        and all(len(term) > 2 for term in terms)
        and sum(map(len, terms)) <= _MEMORY_MATCHER_PATTERN_CHAR_LIMIT
    ):
        return _ExactTermMatcher(terms)
    return None


def _memory_candidate_terms(_query: str, terms: list[str]) -> list[str]:
    """Return a bounded, complete SQL prefilter term set when possible.

    Callers must use an unfiltered streaming scan when this bounded list cannot
    cover every ranking term; dropping a tail term would create false negatives.
    """
    unique: list[str] = []
    for candidate in terms:
        if candidate and candidate not in unique:
            unique.append(candidate)
        if len(unique) >= _MEMORY_CANDIDATE_TERM_LIMIT:
            break
    return unique


def _operational_memory_candidate_sql(
    terms: list[str],
    allowed_types: set[str] | None,
    *,
    broad: bool = False,
) -> tuple[str, list[object]]:
    """Build a streaming candidate query without hydrating the whole table."""
    search_text_sql = (
        "lower(COALESCE(json_extract(data_json, '$.memory_key'), '') || ' ' || "
        "COALESCE(json_extract(data_json, '$.text'), '') || ' ' || "
        "COALESCE(json_extract(data_json, '$.source_page'), '') || ' ' || "
        "COALESCE(json_extract(data_json, '$.memory_type'), ''))"
    )
    term_clauses: list[str] = []
    params: list[object] = []
    for term in terms:
        term_clauses.append(f"instr({search_text_sql}, ?) > 0")
        params.append(term)

    filters: list[str] = []
    if term_clauses:
        if broad or len(term_clauses) == 1:
            filters.append(f"({' OR '.join(term_clauses)})")
        else:
            exact_clause = term_clauses[0]
            supporting_clause = " AND ".join(term_clauses[1:])
            filters.append(f"({exact_clause} OR ({supporting_clause}))")
    if allowed_types:
        placeholders = ", ".join("?" for _ in allowed_types)
        filters.append(f"lower(COALESCE(memory_type, 'fact')) IN ({placeholders})")
        params.extend(sorted(allowed_types))

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = f"SELECT data_json FROM operational_memory {where_sql} ORDER BY memory_id ASC"
    return sql, params


def _legacy_operational_memory_views(
    query: str,
    current_top_k: int,
    history_top_k: int,
    allowed_types: set[str] | None,
    include_polluted: bool,
) -> tuple[list[dict], list[dict]]:
    """Compatibility scan with bounded result heaps for SQLite without JSON1."""
    # Caller guarantees the canonical tables already exist; this path is read-only.
    conn = get_connection()
    terms = _bounded_memory_query_terms(query)
    matcher = _memory_term_matcher(terms)
    current_heap: list[tuple] = []
    history_heap: list[tuple] = []

    def push_ranked(
        heap: list[tuple],
        limit: int,
        rank: tuple[float, float, float, int],
        memory: dict,
    ) -> None:
        if limit <= 0:
            return
        entry = (*rank, memory)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif rank > heap[0][:4]:
            heapq.heapreplace(heap, entry)

    sequence = 0
    for row in conn.execute(
        "SELECT data_json FROM operational_memory ORDER BY memory_id ASC"
    ):
        memory = _decode_operational_memory_json(row["data_json"])
        memory_type = str(memory.get("memory_type", "fact")).lower()
        if allowed_types and memory_type not in allowed_types:
            continue
        if not include_polluted and classify_non_claim_text(
            str(memory.get("text") or "")
        ):
            continue
        relevance = _memory_relevance(memory, terms, matcher=matcher)
        if relevance <= 0 and terms:
            continue
        memory_score = float(memory.get("memory_score", 0) or 0)
        score = round(relevance + (memory_score * 5), 4)
        rank = (
            score,
            memory_score,
            _dt_rank(memory.get("updated_at")),
            -sequence,
        )
        sequence += 1
        push_ranked(history_heap, history_top_k, rank, memory)
        state = str(memory.get("validity_state", "active")).lower()
        if state not in _MEMORY_HIDDEN_STATES:
            push_ranked(current_heap, current_top_k, rank, memory)

    def materialize(heap: list[tuple]) -> list[dict]:
        results: list[dict] = []
        for score, _, _, _, memory in sorted(heap, reverse=True):
            item = copy.deepcopy(memory)
            item["retrieval_score"] = score
            results.append(item)
        return results

    return materialize(current_heap), materialize(history_heap)


def search_operational_memory_views(
    query: str,
    current_top_k: int = 12,
    history_top_k: int = 12,
    memory_types: list[str] | None = None,
    include_polluted: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Return current and historical views from one streaming candidate scan."""
    current_top_k = max(0, int(current_top_k))
    history_top_k = max(0, int(history_top_k))
    if current_top_k == 0 and history_top_k == 0:
        return [], []

    if not peek_db_path().exists():
        raise OperationalMemoryNotReady("database_missing")
    conn = get_connection()
    required_tables = {"operational_memory", "claims"}
    available_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('operational_memory', 'claims')"
        )
    }
    if available_tables != required_tables:
        raise OperationalMemoryNotReady("schema_not_ready")
    if not conn.execute(
        "SELECT 1 FROM operational_memory WHERE memory_id >= '' LIMIT 1"
    ).fetchone():
        if conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
            raise OperationalMemoryNotReady("projection_empty")
        return [], []

    allowed_types = None
    if memory_types:
        allowed_types = {
            str(item).strip().lower().replace("-", "_") for item in memory_types
        }
    terms = _bounded_memory_query_terms(query)
    matcher = _memory_term_matcher(terms)
    candidate_limit = min(
        _operational_memory_degraded_row_limit(),
        max(128, (current_top_k + history_top_k) * 8),
    )
    indexed_result = _indexed_operational_memory_rows(
        conn,
        terms,
        allowed_types,
        candidate_limit=candidate_limit,
    )
    indexed_rows = indexed_result[0] if indexed_result is not None else None
    indexed_signature = indexed_result[1] if indexed_result is not None else None
    candidate_sql = None
    candidate_params: list[object] = []
    if indexed_rows is None:
        fallback_reason = (
            "search_index_unavailable"
            if _operational_memory_search_index_enabled()
            else "search_index_disabled"
        )
        _raise_if_unbounded_operational_memory_fallback(
            conn,
            reason=fallback_reason,
        )
        candidate_terms = _memory_candidate_terms(query, terms)
        prefilter_terms = candidate_terms if len(candidate_terms) == len(terms) else []
        candidate_sql, candidate_params = _operational_memory_candidate_sql(
            prefilter_terms,
            allowed_types,
            broad=True,
        )

    current_heap: list[tuple] = []
    history_heap: list[tuple] = []

    def push_ranked(
        heap: list[tuple],
        limit: int,
        rank: tuple[float, float, float, int],
        memory: dict,
    ) -> None:
        if limit <= 0:
            return
        entry = (*rank, memory)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif rank > heap[0][:4]:
            heapq.heapreplace(heap, entry)

    def materialize(heap: list[tuple]) -> list[dict]:
        results: list[dict] = []
        for score, _, _, _, memory in sorted(heap, reverse=True):
            item = copy.deepcopy(memory)
            item["retrieval_score"] = score
            results.append(item)
        return results

    try:
        sequence = 0
        rows = (
            indexed_rows
            if indexed_rows is not None
            else conn.execute(candidate_sql, tuple(candidate_params))
        )
        for row in rows:
            memory = _decode_operational_memory_json(row["data_json"])
            if not include_polluted and classify_non_claim_text(
                str(memory.get("text") or "")
            ):
                continue
            relevance = _memory_relevance(memory, terms, matcher=matcher)
            if relevance <= 0 and terms:
                continue
            memory_score = float(memory.get("memory_score", 0) or 0)
            score = round(relevance + (memory_score * 5), 4)
            rank = (
                score,
                memory_score,
                _dt_rank(memory.get("updated_at")),
                -sequence,
            )
            sequence += 1
            push_ranked(history_heap, history_top_k, rank, memory)
            state = str(memory.get("validity_state", "active")).lower()
            if state not in _MEMORY_HIDDEN_STATES:
                push_ranked(current_heap, current_top_k, rank, memory)
        if indexed_signature is not None:
            integrity_after = verify_operational_memory_search_integrity(
                conn,
                allow_full_scan=False,
                allow_durable_proof=True,
            )
            if (
                integrity_after.get("status") != "ready"
                or integrity_after.get("signature") != indexed_signature
            ):
                issue = str(
                    integrity_after.get("issue")
                    or "operational_memory_search_integrity_race"
                )
                reason = {
                    "operational_memory_search_integrity_limit": (
                        "search_index_integrity_limit"
                    ),
                    "operational_memory_search_integrity_state": (
                        "search_index_integrity_state"
                    ),
                }.get(issue, "search_index_integrity_race")
                _raise_if_unbounded_operational_memory_fallback(
                    conn,
                    reason=reason,
                )
                return _legacy_operational_memory_views(
                    query,
                    current_top_k,
                    history_top_k,
                    allowed_types,
                    include_polluted,
                )
    except sqlite3.OperationalError as exc:
        log.warning(
            "Operational-memory candidate query unavailable; using compatibility "
            "scan: %s",
            exc,
        )
        _raise_if_unbounded_operational_memory_fallback(
            conn,
            reason="search_index_query_failed",
        )
        return _legacy_operational_memory_views(
            query,
            current_top_k,
            history_top_k,
            allowed_types,
            include_polluted,
        )

    return materialize(current_heap), materialize(history_heap)


def search_operational_memory(
    query: str,
    top_k: int = 12,
    memory_types: list[str] | None = None,
    include_history: bool = False,
    include_polluted: bool = False,
) -> list[dict]:
    current, history = search_operational_memory_views(
        query,
        current_top_k=0 if include_history else top_k,
        history_top_k=top_k if include_history else 0,
        memory_types=memory_types,
        include_polluted=include_polluted,
    )
    return history if include_history else current


def remediate_operational_memory_pollution(
    dry_run: bool = True,
    limit: int = 0,
    sample_size: int = 20,
) -> dict:
    """Preview or archive known infrastructure artifacts in operational memory."""
    store = load_memory_objects()
    candidates: list[tuple[dict, str]] = []
    reason_counts: dict[str, int] = {}
    for memory in store.get("items", {}).values():
        reason = classify_non_claim_text(str(memory.get("text") or ""))
        reasons = memory.get("validity_reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        if not reason or (
            str(memory.get("validity_state") or "").lower() == "archived"
            and f"infrastructure_artifact:{reason}" in reasons
        ):
            continue
        candidates.append((memory, reason))
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    candidates.sort(key=lambda item: str(item[0].get("memory_id") or ""))
    selected = candidates[:limit] if limit > 0 else candidates
    result = {
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "reason_counts": dict(sorted(reason_counts.items())),
        "sample": [
            {
                "memory_id": memory.get("memory_id"),
                "source_page": memory.get("source_page"),
                "reason": reason,
                "text": " ".join(str(memory.get("text") or "").split())[:180],
            }
            for memory, reason in selected[: max(0, sample_size)]
        ],
    }
    if dry_run or not selected:
        return result

    now = _utc_now()
    updates = []
    for memory, reason in selected:
        archived = copy.deepcopy(memory)
        reasons = archived.get("validity_reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        marker = f"infrastructure_artifact:{reason}"
        if marker not in reasons:
            reasons.append(marker)
        archived.update(
            {
                "status": "Archived",
                "validity_state": "archived",
                "validity_reasons": reasons,
                "archived_at": now,
                "updated_at": now,
            }
        )
        updates.append(
            (
                "Archived",
                json.dumps(archived, ensure_ascii=False),
                now,
                archived.get("memory_id"),
            )
        )
    with transaction():
        get_connection().executemany(
            "UPDATE operational_memory SET status = ?, data_json = ?, updated_at = ? "
            "WHERE memory_id = ?",
            updates,
        )
    result["archived_count"] = len(updates)
    return result


def build_claim_graph_projection(
    limit_nodes: int | None = None,
    *,
    connection=None,
) -> dict:
    max_degree = 12
    entity_window = 6
    source_window = 4
    if limit_nodes is None:
        limit_nodes = (
            2500  # Hard cap to prevent 3D-force-graph from freezing the browser
        )
    from vector_lake import governance_metrics

    conn = connection or get_connection()
    claim_rows = conn.execute(
        "SELECT data_json FROM claims ORDER BY "
        "COALESCE(json_extract(data_json, '$.updated_at'), "
        "json_extract(data_json, '$.created_at'), "
        "json_extract(data_json, '$.temporal_anchor'), '') DESC, "
        "claim_id ASC LIMIT ?",
        (max(1, int(limit_nodes)),),
    ).fetchall()
    claims = [
        governance_metrics.annotate_claim_validity(json.loads(row["data_json"]))
        for row in claim_rows
    ]
    referenced_entity_ids = {
        str(entity_id)
        for claim in claims
        for entity_id in claim.get("subject_entity_ids", [])
    }
    referenced_source_ids = {
        str(source_id) for claim in claims for source_id in claim.get("source_ids", [])
    }

    def load_referenced(
        table_name: str, id_column: str, ids: set[str]
    ) -> dict[str, dict]:
        records = {}
        ordered = sorted(ids)
        for offset in range(0, len(ordered), 500):
            batch = ordered[offset : offset + 500]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT {id_column}, data_json FROM {table_name} "
                f"WHERE {id_column} IN ({placeholders})",
                tuple(batch),
            ).fetchall()
            for row in rows:
                records[str(row[id_column])] = json.loads(row["data_json"])
        return records

    entities = load_referenced("entities", "entity_id", referenced_entity_ids)
    sources = load_referenced("sources", "source_id", referenced_source_ids)

    nodes = []
    claim_ids = {claim["claim_id"] for claim in claims}
    node_lookup = {}
    degree_map = {}

    for claim in claims:
        subject_names = [
            entities[entity_id]["canonical_name"]
            for entity_id in claim.get("subject_entity_ids", [])
            if entity_id in entities
        ]
        source_pages = [
            sources[source_id]["canonical_source_page"]
            for source_id in claim.get("source_ids", [])
            if source_id in sources
        ]
        compact_text = _compact_claim_text(claim.get("claim_text", ""))
        nodes.append(
            {
                "id": claim["claim_id"],
                "name": claim.get("claim_text", "")[:96] or claim["claim_id"],
                "group": "Claim",
                "validity_state": claim.get("validity_state", "unknown"),
                "claim_type": claim.get("claim_type", "claim"),
                "confidence": claim.get("confidence"),
                "summary": compact_text,
                "subject_entities": subject_names,
                "source_pages": source_pages,
                "degree": 0,
                "updated": claim.get("updated_at", ""),
            }
        )
        node_lookup[claim["claim_id"]] = nodes[-1]
        degree_map[claim["claim_id"]] = 0

    edge_records = {}

    def _record_edge(
        left_id: str, right_id: str, relation: str, weight: float, force: bool = False
    ):
        if left_id == right_id or left_id not in claim_ids or right_id not in claim_ids:
            return
        source_id, target_id = sorted((left_id, right_id))
        edge_key = (source_id, target_id)

        existing = edge_records.get(edge_key)
        if existing:
            if weight > existing["weight"]:
                existing["weight"] = round(weight, 3)
                existing["relation"] = relation
            return

        if not force and (
            degree_map[source_id] >= max_degree or degree_map[target_id] >= max_degree
        ):
            return

        edge_records[edge_key] = {
            "source": source_id,
            "target": target_id,
            "weight": round(weight, 3),
            "relation": relation,
        }
        degree_map[source_id] += 1
        degree_map[target_id] += 1

    contradiction_pairs = set()
    entity_buckets = {}
    source_buckets = {}

    for claim in claims:
        claim_id = claim["claim_id"]
        for right_id in claim.get("contradicts", []):
            if right_id in claim_ids:
                contradiction_pairs.add(tuple(sorted((claim_id, right_id))))
        for entity_id in claim.get("subject_entity_ids", []):
            entity_buckets.setdefault(entity_id, []).append(claim_id)
        for source_id in claim.get("source_ids", []):
            source_buckets.setdefault(source_id, []).append(claim_id)

    for source_id, target_id in sorted(contradiction_pairs):
        _record_edge(source_id, target_id, "contradiction", 4.0, force=True)

    entity_pair_counts = {}
    for claim_ids_for_entity in entity_buckets.values():
        ordered_ids = sorted(set(claim_ids_for_entity))
        for index, left_id in enumerate(ordered_ids):
            for right_id in ordered_ids[index + 1 : index + 1 + entity_window]:
                edge_key = tuple(sorted((left_id, right_id)))
                entity_pair_counts[edge_key] = entity_pair_counts.get(edge_key, 0) + 1

    for (source_id, target_id), shared_count in sorted(
        entity_pair_counts.items(), key=lambda item: (-item[1], item[0])
    ):
        weight = 2.5 + min(shared_count, 3) * 0.5
        _record_edge(source_id, target_id, "shared-entity", weight)

    source_pair_counts = {}
    for claim_ids_for_source in source_buckets.values():
        ordered_ids = sorted(set(claim_ids_for_source))
        for index, left_id in enumerate(ordered_ids):
            for right_id in ordered_ids[index + 1 : index + 1 + source_window]:
                edge_key = tuple(sorted((left_id, right_id)))
                source_pair_counts[edge_key] = source_pair_counts.get(edge_key, 0) + 1

    for (source_id, target_id), shared_count in sorted(
        source_pair_counts.items(), key=lambda item: (-item[1], item[0])
    ):
        if (source_id, target_id) in edge_records:
            continue
        weight = 1.5 + min(shared_count, 3) * 0.5
        _record_edge(source_id, target_id, "shared-source", weight)

    edges = sorted(
        edge_records.values(),
        key=lambda edge: (-edge["weight"], edge["source"], edge["target"]),
    )
    for claim_id, degree in degree_map.items():
        if claim_id in node_lookup:
            node_lookup[claim_id]["degree"] = degree
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "nodes": nodes,
        "edges": edges,
    }


def create_merge_suggestions(limit: int = 20, enqueue: bool = True) -> dict:
    from vector_lake import governance_metrics

    report = governance_metrics.find_merge_candidate_report(
        limit=limit,
        run_preflight=True,
        decision="merge" if enqueue else None,
    )
    suggestions = report["suggestions"]
    eligible = [
        suggestion
        for suggestion in suggestions
        if suggestion.get("decision") == "merge"
        and suggestion.get("preflight_state") == "passed"
        and suggestion.get("left_version")
        and suggestion.get("right_version")
        and suggestion.get("left_projection_hash")
        and suggestion.get("right_projection_hash")
    ]
    result = {
        "created": 0,
        "candidate_pool_size": report["candidate_pool_size"],
        "actionable_pool_size": report["actionable_pool_size"],
        "decision_counts": report["decision_counts"],
        "selected_decision_counts": report["selected_decision_counts"],
        "returned_count": report["returned_count"],
        "eligible_count": len(eligible),
        "skipped_count": len(suggestions) - len(eligible),
        "suggestions": suggestions,
    }
    if not enqueue:
        return result

    created = 0
    for suggestion in eligible:
        item = {
            "item_id": f"gov_{uuid.uuid4().hex[:12]}",
            "type": "merge",
            "title": f"Merge candidate: {suggestion['left_name']} <> {suggestion['right_name']}",
            "description": "; ".join(suggestion["reasons"]),
            "created_at": _utc_now(),
            "status": "pending",
            "source": "merge-suggestions",
            "pair_key": suggestion["pair_key"],
            "affected_ids": [
                suggestion["left_entity_id"],
                suggestion["right_entity_id"],
            ],
            "search_queries": [suggestion["left_name"], suggestion["right_name"]],
            "affected_pages": [
                f"{suggestion['left_page_key']}.md",
                f"{suggestion['right_page_key']}.md",
            ],
            "merge_candidate": suggestion,
        }
        if insert_governance_item_if_absent(item, ("pair_key",)):
            created += 1
    result["created"] = created
    return result


def _canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload is not canonical JSON: {exc}"
        ) from exc
    return text.encode("utf-8")


def _canonical_change_set_payload(change_set: dict) -> tuple[dict, bytes]:
    affected_pages = change_set.get("affected_pages") or []
    if not isinstance(affected_pages, list) or any(
        not isinstance(page, str) or not page for page in affected_pages
    ):
        raise ChangeSetPayloadCorrupt(
            "Change-set affected_pages must be a list of non-empty strings"
        )
    payload: dict[str, object] = {
        "delta_kind": _CHANGE_SET_DELTA_KIND,
        "affected_pages": list(affected_pages),
    }
    for section in _CHANGE_SET_PAYLOAD_SECTIONS:
        records = change_set.get(section) or []
        if not isinstance(records, list) or any(
            not isinstance(record, dict) for record in records
        ):
            raise ChangeSetPayloadCorrupt(
                f"Change-set {section} must be a list of objects"
            )
        payload[section] = records
    return payload, _canonical_json_bytes(payload)


def _change_set_payload_digest(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


def _normalized_change_set_status(value: object) -> str:
    status = str(value or "pending").strip().casefold()
    allowed = {"pending", *_CHANGE_SET_TERMINAL_STATUSES}
    if status not in allowed:
        raise ChangeSetPayloadCorrupt(
            f"Unsupported change-set status: {status or '<missing>'}"
        )
    return status


def _strict_utc_instant(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _terminal_time_for_change_set(
    change_set: dict,
    status: str,
    *,
    default_terminal_at: str | None = None,
) -> tuple[str | None, str]:
    created_at = _strict_utc_instant(change_set.get("created_at"))
    if status not in _CHANGE_SET_TERMINAL_STATUSES:
        return None, "active"
    field_name = {
        "applied": "applied_at",
        "cancelled": "cancelled_at",
        "failed": "failed_at",
        "published": "published_at",
        "rejected": "rejected_at",
        "superseded": "superseded_at",
    }[status]
    terminal_at = _strict_utc_instant(change_set.get(field_name))
    if terminal_at is not None:
        return terminal_at, field_name
    terminal_at = _strict_utc_instant(change_set.get("terminal_at"))
    if terminal_at is not None:
        return terminal_at, "terminal_at"
    if change_set.get("requires_human_review") is False and created_at is not None:
        return created_at, "created_terminal"
    fallback = _strict_utc_instant(default_terminal_at)
    if fallback is not None:
        return fallback, "persisted_terminal"
    return None, "unknown"


def _change_set_manifest(
    change_set: dict,
    payload_bytes: bytes,
    *,
    payload_available: bool,
    terminal_at: str | None = None,
) -> dict:
    payload, canonical_payload = _canonical_change_set_payload(change_set)
    if canonical_payload != payload_bytes:
        raise ChangeSetPayloadCorrupt(
            "Change-set payload changed while its manifest was being built"
        )
    status = _normalized_change_set_status(change_set.get("status"))
    affected_id_values = change_set.get("affected_ids") or []
    if not isinstance(affected_id_values, list):
        raise ChangeSetPayloadCorrupt("Legacy affected_ids must be a list")
    affected_ids = sorted({str(value) for value in affected_id_values if value})
    stored_bytes = len(zlib.compress(payload_bytes)) if payload_available else 0
    record_counts = {
        section: len(payload[section]) for section in _CHANGE_SET_PAYLOAD_SECTIONS
    }
    manifest_keys = (
        "change_set_id",
        "idempotency_key",
        "origin",
        "created_at",
        "summary",
        "risk_level",
        "requires_human_review",
        "write_contract",
        "published_at",
        "applied_at",
        "cancelled_at",
        "failed_at",
        "rejected_at",
        "superseded_at",
        "operational_memory_count",
    )
    manifest = {
        key: copy.deepcopy(change_set[key])
        for key in manifest_keys
        if key in change_set
    }
    affected_pages = list(payload["affected_pages"])
    manifest.update(
        {
            "manifest_version": _CHANGE_SET_MANIFEST_VERSION,
            "delta_kind": _CHANGE_SET_DELTA_KIND,
            "status": status,
            "terminal_at": terminal_at,
            "affected_pages": affected_pages[:_CHANGE_SET_PAGE_PREVIEW_LIMIT],
            "affected_page_count": len(affected_pages),
            "affected_pages_sha256": hashlib.sha256(
                _canonical_json_bytes(affected_pages)
            ).hexdigest(),
            "affected_ids": affected_ids[:_CHANGE_SET_ID_PREVIEW_LIMIT],
            "affected_id_count": len(affected_ids),
            "affected_ids_sha256": hashlib.sha256(
                "\n".join(affected_ids).encode("utf-8")
            ).hexdigest(),
            "payload": {
                "sha256": _change_set_payload_digest(payload_bytes),
                "codec": _CHANGE_SET_PAYLOAD_CODEC,
                "raw_bytes": len(payload_bytes),
                "stored_bytes": stored_bytes,
                "record_counts": record_counts,
                "available": bool(payload_available),
            },
        }
    )
    manifest_bytes = _canonical_json_bytes(manifest)
    if len(manifest_bytes) > _CHANGE_SET_MAX_MANIFEST_BYTES:
        raise ChangeSetPayloadTooLarge(
            "Change-set manifest exceeds hard limit: "
            f"{len(manifest_bytes)} > {_CHANGE_SET_MAX_MANIFEST_BYTES} bytes"
        )
    return manifest


def _legacy_terminal_change_set_manifest(
    change_set: dict,
    *,
    raw_sha256: str,
    raw_bytes: int,
    terminal_at: str | None,
) -> dict:
    """Detach an already-applied legacy snapshot without rebuilding its payload."""
    status = _normalized_change_set_status(change_set.get("status"))
    if status not in _CHANGE_SET_TERMINAL_STATUSES:
        raise ChangeSetPayloadCorrupt(
            "Legacy detached manifests require terminal status"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise ChangeSetPayloadCorrupt("Legacy detached manifest digest is invalid")
    if raw_bytes < 0 or raw_bytes > _CHANGE_SET_COMPACTION_MAX_INPUT_BYTES:
        raise ChangeSetPayloadTooLarge("Legacy detached snapshot exceeds hard limit")
    affected_pages = change_set.get("affected_pages") or []
    if not isinstance(affected_pages, list) or any(
        not isinstance(page, str) or not page for page in affected_pages
    ):
        raise ChangeSetPayloadCorrupt("Legacy affected_pages are malformed")
    affected_id_values = change_set.get("affected_ids") or []
    if not isinstance(affected_id_values, list) or any(
        not isinstance(value, str) or not value for value in affected_id_values
    ):
        raise ChangeSetPayloadCorrupt(
            "Change-set affected_ids must be a list of non-empty strings"
        )
    affected_ids = sorted(set(affected_id_values))
    affected_ids_digest = hashlib.sha256()
    for index, affected_id in enumerate(affected_ids):
        if index:
            affected_ids_digest.update(b"\n")
        affected_ids_digest.update(affected_id.encode("utf-8"))
    record_counts = {}
    for section in _CHANGE_SET_PAYLOAD_SECTIONS:
        records = change_set.get(section) or []
        if not isinstance(records, list) or any(
            not isinstance(record, dict) for record in records
        ):
            raise ChangeSetPayloadCorrupt(f"Legacy change-set {section} must be a list")
        record_counts[section] = len(records)
    manifest_keys = (
        "change_set_id",
        "idempotency_key",
        "origin",
        "created_at",
        "summary",
        "risk_level",
        "requires_human_review",
        "write_contract",
        "published_at",
        "applied_at",
        "cancelled_at",
        "failed_at",
        "rejected_at",
        "superseded_at",
        "operational_memory_count",
    )
    manifest = {
        key: copy.deepcopy(change_set[key])
        for key in manifest_keys
        if key in change_set
    }
    manifest.update(
        {
            "manifest_version": _CHANGE_SET_MANIFEST_VERSION,
            "delta_kind": _CHANGE_SET_DELTA_KIND,
            "status": status,
            "terminal_at": terminal_at,
            "affected_pages": affected_pages[:_CHANGE_SET_PAGE_PREVIEW_LIMIT],
            "affected_page_count": len(affected_pages),
            "affected_pages_sha256": hashlib.sha256(
                _canonical_json_bytes(affected_pages)
            ).hexdigest(),
            "affected_ids": affected_ids[:_CHANGE_SET_ID_PREVIEW_LIMIT],
            "affected_id_count": len(affected_ids),
            "affected_ids_sha256": affected_ids_digest.hexdigest(),
            "payload": {
                "sha256": raw_sha256,
                "codec": _CHANGE_SET_DETACHED_LEGACY_CODEC,
                "raw_bytes": int(raw_bytes),
                "stored_bytes": 0,
                "record_counts": record_counts,
                "available": False,
            },
        }
    )
    manifest_bytes = _canonical_json_bytes(manifest)
    if len(manifest_bytes) > _CHANGE_SET_MAX_MANIFEST_BYTES:
        raise ChangeSetPayloadTooLarge(
            "Legacy detached change-set manifest exceeds hard limit: "
            f"{len(manifest_bytes)} > {_CHANGE_SET_MAX_MANIFEST_BYTES} bytes"
        )
    return manifest


def _validate_change_set_batch_limits(
    change_sets: list[dict],
) -> list[tuple[dict, bytes]]:
    if len(change_sets) > _CHANGE_SET_MAX_BATCH_ITEMS:
        raise ChangeSetBatchTooLarge(
            "Change-set batch item count exceeds hard limit: "
            f"{len(change_sets)} > {_CHANGE_SET_MAX_BATCH_ITEMS}"
        )
    prepared: list[tuple[dict, bytes]] = []
    aggregate = 0
    aggregate_affected_ids = 0
    batch_pages: dict[str, str] = {}
    for index, change_set in enumerate(change_sets):
        if not isinstance(change_set, dict):
            raise ChangeSetPayloadCorrupt("Every change set must be an object")
        pages = change_set.get("affected_pages") or []
        if len(pages) > _CHANGE_SET_MAX_PAGES:
            raise ChangeSetPayloadTooLarge(
                "Change-set page count exceeds hard limit: "
                f"{len(pages)} > {_CHANGE_SET_MAX_PAGES}"
            )
        local_pages: set[str] = set()
        for page in pages:
            normalized_page = unicodedata.normalize(
                "NFKC",
                os.path.basename(str(page)).strip(),
            ).casefold()
            if not normalized_page:
                raise ChangeSetPayloadCorrupt(
                    "Change-set affected_pages contains an empty normalized page"
                )
            if normalized_page in local_pages:
                raise ChangeSetBatchTooLarge(
                    f"Change-set contains a duplicate affected page: {page}"
                )
            local_pages.add(normalized_page)
            prior = batch_pages.get(normalized_page)
            if prior is not None:
                raise ChangeSetBatchTooLarge(
                    "Atomic change-set batch contains overlapping pages: "
                    f"{prior!r} and {page!r}"
                )
            batch_pages[normalized_page] = str(page)
        if len(batch_pages) > _CHANGE_SET_MAX_PAGES:
            raise ChangeSetBatchTooLarge(
                "Change-set batch page count exceeds hard limit: "
                f"{len(batch_pages)} > {_CHANGE_SET_MAX_PAGES} "
                f"after item {index + 1}"
            )
        affected_ids = change_set.get("affected_ids") or []
        if not isinstance(affected_ids, list) or any(
            not isinstance(value, str) or not value for value in affected_ids
        ):
            raise ChangeSetPayloadCorrupt(
                "Change-set affected_ids must be a list of non-empty strings"
            )
        if len(affected_ids) > _CHANGE_SET_MAX_AFFECTED_IDS:
            raise ChangeSetPayloadTooLarge(
                "Change-set affected_ids exceeds hard limit: "
                f"{len(affected_ids)} > {_CHANGE_SET_MAX_AFFECTED_IDS}"
            )
        aggregate_affected_ids += len(affected_ids)
        if aggregate_affected_ids > _CHANGE_SET_BATCH_MAX_AFFECTED_IDS:
            raise ChangeSetBatchTooLarge(
                "Change-set batch affected_ids exceeds hard limit: "
                f"{aggregate_affected_ids} > {_CHANGE_SET_BATCH_MAX_AFFECTED_IDS}"
            )
        _payload, payload_bytes = _canonical_change_set_payload(change_set)
        observed = len(payload_bytes)
        if observed > _CHANGE_SET_MAX_PAYLOAD_BYTES:
            raise ChangeSetPayloadTooLarge(
                "Change-set payload exceeds hard limit: "
                f"{observed} > {_CHANGE_SET_MAX_PAYLOAD_BYTES} bytes"
            )
        aggregate += observed
        if aggregate > _CHANGE_SET_BATCH_MAX_PAYLOAD_BYTES:
            raise ChangeSetBatchTooLarge(
                "Change-set batch payload exceeds hard limit: "
                f"{aggregate} > {_CHANGE_SET_BATCH_MAX_PAYLOAD_BYTES} bytes"
            )
        status = _normalized_change_set_status(change_set.get("status"))
        terminal_at, _source = _terminal_time_for_change_set(change_set, status)
        _change_set_manifest(
            change_set,
            payload_bytes,
            payload_available=status not in _CHANGE_SET_TERMINAL_STATUSES,
            terminal_at=terminal_at,
        )
        prepared.append((change_set, payload_bytes))
    return prepared


def _validate_loaded_change_set_manifest(
    manifest: dict,
    *,
    change_set_id: str,
    lifecycle_status: str,
    lifecycle_terminal_at: object,
) -> None:
    """Fail closed on malformed bounded manifests without hydrating payloads."""
    if not isinstance(manifest, dict) or not manifest:
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set manifest is malformed: {change_set_id}"
        )
    if (
        manifest.get("manifest_version") != _CHANGE_SET_MANIFEST_VERSION
        or str(manifest.get("change_set_id") or "") != change_set_id
        or manifest.get("delta_kind") != _CHANGE_SET_DELTA_KIND
    ):
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set manifest identity is invalid: {change_set_id}"
        )
    status = _normalized_change_set_status(manifest.get("status"))
    if status != _normalized_change_set_status(lifecycle_status):
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set manifest lifecycle drifted: {change_set_id}"
        )
    pages = manifest.get("affected_pages")
    page_count = manifest.get("affected_page_count")
    page_digest = str(manifest.get("affected_pages_sha256") or "")
    if (
        not isinstance(pages, list)
        or any(not isinstance(page, str) or not page for page in pages)
        or len(pages) > _CHANGE_SET_PAGE_PREVIEW_LIMIT
        or not isinstance(page_count, int)
        or page_count < len(pages)
        or not re.fullmatch(r"[0-9a-f]{64}", page_digest)
    ):
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set page summary is invalid: {change_set_id}"
        )
    if page_count == len(pages) and not hmac.compare_digest(
        page_digest,
        hashlib.sha256(_canonical_json_bytes(pages)).hexdigest(),
    ):
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set page digest is invalid: {change_set_id}"
        )
    descriptor = manifest.get("payload")
    if not isinstance(descriptor, dict):
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set payload descriptor is missing: {change_set_id}"
        )
    try:
        raw_bytes = int(descriptor.get("raw_bytes"))
        stored_bytes = int(descriptor.get("stored_bytes"))
    except (TypeError, ValueError) as exc:
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set payload sizes are invalid: {change_set_id}"
        ) from exc
    available = descriptor.get("available")
    record_counts = descriptor.get("record_counts")
    codec = descriptor.get("codec")
    max_descriptor_raw_bytes = (
        _CHANGE_SET_COMPACTION_MAX_INPUT_BYTES
        if available is False and codec == _CHANGE_SET_DETACHED_LEGACY_CODEC
        else _CHANGE_SET_MAX_PAYLOAD_BYTES
    )
    if (
        codec
        not in {
            _CHANGE_SET_PAYLOAD_CODEC,
            _CHANGE_SET_DETACHED_LEGACY_CODEC,
        }
        or (available is True and codec != _CHANGE_SET_PAYLOAD_CODEC)
        or not re.fullmatch(r"[0-9a-f]{64}", str(descriptor.get("sha256") or ""))
        or raw_bytes < 0
        or raw_bytes > max_descriptor_raw_bytes
        or stored_bytes < 0
        or stored_bytes > _CHANGE_SET_MAX_STORED_BYTES
        or (available is True and stored_bytes == 0)
        or (codec == _CHANGE_SET_DETACHED_LEGACY_CODEC and stored_bytes != 0)
        or not isinstance(record_counts, dict)
        or set(record_counts) != set(_CHANGE_SET_PAYLOAD_SECTIONS)
        or any(
            not isinstance(value, int) or value < 0 for value in record_counts.values()
        )
        or not isinstance(available, bool)
        or (status == "pending") is not available
    ):
        raise ChangeSetPayloadCorrupt(
            f"Stored change-set payload descriptor is invalid: {change_set_id}"
        )
    terminal_at = manifest.get("terminal_at")
    if status in _CHANGE_SET_TERMINAL_STATUSES:
        if terminal_at != lifecycle_terminal_at:
            raise ChangeSetPayloadCorrupt(
                f"Stored change-set terminal time drifted: {change_set_id}"
            )
    elif terminal_at is not None or lifecycle_terminal_at is not None:
        raise ChangeSetPayloadCorrupt(
            f"Pending change-set has a terminal time: {change_set_id}"
        )


def _store_change_set_payload(
    conn: sqlite3.Connection,
    payload_bytes: bytes,
    *,
    created_at: str | None = None,
) -> str:
    if len(payload_bytes) > _CHANGE_SET_MAX_PAYLOAD_BYTES:
        raise ChangeSetPayloadTooLarge(
            "Change-set payload exceeds hard limit: "
            f"{len(payload_bytes)} > {_CHANGE_SET_MAX_PAYLOAD_BYTES} bytes"
        )
    payload_sha256 = _change_set_payload_digest(payload_bytes)
    compressed = zlib.compress(payload_bytes)
    if len(compressed) > _CHANGE_SET_MAX_STORED_BYTES:
        raise ChangeSetPayloadTooLarge(
            "Compressed change-set payload exceeds hard input limit: "
            f"{len(compressed)} > {_CHANGE_SET_MAX_STORED_BYTES} bytes"
        )
    conn.execute(
        "INSERT OR IGNORE INTO change_set_payloads "
        "(payload_sha256, codec, payload_blob, raw_bytes, stored_bytes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            payload_sha256,
            _CHANGE_SET_PAYLOAD_CODEC,
            compressed,
            len(payload_bytes),
            len(compressed),
            created_at or _utc_now(),
        ),
    )
    row = conn.execute(
        "SELECT codec, payload_blob, raw_bytes, stored_bytes "
        "FROM change_set_payloads WHERE payload_sha256 = ?",
        (payload_sha256,),
    ).fetchone()
    if row is None or (
        str(row["codec"]) != _CHANGE_SET_PAYLOAD_CODEC
        or bytes(row["payload_blob"]) != compressed
        or int(row["raw_bytes"]) != len(payload_bytes)
        or int(row["stored_bytes"]) != len(compressed)
    ):
        raise ChangeSetPayloadCorrupt(
            f"Content-addressed payload collision or corruption: {payload_sha256}"
        )
    return payload_sha256


def _bounded_decompress_change_set_payload(
    compressed: bytes,
    expected_raw_bytes: int,
) -> bytes:
    if expected_raw_bytes < 0 or expected_raw_bytes > _CHANGE_SET_MAX_PAYLOAD_BYTES:
        raise ChangeSetPayloadCorrupt(
            f"Invalid change-set raw byte declaration: {expected_raw_bytes}"
        )
    decompressor = zlib.decompressobj()
    try:
        payload = decompressor.decompress(compressed, expected_raw_bytes + 1)
    except zlib.error as exc:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload decompression failed: {exc}"
        ) from exc
    if (
        len(payload) != expected_raw_bytes
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise ChangeSetPayloadCorrupt(
            "Change-set payload decompressed size or stream boundary is invalid"
        )
    return payload


def _load_change_set_payload(
    conn: sqlite3.Connection,
    manifest: dict,
) -> dict:
    descriptor = manifest.get("payload")
    if not isinstance(descriptor, dict) or descriptor.get("available") is not True:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload is unavailable: {manifest.get('change_set_id')}"
        )
    payload_sha256 = str(descriptor.get("sha256") or "")
    reference = conn.execute(
        "SELECT payload_sha256 FROM change_set_payload_refs WHERE change_set_id = ?",
        (manifest.get("change_set_id"),),
    ).fetchone()
    if reference is None or str(reference["payload_sha256"]) != payload_sha256:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload reference is missing or mismatched: "
            f"{manifest.get('change_set_id')}"
        )
    row = conn.execute(
        "SELECT codec, raw_bytes, stored_bytes, length(payload_blob) AS blob_bytes "
        "FROM change_set_payloads WHERE payload_sha256 = ?",
        (payload_sha256,),
    ).fetchone()
    if row is None:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload blob is missing: {payload_sha256}"
        )
    raw_bytes = int(row["raw_bytes"])
    stored_bytes = int(row["stored_bytes"])
    blob_bytes = int(row["blob_bytes"])
    if (
        str(row["codec"]) != _CHANGE_SET_PAYLOAD_CODEC
        or str(descriptor.get("codec")) != _CHANGE_SET_PAYLOAD_CODEC
        or raw_bytes < 0
        or raw_bytes > _CHANGE_SET_MAX_PAYLOAD_BYTES
        or raw_bytes != int(descriptor.get("raw_bytes") or -1)
        or stored_bytes < 0
        or stored_bytes > _CHANGE_SET_MAX_STORED_BYTES
        or blob_bytes != stored_bytes
        or stored_bytes != int(descriptor.get("stored_bytes") or -1)
    ):
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload metadata is inconsistent: {payload_sha256}"
        )
    blob_row = conn.execute(
        "SELECT payload_blob FROM change_set_payloads "
        "WHERE payload_sha256 = ? AND raw_bytes = ? AND stored_bytes = ? "
        "AND length(payload_blob) = stored_bytes",
        (payload_sha256, raw_bytes, stored_bytes),
    ).fetchone()
    if blob_row is None:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload changed during bounded load: {payload_sha256}"
        )
    compressed = bytes(blob_row["payload_blob"])
    if len(compressed) != stored_bytes:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload physical length drifted: {payload_sha256}"
        )
    payload_bytes = _bounded_decompress_change_set_payload(compressed, raw_bytes)
    if not hmac.compare_digest(
        _change_set_payload_digest(payload_bytes),
        payload_sha256,
    ):
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload digest mismatch: {payload_sha256}"
        )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload JSON is invalid: {payload_sha256}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("delta_kind") != _CHANGE_SET_DELTA_KIND
    ):
        raise ChangeSetPayloadCorrupt(
            f"Change-set payload delta contract is invalid: {payload_sha256}"
        )
    for section in _CHANGE_SET_PAYLOAD_SECTIONS:
        records = payload.get(section)
        if not isinstance(records, list) or any(
            not isinstance(record, dict) for record in records
        ):
            raise ChangeSetPayloadCorrupt(
                f"Change-set payload section is invalid: {section}"
            )
    affected_pages = payload.get("affected_pages")
    if (
        not isinstance(affected_pages, list)
        or manifest.get("affected_pages")
        != affected_pages[:_CHANGE_SET_PAGE_PREVIEW_LIMIT]
        or manifest.get("affected_page_count") != len(affected_pages)
        or not hmac.compare_digest(
            str(manifest.get("affected_pages_sha256") or ""),
            hashlib.sha256(_canonical_json_bytes(affected_pages)).hexdigest(),
        )
    ):
        raise ChangeSetPayloadCorrupt(
            "Change-set payload affected_pages do not match the manifest summary"
        )
    return payload


def _hydrate_change_set(
    manifest: dict,
    *,
    connection: sqlite3.Connection | None = None,
    require_payload: bool = True,
) -> dict:
    version = manifest.get("manifest_version")
    if version in {None, 1}:
        return copy.deepcopy(manifest)
    if version != _CHANGE_SET_MANIFEST_VERSION:
        raise ChangeSetPayloadCorrupt(
            f"Unsupported change-set manifest version: {version}"
        )
    descriptor = manifest.get("payload")
    available = isinstance(descriptor, dict) and descriptor.get("available") is True
    if not available:
        if require_payload:
            raise ChangeSetPayloadCorrupt(
                f"Terminal change-set payload is detached: "
                f"{manifest.get('change_set_id')}"
            )
        return copy.deepcopy(manifest)
    payload = _load_change_set_payload(connection or get_connection(), manifest)
    hydrated = copy.deepcopy(manifest)
    for section in _CHANGE_SET_PAYLOAD_SECTIONS:
        hydrated[section] = payload[section]
    hydrated["affected_pages"] = payload["affected_pages"]
    return hydrated


def _delete_unreferenced_change_set_payloads(
    conn: sqlite3.Connection,
    payload_hashes: set[str] | None = None,
) -> tuple[int, int]:
    deleted_rows = 0
    deleted_bytes = 0
    if payload_hashes is None:
        rows = conn.execute(
            "SELECT payload_sha256, stored_bytes FROM change_set_payloads AS payload "
            "WHERE NOT EXISTS (SELECT 1 FROM change_set_payload_refs AS ref "
            "WHERE ref.payload_sha256 = payload.payload_sha256)"
        ).fetchall()
    else:
        rows = []
        for payload_hash in sorted(payload_hashes):
            row = conn.execute(
                "SELECT payload_sha256, stored_bytes FROM change_set_payloads "
                "WHERE payload_sha256 = ? AND NOT EXISTS ("
                "SELECT 1 FROM change_set_payload_refs "
                "WHERE payload_sha256 = change_set_payloads.payload_sha256)",
                (payload_hash,),
            ).fetchone()
            if row is not None:
                rows.append(row)
    for row in rows:
        cursor = conn.execute(
            "DELETE FROM change_set_payloads WHERE payload_sha256 = ? "
            "AND NOT EXISTS (SELECT 1 FROM change_set_payload_refs "
            "WHERE payload_sha256 = change_set_payloads.payload_sha256)",
            (row["payload_sha256"],),
        )
        if cursor.rowcount:
            deleted_rows += 1
            deleted_bytes += int(row["stored_bytes"] or 0)
    return deleted_rows, deleted_bytes


def _persist_prepared_change_set(
    conn: sqlite3.Connection,
    change_set: dict,
    now: str,
    *,
    payload_bytes: bytes | None = None,
) -> bool:
    if payload_bytes is None:
        _payload, payload_bytes = _canonical_change_set_payload(change_set)
    payload_sha256 = _change_set_payload_digest(payload_bytes)
    change_set_id = str(change_set.get("change_set_id") or "")
    idempotency_key = str(change_set.get("idempotency_key") or change_set_id)
    if not change_set_id or not idempotency_key:
        raise ChangeSetPayloadCorrupt(
            "Change sets require non-empty change_set_id and idempotency_key"
        )
    status = _normalized_change_set_status(change_set.get("status"))
    if status != "pending":
        raise ChangeSetPayloadCorrupt(
            "Prepared change-set persistence accepts pending deltas only; "
            "terminal state requires the apply-and-terminalize transaction"
        )
    existing = conn.execute(
        "SELECT change_sets.change_set_id, change_sets.data_json, lifecycle.status "
        "FROM change_set_idempotency JOIN change_sets "
        "ON change_sets.change_set_id = change_set_idempotency.change_set_id "
        "JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "WHERE change_set_idempotency.idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        try:
            existing_manifest = json.loads(existing["data_json"])
        except (TypeError, ValueError) as exc:
            raise ChangeSetIdempotencyConflict(
                f"Existing idempotency owner is malformed: {idempotency_key}"
            ) from exc
        existing_descriptor = existing_manifest.get("payload") or {}
        existing_payload_sha256 = str(existing_descriptor.get("sha256") or "")
        if not existing_payload_sha256 and existing_manifest.get(
            "manifest_version"
        ) in {
            None,
            1,
        }:
            _existing_payload, existing_bytes = _canonical_change_set_payload(
                existing_manifest
            )
            existing_payload_sha256 = _change_set_payload_digest(existing_bytes)
        if existing_payload_sha256 != payload_sha256:
            raise ChangeSetIdempotencyConflict(
                "Idempotency key is already owned by a different payload: "
                f"{idempotency_key}"
            )
        existing_status = _normalized_change_set_status(existing["status"])
        manifest_status = _normalized_change_set_status(existing_manifest.get("status"))
        if manifest_status != existing_status:
            raise ChangeSetPayloadCorrupt(
                f"Existing idempotency lifecycle drifted: {idempotency_key}"
            )
        available = (existing_manifest.get("payload") or {}).get("available")
        if (existing_status == "pending") is not (available is True):
            raise ChangeSetPayloadCorrupt(
                f"Existing idempotency payload availability drifted: {idempotency_key}"
            )
        return False

    terminal_at = None
    time_source = "active"
    manifest = _change_set_manifest(
        change_set,
        payload_bytes,
        payload_available=True,
        terminal_at=terminal_at,
    )
    manifest_json = _canonical_json_bytes(manifest).decode("utf-8")
    reserved = conn.execute(
        "INSERT OR IGNORE INTO change_set_idempotency "
        "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
        (idempotency_key, change_set_id, now),
    )
    if not reserved.rowcount:
        raise ChangeSetIdempotencyConflict(
            f"Idempotency key reservation raced with another owner: {idempotency_key}"
        )
    conn.execute(
        "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
        "VALUES (?, ?, ?)",
        (change_set_id, manifest_json, now),
    )
    stored_hash = _store_change_set_payload(conn, payload_bytes, created_at=now)
    conn.execute(
        "INSERT INTO change_set_payload_refs "
        "(change_set_id, payload_sha256, created_at) VALUES (?, ?, ?)",
        (change_set_id, stored_hash, now),
    )
    conn.execute(
        "INSERT INTO change_set_lifecycle_v6 "
        "(change_set_id, status, created_at, terminal_at, time_source, "
        "payload_guard_sha256) VALUES (?, ?, ?, ?, ?, ?)",
        (
            change_set_id,
            status,
            _strict_utc_instant(change_set.get("created_at")),
            terminal_at,
            time_source,
            hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
        ),
    )
    return True


def _terminalize_change_set(
    conn: sqlite3.Connection,
    change_set: dict,
    terminal_at: str,
) -> dict:
    change_set_id = str(change_set.get("change_set_id") or "")
    terminal_at = _strict_utc_instant(terminal_at) or ""
    if not change_set_id or not terminal_at:
        raise ChangeSetPayloadCorrupt(
            "Terminal change-set transition requires an id and UTC instant"
        )
    row = conn.execute(
        "SELECT change_sets.data_json, lifecycle.status, "
        "lifecycle.payload_guard_sha256, ref.payload_sha256 "
        "FROM change_sets JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "LEFT JOIN change_set_payload_refs AS ref "
        "ON ref.change_set_id = change_sets.change_set_id "
        "WHERE change_sets.change_set_id = ?",
        (change_set_id,),
    ).fetchone()
    if row is None or str(row["status"]) != "pending":
        raise ChangeSetPayloadCorrupt(
            f"Change-set is not a current pending candidate: {change_set_id}"
        )
    raw_manifest = str(row["data_json"])
    if not hmac.compare_digest(
        hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest(),
        str(row["payload_guard_sha256"]),
    ):
        raise ChangeSetPayloadCorrupt(
            f"Change-set manifest guard drifted: {change_set_id}"
        )
    _payload, payload_bytes = _canonical_change_set_payload(change_set)
    terminal = copy.deepcopy(change_set)
    terminal["status"] = "published"
    terminal["published_at"] = terminal_at
    terminal["terminal_at"] = terminal_at
    manifest = _change_set_manifest(
        terminal,
        payload_bytes,
        payload_available=False,
        terminal_at=terminal_at,
    )
    manifest_json = _canonical_json_bytes(manifest).decode("utf-8")
    cursor = conn.execute(
        "UPDATE change_sets SET data_json = ?, updated_at = ? "
        "WHERE change_set_id = ? AND data_json IS ?",
        (manifest_json, terminal_at, change_set_id, raw_manifest),
    )
    if cursor.rowcount != 1:
        raise ChangeSetPayloadCorrupt(
            f"Change-set manifest changed before terminal transition: {change_set_id}"
        )
    lifecycle_cursor = conn.execute(
        "UPDATE change_set_lifecycle_v6 SET status = 'published', terminal_at = ?, "
        "time_source = 'published_at', payload_guard_sha256 = ? "
        "WHERE change_set_id = ? AND status = 'pending' AND terminal_at IS NULL",
        (
            terminal_at,
            hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
            change_set_id,
        ),
    )
    if lifecycle_cursor.rowcount != 1:
        raise ChangeSetPayloadCorrupt(
            f"Change-set lifecycle changed before terminal transition: {change_set_id}"
        )
    payload_hash = str(row["payload_sha256"] or "")
    conn.execute(
        "DELETE FROM change_set_payload_refs WHERE change_set_id = ?",
        (change_set_id,),
    )
    if payload_hash:
        _delete_unreferenced_change_set_payloads(conn, {payload_hash})
    return manifest


def _load_change_set_by_idempotency_key(idempotency_key: str) -> dict | None:
    """Load one change set through the dedicated key index with legacy fallback."""
    conn = get_connection()
    row = conn.execute(
        "SELECT change_sets.data_json FROM change_set_idempotency "
        "JOIN change_sets ON change_sets.change_set_id = "
        "change_set_idempotency.change_set_id "
        "WHERE change_set_idempotency.idempotency_key = ? LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        # Historical stores can predate change_set_idempotency. Keep them
        # readable until a separately approved maintenance window backfills
        # and validates every legacy key.
        legacy_rows = conn.execute(
            "SELECT change_set_id, data_json FROM change_sets "
            "WHERE json_valid(data_json) = 1 "
            "AND json_extract(data_json, '$.idempotency_key') = ? "
            "ORDER BY change_set_id LIMIT 2",
            (idempotency_key,),
        ).fetchall()
        if len(legacy_rows) > 1:
            owners = ", ".join(str(item["change_set_id"]) for item in legacy_rows)
            raise ChangeSetIdempotencyConflict(
                "Legacy change-set idempotency key has multiple unmapped owners: "
                f"{idempotency_key} ({owners})."
            )
        row = legacy_rows[0] if legacy_rows else None
    if row is None:
        return None
    loaded = json.loads(row["data_json"])
    if loaded.get("manifest_version") == _CHANGE_SET_MANIFEST_VERSION:
        descriptor = loaded.get("payload") or {}
        return _hydrate_change_set(
            loaded,
            connection=conn,
            require_payload=descriptor.get("available") is True,
        )
    return loaded


def create_change_set(
    page_paths: list[str],
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    if len(page_paths) > _CHANGE_SET_MAX_PAGES:
        raise ChangeSetPayloadTooLarge(
            "Change-set page count exceeds hard limit: "
            f"{len(page_paths)} > {_CHANGE_SET_MAX_PAGES}"
        )
    initialize_meta_store()
    proposed_entities = []
    proposed_claims = []
    proposed_evidence = []
    proposed_source_updates = []
    proposed_source_artifacts = []
    proposed_extraction_runs = []
    proposed_edges = []
    affected_ids = []
    page_summaries = []
    page_fingerprints = []

    for page_path in page_paths:
        if not os.path.exists(page_path):
            continue
        frontmatter, body, raw_content = read_markdown_file(page_path)
        page_fingerprints.append(hashlib.sha1(raw_content.encode("utf-8")).hexdigest())
        extracted = extract_page_objects(page_path, frontmatter, body)
        proposed_entities.extend(extracted["entities"])
        proposed_claims.extend(extracted["claims"])
        proposed_evidence.extend(extracted["evidence"])
        proposed_source_updates.extend(extracted["sources"])
        proposed_source_artifacts.extend(extracted.get("source_artifacts", []))
        proposed_extraction_runs.extend(extracted.get("extraction_runs", []))
        proposed_edges.extend(extracted.get("edges", []))
        affected_ids.extend([record["entity_id"] for record in extracted["entities"]])
        affected_ids.extend([record["claim_id"] for record in extracted["claims"]])
        page_summaries.append(extracted["page_key"])

    idempotency_key = _stable_id(
        "changeset_idem",
        "|".join([origin, *sorted(page_summaries), *sorted(page_fingerprints)]),
    )
    if not force:
        duplicate = _load_change_set_by_idempotency_key(idempotency_key)
        if duplicate:
            if (
                auto_approve
                and _normalized_change_set_status(duplicate.get("status")) == "pending"
            ):
                duplicate = apply_and_record_change_sets_batch([duplicate])[0]
            duplicate["deduplicated"] = True
            return duplicate

    change_set = {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
        "idempotency_key": idempotency_key,
        "origin": origin,
        "created_at": _utc_now(),
        "status": "pending",
        "summary": summary or f"Sync pages: {', '.join(page_summaries[:5])}",
        "risk_level": "medium" if len(page_paths) > 3 else "low",
        "requires_human_review": not auto_approve,
        "affected_ids": sorted(set(affected_ids)),
        "affected_pages": [os.path.basename(path) for path in page_paths],
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
        "proposed_evidence": proposed_evidence,
        "proposed_source_updates": proposed_source_updates,
        "proposed_source_artifacts": proposed_source_artifacts,
        "proposed_extraction_runs": proposed_extraction_runs,
        "proposed_edges": proposed_edges,
        "write_contract": {
            "transactional": True,
            "idempotent": True,
            "canonical_targets": [
                "entities",
                "claims",
                "evidence",
                "sources",
                "operational_memory",
                "source_artifacts",
                "extraction_runs",
                "claim_versions",
                "evidence_versions",
                "entity_identities",
                "canonical_identities",
            ],
        },
    }

    with transaction():
        if auto_approve:
            return apply_and_record_change_sets_batch([change_set])[0]
        else:
            item = {
                "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                "type": "publish-candidate",
                "title": change_set["summary"],
                "description": f"Pending publish candidate from {origin}",
                "created_at": change_set["created_at"],
                "status": "pending",
                "source": origin,
                "affected_ids": change_set["affected_ids"],
                "change_set_id": change_set["change_set_id"],
                "search_queries": [],
                "affected_pages": [os.path.basename(path) for path in page_paths],
            }
            insert_governance_item_if_absent(item, ("change_set_id",))
            record_prepared_change_sets([change_set])
    return change_set


def prepare_change_set_from_content(
    filename: str,
    content: str,
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
) -> dict:
    """Build one canonical change set without applying or persisting it."""
    initialize_meta_store()
    frontmatter, body = split_frontmatter(content)
    extracted = extract_page_objects(filename, frontmatter, body)
    if not extracted.get("entities") and not filename.startswith("System_"):
        raise ValueError(f"No canonical entity could be extracted from {filename}.")

    page_key = extracted["page_key"]
    fingerprint = hashlib.sha1(content.encode("utf-8")).hexdigest()
    idempotency_key = _stable_id(
        "changeset_idem", "|".join([origin, page_key, fingerprint])
    )

    proposed_entities = extracted.get("entities", [])
    proposed_claims = extracted.get("claims", [])
    return {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
        "idempotency_key": idempotency_key,
        "origin": origin,
        "created_at": _utc_now(),
        "status": "pending",
        "summary": summary or f"Sync page: {page_key}",
        "risk_level": "low",
        "requires_human_review": not auto_approve,
        "affected_ids": sorted(
            {
                *[record["entity_id"] for record in proposed_entities],
                *[record["claim_id"] for record in proposed_claims],
            }
        ),
        "affected_pages": [filename],
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
        "proposed_evidence": extracted.get("evidence", []),
        "proposed_source_updates": extracted.get("sources", []),
        "proposed_source_artifacts": extracted.get("source_artifacts", []),
        "proposed_extraction_runs": extracted.get("extraction_runs", []),
        "proposed_edges": extracted.get("edges", []),
        "write_contract": {
            "transactional": True,
            "idempotent": True,
            "canonical_targets": [
                "entities",
                "claims",
                "evidence",
                "sources",
                "operational_memory",
                "source_artifacts",
                "extraction_runs",
                "claim_versions",
                "evidence_versions",
                "entity_identities",
                "canonical_identities",
            ],
        },
    }


def record_prepared_change_sets(change_sets: list[dict]) -> int:
    """Persist pending change sets once without scanning the JSON history."""
    if not change_sets:
        return 0
    prepared = _validate_change_set_batch_limits(change_sets)
    conn = get_connection()
    now = _utc_now()
    added = 0
    with transaction():
        for change_set, payload_bytes in prepared:
            added += int(
                _persist_prepared_change_set(
                    conn,
                    change_set,
                    now,
                    payload_bytes=payload_bytes,
                )
            )
    return added


def apply_and_record_change_sets_batch(change_sets: list[dict]) -> list[dict]:
    """Persist pending deltas, apply them, and detach payloads in one transaction."""
    if not change_sets:
        return []
    prepared = _validate_change_set_batch_limits(change_sets)
    if any(
        _normalized_change_set_status(change_set.get("status")) != "pending"
        for change_set, _payload_bytes in prepared
    ):
        raise ChangeSetPayloadCorrupt(
            "Apply-and-record accepts pending change sets only"
        )
    conn = get_connection()
    outcomes: dict[str, dict] = {}
    with transaction():
        now = _utc_now()
        active: list[dict] = []
        for change_set, payload_bytes in prepared:
            _persist_prepared_change_set(
                conn,
                change_set,
                now,
                payload_bytes=payload_bytes,
            )
            idempotency_key = str(
                change_set.get("idempotency_key")
                or change_set.get("change_set_id")
                or ""
            )
            row = conn.execute(
                "SELECT change_sets.data_json, lifecycle.status "
                "FROM change_set_idempotency JOIN change_sets "
                "ON change_sets.change_set_id = change_set_idempotency.change_set_id "
                "JOIN change_set_lifecycle_v6 AS lifecycle "
                "ON lifecycle.change_set_id = change_sets.change_set_id "
                "WHERE change_set_idempotency.idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ChangeSetPayloadCorrupt(
                    f"Prepared change set disappeared: {idempotency_key}"
                )
            manifest = _history_json_object(row["data_json"])
            if not manifest:
                raise ChangeSetPayloadCorrupt(
                    f"Prepared change-set manifest is malformed: {idempotency_key}"
                )
            status = _normalized_change_set_status(row["status"])
            if status == "pending":
                active.append(
                    _hydrate_change_set(
                        manifest,
                        connection=conn,
                        require_payload=True,
                    )
                )
            else:
                outcomes[idempotency_key] = manifest

        if active:
            _apply_change_sets_batch_unchecked(active)
            published_at = _utc_now()
            for change_set in active:
                terminal = _terminalize_change_set(
                    conn,
                    change_set,
                    published_at,
                )
                idempotency_key = str(
                    change_set.get("idempotency_key")
                    or change_set.get("change_set_id")
                    or ""
                )
                outcomes[idempotency_key] = terminal

    ordered = []
    for change_set in change_sets:
        idempotency_key = str(
            change_set.get("idempotency_key") or change_set.get("change_set_id") or ""
        )
        outcome = outcomes.get(idempotency_key)
        if outcome is None:
            raise ChangeSetPayloadCorrupt(
                f"Apply-and-record outcome is missing: {idempotency_key}"
            )
        ordered.append(outcome)
    return ordered


def create_change_set_from_content(
    filename: str,
    content: str,
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
) -> dict:
    """Create a canonical change set without requiring Markdown to be written first."""
    change_set = prepare_change_set_from_content(
        filename,
        content,
        origin,
        summary=summary,
        auto_approve=auto_approve,
    )
    existing = _load_change_set_by_idempotency_key(change_set["idempotency_key"])
    if existing:
        duplicate = existing
        if (
            auto_approve
            and _normalized_change_set_status(duplicate.get("status")) == "pending"
        ):
            duplicate = apply_and_record_change_sets_batch([duplicate])[0]
        duplicate["deduplicated"] = True
        return duplicate

    with transaction():
        if auto_approve:
            return apply_and_record_change_sets_batch([change_set])[0]
        else:
            item = {
                "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                "type": "publish-candidate",
                "title": change_set["summary"],
                "description": f"Pending publish candidate from {origin}",
                "created_at": change_set["created_at"],
                "status": "pending",
                "source": origin,
                "affected_ids": change_set["affected_ids"],
                "change_set_id": change_set["change_set_id"],
                "search_queries": [],
                "affected_pages": [filename],
            }
            insert_governance_item_if_absent(item, ("change_set_id",))
            record_prepared_change_sets([change_set])
    return change_set


def _normalized_owner_page(value: object) -> str:
    page_key = os.path.basename(str(value or "").strip())
    return page_key[:-3] if page_key.casefold().endswith(".md") else page_key


def _owner_record(
    record: dict,
    *,
    record_kind: str,
    record_id: str,
) -> tuple[str, str]:
    if record_kind == "entity":
        page_key = _normalized_owner_page(record.get("page_key"))
        if not page_key:
            raise CanonicalIdOwnershipError(
                f"Canonical {record_kind}_id {record_id!r} has no page_key owner; "
                "refusing an ambiguous global-ID write."
            )
        return page_key, page_key

    locator = record.get("locator")
    if not isinstance(locator, dict) or not locator:
        raise CanonicalIdOwnershipError(
            f"Canonical {record_kind}_id {record_id!r} has no locator owner; "
            "refusing an ambiguous global-ID write."
        )
    normalized_locator = copy.deepcopy(locator)
    page_key = _normalized_owner_page(normalized_locator.get("page_key"))
    if not page_key:
        raise CanonicalIdOwnershipError(
            f"Canonical {record_kind}_id {record_id!r} has a locator without page_key; "
            "refusing an ambiguous global-ID write."
        )
    # Headings and block positions are mutable projection details. The stable
    # claim ownership boundary is its canonical page; cross-page reuse remains
    # forbidden while same-page edits retain claim identity.
    return page_key, page_key


def _proposed_id_owners(
    records: list[dict],
    *,
    record_kind: str,
    id_field: str,
    affected_page_keys: set[str],
) -> dict[str, tuple[str, str]]:
    owners: dict[str, tuple[str, str]] = {}
    for record in records:
        record_id = str(record.get(id_field) or "").strip()
        if not record_id:
            raise CanonicalIdOwnershipError(
                f"Proposed canonical {record_kind} is missing required {id_field}."
            )
        owner = _owner_record(
            record,
            record_kind=record_kind,
            record_id=record_id,
        )
        if owner[0] not in affected_page_keys:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} declares owner {owner[0]!r}, "
                f"outside affected pages {sorted(affected_page_keys)!r}."
            )
        prior = owners.get(record_id)
        if prior is not None and prior != owner:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} has conflicting proposed owners: "
                f"{prior[1]!r} versus {owner[1]!r}."
            )
        owners[record_id] = owner
    return owners


def _rows_for_ids(
    conn,
    *,
    select_prefix: str,
    record_ids: set[str],
):
    ordered_ids = sorted(record_ids)
    for offset in range(0, len(ordered_ids), 500):
        batch = ordered_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        yield from conn.execute(
            f"{select_prefix} ({placeholders})",
            tuple(batch),
        ).fetchall()


def _identity_registry_owner_page(
    row,
    *,
    record_kind: str,
    record_id: str,
) -> str:
    page_key = _normalized_owner_page(row["page_key"])
    if not page_key or not str(row["identity_origin"] or "").strip():
        raise CanonicalIdOwnershipError(
            f"Canonical {record_kind}_id {record_id!r} has an invalid identity owner row."
        )
    try:
        payload = json.loads(row["data_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise CanonicalIdOwnershipError(
            f"Canonical {record_kind}_id {record_id!r} has invalid identity registry metadata."
        ) from exc
    expected = {
        "record_kind": record_kind,
        "record_id": record_id,
        "page_key": page_key,
    }
    if not isinstance(payload, dict) or payload != expected:
        raise CanonicalIdOwnershipError(
            f"Canonical {record_kind}_id {record_id!r} has conflicting identity registry metadata."
        )
    return page_key


def _register_locator_id_ownership(
    conn,
    *,
    owners: dict[str, tuple[str, str]],
    record_kind: str,
) -> None:
    """Reserve new IDs without ever rewriting an existing ownership row."""
    if not owners:
        return
    if not conn.in_transaction:
        raise RuntimeError(
            "Canonical identity registration requires an active transaction"
        )
    now = _utc_now()
    for record_id, owner in sorted(owners.items()):
        page_key = owner[0]
        row = conn.execute(
            "SELECT page_key, identity_origin, data_json FROM canonical_identities "
            "WHERE record_kind = ? AND record_id = ?",
            (record_kind, record_id),
        ).fetchone()
        if row is not None:
            registered_page = _identity_registry_owner_page(
                row,
                record_kind=record_kind,
                record_id=record_id,
            )
            if registered_page != page_key:
                raise CanonicalIdOwnershipError(
                    f"Canonical {record_kind}_id {record_id!r} is reserved by "
                    f"identity page {registered_page!r}, not {page_key!r}."
                )
            continue
        payload = _canonical_record_json(
            {
                "record_kind": record_kind,
                "record_id": record_id,
                "page_key": page_key,
            }
        )
        conn.execute(
            "INSERT INTO canonical_identities "
            "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
            "VALUES (?, ?, ?, 'canonical_write', ?, ?)",
            (record_kind, record_id, page_key, payload, now),
        )
        row = conn.execute(
            "SELECT page_key, identity_origin, data_json FROM canonical_identities "
            "WHERE record_kind = ? AND record_id = ?",
            (record_kind, record_id),
        ).fetchone()
        if row is None:
            raise CanonicalIdOwnershipError(
                f"Canonical {record_kind}_id {record_id!r} was not durably reserved."
            )
        registered_page = _identity_registry_owner_page(
            row,
            record_kind=record_kind,
            record_id=record_id,
        )
        if registered_page != page_key:
            raise CanonicalIdOwnershipError(
                f"Canonical {record_kind}_id {record_id!r} is reserved by identity page "
                f"{registered_page!r}, not {page_key!r}."
            )


def _validate_locator_id_ownership(
    conn,
    *,
    owners: dict[str, tuple[str, str]],
    record_kind: str,
) -> None:
    if not owners:
        return
    table_specs = {
        "claim": ("claims", "claim_id", "claim_versions"),
        "evidence": ("evidence", "evidence_id", "evidence_versions"),
    }
    table_name, id_field, version_table = table_specs[record_kind]
    record_ids = set(owners)
    for row in _rows_for_ids(
        conn,
        select_prefix=(
            "SELECT record_id, page_key, identity_origin, data_json "
            "FROM canonical_identities "
            f"WHERE record_kind = '{record_kind}' AND record_id IN"
        ),
        record_ids=record_ids,
    ):
        record_id = str(row["record_id"])
        registered_page = _identity_registry_owner_page(
            row,
            record_kind=record_kind,
            record_id=record_id,
        )
        proposed_page = owners[record_id][0]
        if registered_page != proposed_page:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} is reserved by identity page "
                f"{registered_page!r}, not {proposed_page!r}."
            )
    for row in _rows_for_ids(
        conn,
        select_prefix=(
            f"SELECT {id_field}, data_json FROM {table_name} WHERE {id_field} IN"
        ),
        record_ids=record_ids,
    ):
        record_id = str(row[id_field])
        try:
            existing = json.loads(row["data_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} has invalid locator metadata."
            ) from exc
        existing_owner = _owner_record(
            existing,
            record_kind=record_kind,
            record_id=record_id,
        )
        if owners[record_id] != existing_owner:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} is owned by page "
                f"{existing_owner[0]!r}, not {owners[record_id][0]!r}."
            )

    for row in _rows_for_ids(
        conn,
        select_prefix=(
            f"SELECT {id_field}, page_key FROM {version_table} WHERE {id_field} IN"
        ),
        record_ids=record_ids,
    ):
        record_id = str(row[id_field])
        historical_page = _normalized_owner_page(row["page_key"])
        if not historical_page:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} has a historical version "
                "without page_key ownership."
            )
        proposed_page = owners[record_id][0]
        if historical_page != proposed_page:
            raise CanonicalIdOwnershipError(
                f"Canonical {id_field} {record_id!r} is reserved by historical page "
                f"{historical_page!r}, not {proposed_page!r}."
            )


def _validate_canonical_id_ownership(
    conn,
    *,
    proposed_entities: list[dict],
    proposed_claims: list[dict],
    proposed_evidence: list[dict],
    affected_page_keys: set[str],
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Validate global-ID ownership before any page-scoped delete or upsert."""
    entity_owners = _proposed_id_owners(
        proposed_entities,
        record_kind="entity",
        id_field="entity_id",
        affected_page_keys=affected_page_keys,
    )
    claim_owners = _proposed_id_owners(
        proposed_claims,
        record_kind="claim",
        id_field="claim_id",
        affected_page_keys=affected_page_keys,
    )
    evidence_owners = _proposed_id_owners(
        proposed_evidence,
        record_kind="evidence",
        id_field="evidence_id",
        affected_page_keys=affected_page_keys,
    )

    if entity_owners:
        entity_ids = set(entity_owners)
        for row in _rows_for_ids(
            conn,
            select_prefix=(
                "SELECT entity_id, data_json FROM entities WHERE entity_id IN"
            ),
            record_ids=entity_ids,
        ):
            entity_id = str(row["entity_id"])
            try:
                existing = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise CanonicalIdOwnershipError(
                    f"Canonical entity_id {entity_id!r} has invalid owner metadata."
                ) from exc
            existing_owner = _owner_record(
                existing,
                record_kind="entity",
                record_id=entity_id,
            )
            if entity_owners[entity_id] != existing_owner:
                raise CanonicalIdOwnershipError(
                    f"Canonical entity_id {entity_id!r} is owned by page "
                    f"{existing_owner[0]!r}, not {entity_owners[entity_id][0]!r}."
                )

        for row in _rows_for_ids(
            conn,
            select_prefix=(
                "SELECT entity_id, page_key FROM entity_identities WHERE entity_id IN"
            ),
            record_ids=entity_ids,
        ):
            entity_id = str(row["entity_id"])
            identity_page = _normalized_owner_page(row["page_key"])
            if not identity_page:
                raise CanonicalIdOwnershipError(
                    f"Canonical entity_id {entity_id!r} has an identity row "
                    "without page_key ownership."
                )
            if entity_owners[entity_id][0] != identity_page:
                raise CanonicalIdOwnershipError(
                    f"Canonical entity_id {entity_id!r} is reserved by identity page "
                    f"{identity_page!r}, not {entity_owners[entity_id][0]!r}."
                )

    _validate_locator_id_ownership(
        conn,
        owners=claim_owners,
        record_kind="claim",
    )
    _validate_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    return claim_owners, evidence_owners


def _apply_change_sets_batch_unchecked(change_sets: list[dict]) -> list[dict]:
    """Apply a page-scoped canonical delta inside an existing transaction."""
    if not change_sets:
        return []
    change_sets = [
        _hydrate_change_set(item, connection=get_connection(), require_payload=True)
        for item in change_sets
    ]
    if any(
        _normalized_change_set_status(item.get("status")) != "pending"
        for item in change_sets
    ):
        raise ChangeSetPayloadCorrupt("Canonical apply accepts pending deltas only")
    _validate_change_set_batch_limits(change_sets)
    affected_pages = {
        page
        for change_set in change_sets
        for page in change_set.get("affected_pages", [])
    }
    affected_page_keys = {_normalized_owner_page(page) for page in affected_pages}
    proposed_entities = [
        record for item in change_sets for record in item.get("proposed_entities", [])
    ]
    proposed_claims = [
        record for item in change_sets for record in item.get("proposed_claims", [])
    ]
    proposed_evidence = [
        record for item in change_sets for record in item.get("proposed_evidence", [])
    ]
    proposed_sources = [
        record
        for item in change_sets
        for record in item.get("proposed_source_updates", [])
    ]
    proposed_source_artifacts = [
        record
        for item in change_sets
        for record in item.get("proposed_source_artifacts", [])
    ]
    proposed_extraction_runs = [
        record
        for item in change_sets
        for record in item.get("proposed_extraction_runs", [])
    ]
    proposed_edges = [
        record for item in change_sets for record in item.get("proposed_edges", [])
    ]

    conn = get_connection()
    claim_owners, evidence_owners = _validate_canonical_id_ownership(
        conn,
        proposed_entities=proposed_entities,
        proposed_claims=proposed_claims,
        proposed_evidence=proposed_evidence,
        affected_page_keys=affected_page_keys,
    )
    _register_locator_id_ownership(
        conn,
        owners=claim_owners,
        record_kind="claim",
    )
    _register_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    old_entity_ids: set[str] = set()
    old_claim_ids: set[str] = set()
    old_claim_rows = []
    old_claim_records = []
    old_evidence_records = []
    if affected_page_keys:
        affected_page_params = tuple(sorted(affected_page_keys))
        placeholders = ",".join("?" for _ in affected_page_params)
        old_entity_ids = {
            row["entity_id"]
            for row in conn.execute(
                f"SELECT entity_id FROM entities WHERE json_extract(data_json, '$.page_key') IN ({placeholders})",
                affected_page_params,
            )
        }
        old_claim_rows = conn.execute(
            f"SELECT claim_id, claim_text, data_json, updated_at FROM claims "
            f"WHERE json_extract(data_json, '$.locator.page_key') IN ({placeholders})",
            affected_page_params,
        ).fetchall()
        old_claim_ids = {row["claim_id"] for row in old_claim_rows}
        old_claim_records = [json.loads(row["data_json"]) for row in old_claim_rows]
        old_evidence_rows = conn.execute(
            f"SELECT data_json FROM evidence "
            f"WHERE json_extract(data_json, '$.locator.page_key') IN ({placeholders})",
            affected_page_params,
        ).fetchall()
        old_evidence_records = [
            json.loads(row["data_json"]) for row in old_evidence_rows
        ]
        _append_version_records(
            "claim_versions",
            "claim_id",
            "claim_family_id",
            "claimfamily",
            "claim_version",
            old_claim_records,
        )
        _append_version_records(
            "evidence_versions",
            "evidence_id",
            "evidence_family_id",
            "evidencefamily",
            "evidence_version",
            old_evidence_records,
        )
        conn.execute(
            f"DELETE FROM entities WHERE json_extract(data_json, '$.page_key') IN ({placeholders})",
            affected_page_params,
        )
        conn.execute(
            f"DELETE FROM claims WHERE json_extract(data_json, '$.locator.page_key') IN ({placeholders})",
            affected_page_params,
        )
        conn.execute(
            f"DELETE FROM evidence WHERE json_extract(data_json, '$.locator.page_key') IN ({placeholders})",
            affected_page_params,
        )
        conn.execute(
            f"DELETE FROM claim_graph_edges WHERE source_id IN ({placeholders})",
            affected_page_params,
        )
        conn.execute(
            f"DELETE FROM page_graph_edges WHERE source_id IN ({placeholders})",
            affected_page_params,
        )

    _upsert_canonical_records("entities", "entity_id", proposed_entities)
    _upsert_canonical_records("claims", "claim_id", proposed_claims)
    _upsert_canonical_records("evidence", "evidence_id", proposed_evidence)
    _upsert_canonical_records("sources", "source_id", proposed_sources)
    _upsert_foundation_records(
        proposed_entities,
        proposed_source_artifacts,
        proposed_extraction_runs,
    )
    _append_version_records(
        "claim_versions",
        "claim_id",
        "claim_family_id",
        "claimfamily",
        "claim_version",
        proposed_claims,
    )
    _append_version_records(
        "evidence_versions",
        "evidence_id",
        "evidence_family_id",
        "evidencefamily",
        "evidence_version",
        proposed_evidence,
    )
    _refresh_alias_delta(old_entity_ids, proposed_entities)
    _refresh_operational_memory_delta(old_claim_ids, proposed_claims)
    from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

    sync_timeline_events_for_claim_delta(old_claim_rows, proposed_claims)
    save_graph_edges(proposed_edges)
    memory_count = conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0]
    for change_set in change_sets:
        change_set["operational_memory_count"] = int(memory_count)
    # Obsolete view_builder block removed to prevent ImportError warnings
    # try:
    #     from vector_lake import view_builder
    #
    #     change_set["view_rebuild"] = view_builder.rebuild_views_for_change_set(change_set)
    # except Exception as exc:
    #     log.warning(f"View rebuild failed for {change_set.get('change_set_id')}: {exc}")
    return change_sets


def apply_change_sets_batch(change_sets: list[dict]) -> list[dict]:
    """Persist, apply, and terminalize direct canonical deltas atomically."""
    prepared = []
    for item in change_sets:
        if not isinstance(item, dict):
            raise ChangeSetPayloadCorrupt("Every change set must be an object")
        change_set = copy.deepcopy(item)
        status = _normalized_change_set_status(change_set.get("status"))
        if status != "pending":
            raise ChangeSetPayloadCorrupt(
                "Direct canonical apply accepts pending change sets only"
            )
        change_set["status"] = "pending"
        change_set.setdefault("origin", "direct_apply")
        change_set.setdefault("created_at", _utc_now())
        change_set.setdefault("summary", "Direct canonical delta")
        change_set.setdefault("requires_human_review", False)
        _payload, payload_bytes = _canonical_change_set_payload(change_set)
        payload_sha256 = _change_set_payload_digest(payload_bytes)
        if not str(change_set.get("change_set_id") or "").strip():
            change_set["change_set_id"] = f"changeset_direct_{payload_sha256[:16]}"
        if not str(change_set.get("idempotency_key") or "").strip():
            change_set["idempotency_key"] = f"changeset_direct_idem_{payload_sha256}"
        prepared.append(change_set)
    outcomes = apply_and_record_change_sets_batch(prepared)
    for original, outcome in zip(change_sets, outcomes):
        original.update(copy.deepcopy(outcome))
    return change_sets


def apply_change_set(change_set: dict) -> dict:
    apply_change_sets_batch([change_set])
    return change_set


def _validated_change_set_limit(limit: int | None, *, default: int = 100) -> int:
    normalized = default if limit is None else int(limit)
    if normalized < 1 or normalized > _CHANGE_SET_MAX_BATCH_ITEMS:
        raise ValueError(
            f"change-set limit must be between 1 and {_CHANGE_SET_MAX_BATCH_ITEMS}"
        )
    return normalized


def publish_change_sets(limit: int | None = None) -> dict:
    from vector_lake.db_store import get_connection, transaction

    conn = get_connection()
    normalized_limit = _validated_change_set_limit(limit)
    rows = conn.execute(
        "SELECT change_sets.change_set_id FROM change_sets "
        "JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "WHERE lifecycle.status = 'pending' "
        "ORDER BY lifecycle.created_at, change_sets.change_set_id LIMIT ?",
        (normalized_limit,),
    ).fetchall()

    published = 0
    published_ids = []

    for row in rows:
        with transaction():
            current = conn.execute(
                "SELECT data_json FROM change_sets WHERE change_set_id = ?",
                (row["change_set_id"],),
            ).fetchone()
            if current is None:
                raise ChangeSetPayloadCorrupt(
                    f"Pending change set disappeared: {row['change_set_id']}"
                )
            manifest = json.loads(current["data_json"])
            change_set = _hydrate_change_set(
                manifest,
                connection=conn,
                require_payload=True,
            )
            _apply_change_sets_batch_unchecked([change_set])
            published_at = _utc_now()
            _terminalize_change_set(conn, change_set, published_at)

        published += 1
        published_ids.append(str(row["change_set_id"]))

    for change_set_id in published_ids:
        update_governance_items_by_field(
            "change_set_id",
            change_set_id,
            {"status": "published", "resolved_at": _utc_now()},
        )
    return {"published": published, "change_set_ids": published_ids}


def pending_change_sets(limit: int = 100) -> list:
    from vector_lake.db_store import get_connection

    conn = get_connection()
    normalized_limit = _validated_change_set_limit(limit)
    rows = conn.execute(
        "SELECT change_sets.data_json FROM change_sets "
        "JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "WHERE lifecycle.status = 'pending' "
        "ORDER BY lifecycle.created_at, change_sets.change_set_id LIMIT ?",
        (normalized_limit,),
    ).fetchall()
    return [
        _hydrate_change_set(json.loads(row["data_json"]), connection=conn)
        for row in rows
    ]


def pending_governance_items() -> list:
    return [
        item
        for item in load_governance_queue()["items"]
        if item.get("status") == "pending"
    ]


def reviewable_governance_items() -> list:
    """Return items that the public review surface can act on.

    Research keeps using ``pending_governance_items`` so a committed merge that
    only needs projection reconciliation is not rediscovered as new research.
    """
    return [
        item
        for item in load_governance_queue()["items"]
        if item.get("status") == "pending"
        or (item.get("type") == "merge" and item.get("status") == "projection_pending")
    ]


def sync_pages_to_canonical(
    page_paths: list[str],
    origin: str,
    auto_approve: bool = True,
    summary: str | None = None,
) -> dict | None:
    existing_paths = []
    deleted_paths = []
    for path in page_paths:
        if path:
            if os.path.exists(path):
                existing_paths.append(str(path))
            else:
                deleted_paths.append(str(path))

    # V10.1 Delete orphaned entities in SQLite when Markdown file is deleted/renamed
    if deleted_paths:
        for path in deleted_paths:
            basename = os.path.basename(path)
            if basename.casefold().endswith(".md"):
                page_key = basename[:-3]
                entity_id = _stable_id("entity", page_key)
                delete_entity(entity_id)
                import logging

                logging.getLogger("governance").info(
                    f"Deleted orphan entity {entity_id} ({page_key}) from SQLite due to missing markdown file."
                )

    if not existing_paths:
        return None
    return create_change_set(
        existing_paths, origin=origin, summary=summary, auto_approve=auto_approve
    )


def migrate_existing_wiki(dry_run: bool = False) -> dict:
    wiki_dir = get_wiki_dir()
    excluded = {"index.md", "log.md", "overview.md"}
    page_paths = [
        str(path)
        for path in sorted(iter_markdown_files(wiki_dir), key=lambda item: item.name)
        if path.name.casefold() not in excluded
    ]

    if dry_run:
        counts = {
            "entities": 0,
            "claims": 0,
            "evidence": 0,
            "sources": 0,
            "valid_pages": 0,
        }
        for page_path in page_paths:
            frontmatter, body, _ = read_markdown_file(page_path)
            extracted = extract_page_objects(page_path, frontmatter, body)
            if extracted.get("entities") or os.path.basename(page_path).startswith(
                "System_"
            ):
                counts["valid_pages"] += 1
            counts["entities"] += len(extracted.get("entities", []))
            counts["claims"] += len(extracted.get("claims", []))
            counts["evidence"] += len(extracted.get("evidence", []))
            counts["sources"] += len(extracted.get("sources", []))
        return {
            "dry_run": True,
            "pages_scanned": len(page_paths),
            **counts,
        }

    initialize_meta_store()
    preflight: list[tuple[str, int]] = []
    migrated_page_keys: set[str] = set()
    for page_path in page_paths:
        _frontmatter, _body, content = read_markdown_file(page_path)
        prepared = prepare_change_set_from_content(
            os.path.basename(page_path),
            content,
            origin="migrate-v8-preflight",
            auto_approve=True,
        )
        _payload, payload_bytes = _canonical_change_set_payload(prepared)
        if len(payload_bytes) > _CHANGE_SET_MAX_PAYLOAD_BYTES:
            raise ChangeSetPayloadTooLarge(
                "Migration page exceeds the change-set payload hard limit: "
                f"{os.path.basename(page_path)} ({len(payload_bytes)} > "
                f"{_CHANGE_SET_MAX_PAYLOAD_BYTES} bytes)"
            )
        preflight.append((page_path, len(payload_bytes)))
        migrated_page_keys.update(
            _normalized_owner_page(page)
            for page in prepared.get("affected_pages", [])
            if page
        )

    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_bytes = 0
    conservative_bytes = int(_CHANGE_SET_MAX_PAYLOAD_BYTES * 0.85)
    for page_path, payload_bytes in preflight:
        if current_batch and (
            len(current_batch) >= 20
            or current_bytes + payload_bytes > conservative_bytes
        ):
            batches.append(current_batch)
            current_batch = []
            current_bytes = 0
        current_batch.append(page_path)
        current_bytes += payload_bytes
    if current_batch:
        batches.append(current_batch)

    change_set_ids = []
    for index, batch in enumerate(batches, start=1):
        change_set = create_change_set(
            batch,
            origin="migrate-v8",
            summary=f"V8 migration batch {index}/{len(batches)}",
            auto_approve=True,
            force=True,
        )
        change_set_ids.append(change_set["change_set_id"])
    canonical_page_keys = {
        item.get("page_key")
        for item in load_entities()["items"].values()
        if item.get("page_key")
    }
    stale_entities = canonical_page_keys - migrated_page_keys

    return {
        "dry_run": False,
        "change_set_id": change_set_ids[0] if change_set_ids else None,
        "change_set_ids": change_set_ids,
        "change_set_batches": len(change_set_ids),
        "pages_scanned": len(page_paths),
        "entities": len(load_entities()["items"]),
        "claims": len(load_claims()["items"]),
        "evidence": len(load_evidence()["items"]),
        "sources": len(load_sources()["items"]),
        "stale_entities_preserved": len(stale_entities),
    }


def ensure_canonical_store_populated() -> dict:
    initialize_meta_store()
    counts = canonical_store_counts()
    wiki_pages = _count_wiki_pages()

    if any(counts.values()) or wiki_pages == 0:
        return {
            "bootstrapped": False,
            "entities": counts["entities"],
            "claims": counts["claims"],
            "sources": counts["sources"],
            "pages_scanned": wiki_pages,
        }

    if mcp_readonly_surface_enabled():
        raise CanonicalStoreNotReady("canonical_store_not_ready:empty")

    log.info(
        "Canonical store is empty; bootstrapping V8 objects from existing wiki pages."
    )
    result = migrate_existing_wiki(dry_run=False)
    result["bootstrapped"] = True
    return result


def governance_projection() -> dict:
    ensure_canonical_store_populated()
    entities = load_entities()
    sources = load_sources()
    memory_objects = load_memory_objects()
    if not memory_objects.get("items") and load_claims().get("items"):
        memory_objects = rebuild_operational_memory()
    queue = load_governance_queue()
    annotated = annotated_claims()
    claim_index = {claim["claim_id"]: copy.deepcopy(claim) for claim in annotated}
    return {
        "entity_index": copy.deepcopy(entities["items"]),
        "claim_index": claim_index,
        "memory_index": copy.deepcopy(memory_objects["items"]),
        "memory_type_counts": copy.deepcopy(
            memory_objects.get("memory_type_counts", {})
        ),
        "source_index": copy.deepcopy(sources["items"]),
        "pending_change_set_count": len(
            [item for item in queue["items"] if item.get("status") == "pending"]
        ),
        "claim_graph": build_claim_graph_projection(),
    }


def enqueue_governance_item(
    item_type: str,
    title: str,
    description: str,
    source: str,
    search_queries: list,
    affected_pages: list,
    priority: str | None = None,
    critical_decision_refs: list[str] | None = None,
):
    import uuid

    item = {
        "item_id": f"gov_{uuid.uuid4().hex[:12]}",
        "type": item_type,
        "title": title,
        "description": description,
        "created_at": _utc_now(),
        "status": "pending",
        "source": source,
        "search_queries": search_queries,
        "affected_pages": affected_pages,
        "critical_decision_refs": critical_decision_refs or [],
    }
    if priority is not None:
        item["priority"] = priority
    item = normalize_governance_item(item)
    upsert_governance_item(item, insert_only=True)
    return item


_HISTORY_TERMINAL_CHANGE_SET_STATUSES = (
    "applied",
    "cancelled",
    "failed",
    "published",
    "rejected",
    "superseded",
)
_HISTORY_TERMINAL_QUEUE_STATUSES = (
    "cancelled",
    "completed",
    "dismissed",
    "published",
    "rejected",
    "resolved",
    "superseded",
)
_HISTORY_TERMINAL_JOB_STATUSES = (
    "finalized",
    "completed",
    "cancelled",
    "superseded",
)
_HISTORY_TERMINAL_OUTBOX_STATUSES = ("completed", "failed", "superseded")
_HISTORY_RETENTION_TABLES = (
    "change_sets",
    "jobs",
    "mutation_outbox",
    "claim_versions",
    "evidence_versions",
)
_HISTORY_ACTIVE_CHANGE_SET_MAX_ROWS = 1000
_HISTORY_ACTIVE_CHANGE_SET_MAX_BYTES = 64 * 1024 * 1024
_HISTORY_ACTIVE_OUTBOX_MAX_ROWS = 1000
_HISTORY_ACTIVE_OUTBOX_MAX_BYTES = 16 * 1024 * 1024
_HISTORY_ACTIVE_JOB_MAX_ROWS = 1000
_HISTORY_ACTIVE_JOB_MAX_BYTES = 64 * 1024 * 1024
_HISTORY_VERSION_MIN_SCAN_ROWS = 1000
_HISTORY_VERSION_MAX_SCAN_ROWS = 5000
_HISTORY_VERSION_MAX_CURSOR_BYTES = 1024
_HISTORY_VERSION_MAX_KEEP_PER_FAMILY = 1000
_HISTORY_CANONICAL_GUARD_MAX_BYTES = 4 * 1024 * 1024
_HISTORY_RETENTION_MAX_DELETE_BYTES = 128 * 1024 * 1024


def _history_page_key(value: object) -> str:
    page_key = os.path.basename(str(value or "").strip())
    return page_key[:-3] if page_key.casefold().endswith(".md") else page_key


def _history_json_object(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sqlite_text_blob_sha256(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    rowid: int,
    expected_bytes: int,
) -> str:
    """Hash one SQLite TEXT/BLOB value without materializing it in Python."""
    if (table_name, column_name) not in {("change_sets", "data_json")}:
        raise ValueError("Unsupported SQLite streaming hash target")
    if expected_bytes < 0:
        raise ValueError("expected_bytes must be zero or positive")
    digest = hashlib.sha256()
    observed = 0
    try:
        with conn.blobopen(
            table_name,
            column_name,
            int(rowid),
            readonly=True,
        ) as blob:
            if len(blob) != expected_bytes:
                raise RuntimeError("SQLite value length drifted before streaming hash")
            while observed < expected_bytes:
                chunk = blob.read(min(1024 * 1024, expected_bytes - observed))
                if not chunk:
                    break
                digest.update(chunk)
                observed += len(chunk)
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite streaming hash failed") from exc
    if observed != expected_bytes:
        raise RuntimeError(
            f"SQLite streaming hash read {observed} of {expected_bytes} bytes"
        )
    return digest.hexdigest()


def _history_active_protections(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Collect identifiers that unfinished work may still need."""
    protected = {
        "claim_ids": set(),
        "claim_family_ids": set(),
        "evidence_ids": set(),
        "evidence_family_ids": set(),
        "page_keys": set(),
        "block_version_retention": set(),
        "guard_parts": set(),
    }
    active_rows = conn.execute(
        "SELECT change_sets.change_set_id, lifecycle.status, "
        "lifecycle.terminal_at, lifecycle.payload_guard_sha256, "
        "length(CAST(change_sets.data_json AS BLOB)) AS manifest_bytes, "
        "CASE WHEN json_valid(change_sets.data_json) "
        "THEN json_extract(change_sets.data_json, '$.manifest_version') END "
        "AS manifest_version, CASE WHEN json_valid(change_sets.data_json) "
        "THEN json_extract(change_sets.data_json, '$.payload.raw_bytes') END "
        "AS payload_raw_bytes "
        "FROM change_sets JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "WHERE lifecycle.status NOT IN "
        "('applied','cancelled','failed','published','rejected','superseded') "
        "ORDER BY change_sets.change_set_id LIMIT ?",
        (_HISTORY_ACTIVE_CHANGE_SET_MAX_ROWS + 1,),
    ).fetchall()
    if len(active_rows) > _HISTORY_ACTIVE_CHANGE_SET_MAX_ROWS:
        protected["block_version_retention"].add("active_change_set_row_limit")
        protected["guard_parts"].add(
            "active_change_set_row_limit:" + str(_HISTORY_ACTIVE_CHANGE_SET_MAX_ROWS)
        )
        active_rows = active_rows[:_HISTORY_ACTIVE_CHANGE_SET_MAX_ROWS]
    active_bytes = 0
    for row in active_rows:
        change_set_id = str(row["change_set_id"])
        manifest_bytes = int(row["manifest_bytes"] or 0)
        manifest_version = int(row["manifest_version"] or 0)
        try:
            payload_raw_bytes = int(row["payload_raw_bytes"])
        except (TypeError, ValueError):
            payload_raw_bytes = 0 if manifest_version == 0 else -1
        estimated_bytes = (
            manifest_bytes + payload_raw_bytes
            if manifest_version == _CHANGE_SET_MANIFEST_VERSION
            else manifest_bytes * 2
        )
        active_bytes += max(0, estimated_bytes)
        protected["guard_parts"].add(
            f"change_set:{change_set_id}:{row['payload_guard_sha256']}:"
            f"{manifest_version}:{manifest_bytes}:{payload_raw_bytes}"
        )
        if manifest_version not in {0, 1, _CHANGE_SET_MANIFEST_VERSION}:
            protected["block_version_retention"].add("unbounded_active_change_set")
            continue
        max_inline_bytes = (
            _CHANGE_SET_MAX_MANIFEST_BYTES
            if manifest_version == _CHANGE_SET_MANIFEST_VERSION
            else _CHANGE_SET_MAX_PAYLOAD_BYTES
        )
        if (
            manifest_bytes < 0
            or manifest_bytes > max_inline_bytes
            or payload_raw_bytes < 0
            or payload_raw_bytes > _CHANGE_SET_MAX_PAYLOAD_BYTES
        ):
            protected["block_version_retention"].add("unbounded_active_change_set")
            continue
        if active_bytes > _HISTORY_ACTIVE_CHANGE_SET_MAX_BYTES:
            protected["block_version_retention"].add("active_change_set_byte_limit")
            continue
        manifest_row = conn.execute(
            "SELECT data_json FROM change_sets WHERE change_set_id = ? "
            "AND length(CAST(data_json AS BLOB)) = ? "
            "AND length(CAST(data_json AS BLOB)) <= ?",
            (
                change_set_id,
                manifest_bytes,
                max_inline_bytes,
            ),
        ).fetchone()
        change_set = _history_json_object(
            manifest_row["data_json"] if manifest_row is not None else ""
        )
        if not change_set:
            protected["block_version_retention"].add("malformed_change_set")
            continue
        try:
            if manifest_version == _CHANGE_SET_MANIFEST_VERSION:
                _validate_loaded_change_set_manifest(
                    change_set,
                    change_set_id=change_set_id,
                    lifecycle_status=str(row["status"]),
                    lifecycle_terminal_at=row["terminal_at"],
                )
            else:
                _validate_change_set_batch_limits([change_set])
            if not hmac.compare_digest(
                hashlib.sha256(
                    str(manifest_row["data_json"]).encode("utf-8")
                ).hexdigest(),
                str(row["payload_guard_sha256"] or ""),
            ):
                raise ChangeSetPayloadCorrupt("active manifest guard drifted")
            if manifest_version == _CHANGE_SET_MANIFEST_VERSION:
                change_set = _hydrate_change_set(change_set, connection=conn)
        except ChangeSetPayloadCorrupt:
            protected["block_version_retention"].add("corrupt_active_change_set")
            continue
        for page in change_set.get("affected_pages") or []:
            if page_key := _history_page_key(page):
                protected["page_keys"].add(page_key)
        for record in change_set.get("proposed_claims") or []:
            if not isinstance(record, dict):
                continue
            if claim_id := str(record.get("claim_id") or ""):
                protected["claim_ids"].add(claim_id)
            if family_id := str(record.get("claim_family_id") or ""):
                protected["claim_family_ids"].add(family_id)
            locator = record.get("locator") or record.get("projection_locator") or {}
            if isinstance(locator, dict):
                if page_key := _history_page_key(locator.get("page_key")):
                    protected["page_keys"].add(page_key)
        for record in change_set.get("proposed_evidence") or []:
            if not isinstance(record, dict):
                continue
            if evidence_id := str(record.get("evidence_id") or ""):
                protected["evidence_ids"].add(evidence_id)
            if family_id := str(record.get("evidence_family_id") or ""):
                protected["evidence_family_ids"].add(family_id)
            locator = record.get("locator") or record.get("projection_locator") or {}
            if isinstance(locator, dict):
                if page_key := _history_page_key(locator.get("page_key")):
                    protected["page_keys"].add(page_key)

    terminal_outbox = ",".join("?" for _ in _HISTORY_TERMINAL_OUTBOX_STATUSES)
    outbox_guard_expr = (
        "length(CAST(id AS TEXT)) + "
        "length(CAST(COALESCE(filename, '') AS BLOB)) + "
        "length(CAST(COALESCE(status, '') AS BLOB)) + "
        "length(CAST(COALESCE(lease_until, '') AS BLOB))"
    )
    active_outbox_rows = conn.execute(
        f"SELECT id, {outbox_guard_expr} AS guard_bytes "
        "FROM mutation_outbox "
        f"WHERE COALESCE(status, '') NOT IN ({terminal_outbox}) "
        "ORDER BY id LIMIT ?",
        (*_HISTORY_TERMINAL_OUTBOX_STATUSES, _HISTORY_ACTIVE_OUTBOX_MAX_ROWS + 1),
    ).fetchall()
    if len(active_outbox_rows) > _HISTORY_ACTIVE_OUTBOX_MAX_ROWS:
        protected["block_version_retention"].add("active_outbox_row_limit")
        protected["guard_parts"].add(
            f"active_outbox_row_limit:{_HISTORY_ACTIVE_OUTBOX_MAX_ROWS}"
        )
        active_outbox_rows = active_outbox_rows[:_HISTORY_ACTIVE_OUTBOX_MAX_ROWS]
    active_outbox_bytes = 0
    for metadata in active_outbox_rows:
        guard_bytes = int(metadata["guard_bytes"] or 0)
        if (
            guard_bytes < 0
            or active_outbox_bytes + guard_bytes > _HISTORY_ACTIVE_OUTBOX_MAX_BYTES
        ):
            protected["block_version_retention"].add("active_outbox_byte_limit")
            protected["guard_parts"].add(
                f"active_outbox_byte_limit:{_HISTORY_ACTIVE_OUTBOX_MAX_BYTES}"
            )
            break
        row = conn.execute(
            "SELECT id, filename, status, lease_until FROM mutation_outbox "
            f"WHERE id = ? AND ({outbox_guard_expr}) = ? "
            f"AND ({outbox_guard_expr}) <= ?",
            (metadata["id"], guard_bytes, _HISTORY_ACTIVE_OUTBOX_MAX_BYTES),
        ).fetchone()
        if row is None:
            protected["block_version_retention"].add("active_outbox_guard_drift")
            protected["guard_parts"].add(f"active_outbox_guard_drift:{metadata['id']}")
            break
        active_outbox_bytes += guard_bytes
        if page_key := _history_page_key(row["filename"]):
            protected["page_keys"].add(page_key)
        else:
            protected["block_version_retention"].add("missing_active_outbox_filename")
        protected["guard_parts"].add(
            "outbox:"
            + ":".join(
                str(row[field] or "")
                for field in ("id", "filename", "status", "lease_until")
            )
        )

    terminal_jobs = ",".join("?" for _ in _HISTORY_TERMINAL_JOB_STATUSES)
    job_guard_expr = (
        "length(CAST(COALESCE(job_id, '') AS BLOB)) + "
        "length(CAST(COALESCE(payload, '') AS BLOB)) + "
        "length(CAST(COALESCE(status, '') AS BLOB)) + "
        "length(CAST(COALESCE(lease_until, '') AS BLOB))"
    )
    rows = conn.execute(
        f"SELECT job_id, {job_guard_expr} AS guard_bytes "
        "FROM jobs WHERE status IS NULL OR "
        "(status = 'failed' AND COALESCE(retries, 0) < 3) OR "
        f"(status != 'failed' AND status NOT IN ({terminal_jobs})) "
        "ORDER BY job_id LIMIT ?",
        (*_HISTORY_TERMINAL_JOB_STATUSES, _HISTORY_ACTIVE_JOB_MAX_ROWS + 1),
    ).fetchall()
    if len(rows) > _HISTORY_ACTIVE_JOB_MAX_ROWS:
        protected["block_version_retention"].add("active_job_row_limit")
        protected["guard_parts"].add(
            f"active_job_row_limit:{_HISTORY_ACTIVE_JOB_MAX_ROWS}"
        )
        rows = rows[:_HISTORY_ACTIVE_JOB_MAX_ROWS]
    active_job_bytes = 0
    for metadata in rows:
        guard_bytes = int(metadata["guard_bytes"] or 0)
        if (
            guard_bytes < 0
            or active_job_bytes + guard_bytes > _HISTORY_ACTIVE_JOB_MAX_BYTES
        ):
            protected["block_version_retention"].add("active_job_byte_limit")
            protected["guard_parts"].add(
                f"active_job_byte_limit:{_HISTORY_ACTIVE_JOB_MAX_BYTES}"
            )
            break
        row = conn.execute(
            "SELECT job_id, payload, status, lease_until FROM jobs "
            f"WHERE job_id = ? AND ({job_guard_expr}) = ? "
            f"AND ({job_guard_expr}) <= ?",
            (
                metadata["job_id"],
                guard_bytes,
                _HISTORY_ACTIVE_JOB_MAX_BYTES,
            ),
        ).fetchone()
        if row is None:
            protected["block_version_retention"].add("active_job_guard_drift")
            protected["guard_parts"].add(
                f"active_job_guard_drift:job_id:{metadata['job_id']}"
            )
            break
        active_job_bytes += guard_bytes
        payload = _history_json_object(row["payload"])
        if not payload:
            protected["block_version_retention"].add("malformed_active_job_payload")
            continue
        protected["guard_parts"].add(
            "job:"
            + ":".join(
                str(row[field] or "") for field in ("job_id", "status", "lease_until")
            )
            + ":"
            + hashlib.sha256(str(row["payload"]).encode("utf-8")).hexdigest()
        )
        for field in ("canonical_name", "page_key"):
            if page_key := _history_page_key(payload.get(field)):
                protected["page_keys"].add(page_key)
    return protected


def _select_change_set_retention_candidates(
    conn: sqlite3.Connection,
    cutoff: str,
    batch_size: int,
    keep_latest: int,
) -> list[str]:
    queue_rows = conn.execute(
        "SELECT COALESCE(json_valid(data_json), 0) AS is_valid, "
        "CASE WHEN json_valid(data_json) "
        "THEN json_extract(data_json, '$.change_set_id') END AS change_set_id, "
        "CASE WHEN json_valid(data_json) "
        "THEN LOWER(COALESCE(json_extract(data_json, '$.status'), '')) "
        "END AS status FROM governance_queue"
    ).fetchall()
    if any(not int(row["is_valid"] or 0) for row in queue_rows):
        return []
    active_change_set_ids = {
        str(row["change_set_id"])
        for row in queue_rows
        if isinstance(row["change_set_id"], str)
        and str(row["status"] or "") not in _HISTORY_TERMINAL_QUEUE_STATUSES
    }
    scan_limit = int(batch_size) + len(active_change_set_ids)
    rows = conn.execute(
        "WITH terminal AS ("
        " SELECT lifecycle.change_set_id, lifecycle.terminal_at AS retained_at"
        " FROM change_set_lifecycle_v6 AS lifecycle "
        " WHERE lifecycle.status IN "
        " ('applied','cancelled','failed','published','rejected','superseded')"
        "), ranked AS ("
        " SELECT change_set_id, retained_at,"
        " ROW_NUMBER() OVER (ORDER BY retained_at DESC, change_set_id DESC) AS retain_rank"
        " FROM terminal"
        ") SELECT candidate.change_set_id FROM ranked AS candidate "
        "WHERE julianday(candidate.retained_at) IS NOT NULL "
        "AND julianday(candidate.retained_at) < julianday(?) "
        "AND candidate.retain_rank > ? "
        "ORDER BY candidate.retained_at ASC, candidate.change_set_id ASC LIMIT ?",
        (
            cutoff,
            keep_latest,
            scan_limit,
        ),
    ).fetchall()
    return [
        str(row["change_set_id"])
        for row in rows
        if str(row["change_set_id"]) not in active_change_set_ids
    ][:batch_size]


def _select_job_retention_candidates(
    conn: sqlite3.Connection,
    cutoff: str,
    batch_size: int,
    keep_latest: int,
) -> list[str]:
    rows = conn.execute(
        "WITH terminal AS ("
        " SELECT job_id, task_packet_path, completed_at AS retained_at"
        " FROM jobs WHERE "
        " status IN ('finalized', 'completed', 'cancelled', 'superseded') "
        " OR (status = 'failed' AND COALESCE(retries, 0) >= 3)"
        "), ranked AS ("
        " SELECT job_id, task_packet_path, retained_at,"
        " ROW_NUMBER() OVER (ORDER BY retained_at DESC, job_id DESC) AS retain_rank"
        " FROM terminal"
        ") SELECT candidate.job_id FROM ranked AS candidate "
        "WHERE julianday(candidate.retained_at) IS NOT NULL "
        "AND julianday(candidate.retained_at) < julianday(?) "
        "AND candidate.retain_rank > ? "
        "AND COALESCE(candidate.task_packet_path, '') = '' "
        "AND NOT EXISTS ("
        " SELECT 1 FROM ingest_task_cleanup AS cleanup "
        " WHERE cleanup.job_id = candidate.job_id AND cleanup.status != 'completed'"
        ") ORDER BY candidate.retained_at ASC, candidate.job_id ASC LIMIT ?",
        (cutoff, keep_latest, batch_size),
    ).fetchall()
    return [str(row["job_id"]) for row in rows]


def _select_outbox_retention_candidates(
    conn: sqlite3.Connection,
    cutoff: str,
    batch_size: int,
    keep_latest: int,
) -> list[int]:
    terminal = ",".join("?" for _ in _HISTORY_TERMINAL_OUTBOX_STATUSES)
    rows = conn.execute(
        "WITH terminal AS ("
        " SELECT id, completed_at AS retained_at"
        " FROM mutation_outbox "
        f" WHERE status IN ({terminal})"
        "), ranked AS ("
        " SELECT id, retained_at,"
        " ROW_NUMBER() OVER (ORDER BY retained_at DESC, id DESC) AS retain_rank"
        " FROM terminal"
        ") SELECT candidate.id FROM ranked AS candidate "
        "WHERE julianday(candidate.retained_at) IS NOT NULL "
        "AND julianday(candidate.retained_at) < julianday(?) "
        "AND candidate.retain_rank > ? "
        "AND NOT EXISTS ("
        " SELECT 1 FROM mutation_outbox AS active "
        " WHERE active.superseded_by = candidate.id "
        f" AND COALESCE(active.status, '') NOT IN ({terminal})"
        ") ORDER BY candidate.retained_at ASC, candidate.id ASC LIMIT ?",
        (
            *_HISTORY_TERMINAL_OUTBOX_STATUSES,
            cutoff,
            keep_latest,
            *_HISTORY_TERMINAL_OUTBOX_STATUSES,
            batch_size,
        ),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _select_version_retention_candidates(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    version_id_field: str,
    id_field: str,
    family_field: str,
    canonical_table: str,
    cutoff: str,
    batch_size: int,
    keep_per_family: int,
    protected: dict[str, set[str]],
    cursor: str = "",
) -> tuple[list[str], dict[str, object], list[tuple[str, str | None]]]:
    allowed = {
        (
            "claim_versions",
            "claim_version_id",
            "claim_id",
            "claim_family_id",
            "claims",
            "idx_claim_versions_retention_v6",
        ),
        (
            "evidence_versions",
            "evidence_version_id",
            "evidence_id",
            "evidence_family_id",
            "evidence",
            "idx_evidence_versions_retention_v6",
        ),
    }
    retention_index = (
        "idx_claim_versions_retention_v6"
        if table_name == "claim_versions"
        else "idx_evidence_versions_retention_v6"
    )
    identity = (
        table_name,
        version_id_field,
        id_field,
        family_field,
        canonical_table,
        retention_index,
    )
    if identity not in allowed:
        raise ValueError(f"Unsupported history table: {table_name}")
    normalized_cursor = str(cursor or "")
    if (
        "\x00" in normalized_cursor
        or len(normalized_cursor.encode("utf-8")) > _HISTORY_VERSION_MAX_CURSOR_BYTES
    ):
        raise ValueError(f"{table_name} retention cursor is malformed")
    scan_limit = min(
        _HISTORY_VERSION_MAX_SCAN_ROWS,
        max(_HISTORY_VERSION_MIN_SCAN_ROWS, batch_size * 8),
    )
    skipped: dict[str, object] = {
        "active_work": 0,
        "current_canonical": 0,
        "malformed_canonical": 0,
        "oversize_canonical": 0,
        "invalid_business_time": 0,
        "not_expired": 0,
        "newest_family_versions": 0,
        "scanned": 0,
        "scanned_rows": 0,
        "scan_limit": scan_limit,
        "scan_truncated": False,
        "input_cursor": normalized_cursor,
        "last_scanned_cursor": normalized_cursor,
        "blocked_by_unknown_active_work": 0,
    }
    if protected["block_version_retention"]:
        skipped["blocked_by_unknown_active_work"] = len(
            protected["block_version_retention"]
        )
        return [], skipped, []
    # SQLite orders storage classes as NULL/numeric < TEXT < BLOB.  Checking
    # both indexed key boundaries therefore detects every non-TEXT storage
    # class without rescanning the whole history table on every batch.
    for direction in ("ASC", "DESC"):
        boundary_row = conn.execute(
            f"SELECT {version_id_field} AS version_id, "
            f"typeof({version_id_field}) AS storage_class FROM {table_name} "
            f"ORDER BY {version_id_field} {direction} LIMIT 1"
        ).fetchone()
        if boundary_row is not None and (
            str(boundary_row["storage_class"]) != "text"
            or not isinstance(boundary_row["version_id"], str)
            or not boundary_row["version_id"]
        ):
            raise RuntimeError(
                f"{table_name} contains an unresumable version identifier"
            )
    raw_rows = conn.execute(
        f"SELECT {version_id_field}, {id_field}, {family_field}, page_key, "
        f"record_hash, recorded_at, version_no FROM {table_name} "
        f"WHERE {version_id_field} > ? "
        f"ORDER BY {version_id_field} LIMIT ?",
        (normalized_cursor, scan_limit + 1),
    ).fetchall()
    more_after_scan_window = len(raw_rows) > scan_limit
    rows = raw_rows[:scan_limit]
    protected_ids = protected[f"{id_field}s"]
    protected_families = protected[f"{family_field}s"]
    protected_pages = protected["page_keys"]
    selected: list[str] = []
    trace: list[tuple[str, str | None]] = []
    cutoff_dt = datetime.fromisoformat(cutoff)
    for row in rows:
        raw_version_id = row[version_id_field]
        if not isinstance(raw_version_id, str) or not raw_version_id:
            raise RuntimeError(
                f"{table_name} contains an unresumable version identifier"
            )
        version_id = raw_version_id
        if (
            "\x00" in version_id
            or len(version_id.encode("utf-8")) > _HISTORY_VERSION_MAX_CURSOR_BYTES
        ):
            raise RuntimeError(
                f"{table_name} contains an unresumable version identifier"
            )
        skipped["scanned"] = int(skipped["scanned"]) + 1
        skipped["scanned_rows"] = int(skipped["scanned_rows"]) + 1
        skipped["last_scanned_cursor"] = version_id
        recorded_at = _strict_utc_instant(row["recorded_at"])
        if recorded_at is None:
            skipped["invalid_business_time"] = int(skipped["invalid_business_time"]) + 1
            trace.append((version_id, None))
            continue
        if datetime.fromisoformat(recorded_at) >= cutoff_dt:
            skipped["not_expired"] = int(skipped["not_expired"]) + 1
            trace.append((version_id, None))
            continue
        page_key = _history_page_key(row["page_key"])
        if (
            str(row[id_field]) in protected_ids
            or str(row[family_field]) in protected_families
            or (page_key and page_key in protected_pages)
        ):
            skipped["active_work"] = int(skipped["active_work"]) + 1
            trace.append((version_id, None))
            continue
        newest_rows = conn.execute(
            f"SELECT {version_id_field} FROM {table_name} "
            f"INDEXED BY {retention_index} WHERE {family_field} = ? "
            f"ORDER BY version_no DESC, recorded_at DESC, "
            f"{version_id_field} DESC LIMIT ?",
            (row[family_field], keep_per_family),
        ).fetchall()
        if version_id in {str(newest[version_id_field]) for newest in newest_rows}:
            skipped["newest_family_versions"] = (
                int(skipped["newest_family_versions"]) + 1
            )
            trace.append((version_id, None))
            continue
        canonical_metadata = conn.execute(
            f"SELECT length(CAST(data_json AS BLOB)) AS data_bytes "
            f"FROM {canonical_table} WHERE {id_field} = ?",
            (row[id_field],),
        ).fetchone()
        if canonical_metadata is not None:
            canonical_bytes = int(canonical_metadata["data_bytes"] or 0)
            if (
                canonical_bytes < 0
                or canonical_bytes > _HISTORY_CANONICAL_GUARD_MAX_BYTES
            ):
                skipped["oversize_canonical"] = int(skipped["oversize_canonical"]) + 1
                trace.append((version_id, None))
                continue
            canonical_row = conn.execute(
                f"SELECT data_json FROM {canonical_table} "
                f"WHERE {id_field} = ? "
                "AND length(CAST(data_json AS BLOB)) = ? "
                "AND length(CAST(data_json AS BLOB)) <= ?",
                (
                    row[id_field],
                    canonical_bytes,
                    _HISTORY_CANONICAL_GUARD_MAX_BYTES,
                ),
            ).fetchone()
            if canonical_row is None:
                skipped["malformed_canonical"] = int(skipped["malformed_canonical"]) + 1
                trace.append((version_id, None))
                continue
            current_data = canonical_row["data_json"]
            current_record = _history_json_object(current_data)
            if not current_record:
                skipped["malformed_canonical"] = int(skipped["malformed_canonical"]) + 1
                trace.append((version_id, None))
                continue
            current_hash = hashlib.sha256(
                _canonical_record_json(current_record).encode("utf-8")
            ).hexdigest()
            if current_hash == str(row["record_hash"] or ""):
                skipped["current_canonical"] = int(skipped["current_canonical"]) + 1
                trace.append((version_id, None))
                continue
        selected.append(version_id)
        trace.append((version_id, version_id))
        if len(selected) >= batch_size:
            break
    skipped["scan_truncated"] = bool(
        more_after_scan_window or int(skipped["scanned_rows"]) < len(rows)
    )
    return selected, skipped, trace


_HISTORY_GENERATION_SURFACES = (
    "change_sets",
    "governance_queue",
    "jobs",
    "mutation_outbox",
    "claim_versions",
    "evidence_versions",
    "claims",
    "evidence",
)


def _history_runtime_generations(conn: sqlite3.Connection) -> dict[str, int]:
    placeholders = ",".join("?" for _ in _HISTORY_GENERATION_SURFACES)
    rows = conn.execute(
        "SELECT surface, generation FROM runtime_generations "
        f"WHERE surface IN ({placeholders}) ORDER BY surface",
        _HISTORY_GENERATION_SURFACES,
    ).fetchall()
    generations = {str(row["surface"]): int(row["generation"]) for row in rows}
    missing = sorted(set(_HISTORY_GENERATION_SURFACES) - set(generations))
    if missing:
        raise RuntimeError(
            "History retention generation registry is incomplete: " + ", ".join(missing)
        )
    return generations


def _history_protection_digest(protected: dict[str, set[str]]) -> str:
    canonical = {
        name: sorted(str(value) for value in values)
        for name, values in sorted(protected.items())
    }
    return hashlib.sha256(_canonical_json_bytes(canonical)).hexdigest()


def _history_candidate_metadata(
    conn: sqlite3.Connection,
    table_name: str,
    key: object,
) -> dict | None:
    if table_name == "change_sets":
        row = conn.execute(
            "SELECT lifecycle.terminal_at AS business_at, "
            "length(CAST(change_sets.data_json AS BLOB)) + COALESCE(("
            "SELECT payload.stored_bytes FROM change_set_payload_refs AS ref "
            "JOIN change_set_payloads AS payload "
            "ON payload.payload_sha256 = ref.payload_sha256 "
            "WHERE ref.change_set_id = change_sets.change_set_id), 0) "
            "AS logical_bytes "
            "FROM change_sets JOIN change_set_lifecycle_v6 AS lifecycle "
            "ON lifecycle.change_set_id = change_sets.change_set_id "
            "WHERE change_sets.change_set_id = ?",
            (key,),
        ).fetchone()
    elif table_name == "jobs":
        row = conn.execute(
            "SELECT completed_at AS business_at, "
            "length(CAST(COALESCE(payload,'') AS BLOB)) + "
            "length(CAST(COALESCE(result_json,'') AS BLOB)) + "
            "length(CAST(COALESCE(error_msg,'') AS BLOB)) + 256 AS logical_bytes "
            "FROM jobs WHERE job_id = ?",
            (key,),
        ).fetchone()
    elif table_name == "mutation_outbox":
        row = conn.execute(
            "SELECT completed_at AS business_at, "
            "length(CAST(COALESCE(payload_text,'') AS BLOB)) + 256 AS logical_bytes "
            "FROM mutation_outbox WHERE id = ?",
            (key,),
        ).fetchone()
    elif table_name == "claim_versions":
        row = conn.execute(
            "SELECT recorded_at AS business_at, "
            "length(CAST(COALESCE(data_json,'') AS BLOB)) + 256 AS logical_bytes "
            "FROM claim_versions WHERE claim_version_id = ?",
            (key,),
        ).fetchone()
    elif table_name == "evidence_versions":
        row = conn.execute(
            "SELECT recorded_at AS business_at, "
            "length(CAST(COALESCE(data_json,'') AS BLOB)) + 256 AS logical_bytes "
            "FROM evidence_versions WHERE evidence_version_id = ?",
            (key,),
        ).fetchone()
    else:
        raise ValueError(f"Unsupported history candidate table: {table_name}")
    if row is None:
        return None
    return {
        "table": table_name,
        "key": key,
        "business_at": row["business_at"],
        "logical_bytes": int(row["logical_bytes"] or 0),
    }


def _history_guarded_row(
    conn: sqlite3.Connection,
    table_name: str,
    key: object,
) -> dict | None:
    if table_name == "change_sets":
        row = conn.execute(
            "SELECT change_sets.rowid AS storage_rowid, change_sets.change_set_id, "
            "length(CAST(change_sets.data_json AS BLOB)) AS data_bytes, "
            "change_sets.updated_at, lifecycle.status, lifecycle.created_at, "
            "lifecycle.terminal_at, lifecycle.time_source, "
            "lifecycle.payload_guard_sha256 "
            "FROM change_sets JOIN change_set_lifecycle_v6 AS lifecycle "
            "ON lifecycle.change_set_id = change_sets.change_set_id "
            "WHERE change_sets.change_set_id = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        guarded = dict(row)
        storage_rowid = int(guarded.pop("storage_rowid"))
        observed_guard = _sqlite_text_blob_sha256(
            conn,
            table_name="change_sets",
            column_name="data_json",
            rowid=storage_rowid,
            expected_bytes=int(row["data_bytes"] or 0),
        )
        if not hmac.compare_digest(
            observed_guard,
            str(row["payload_guard_sha256"] or ""),
        ):
            raise RuntimeError(
                f"Change-set lifecycle guard drifted: {row['change_set_id']}"
            )
        guarded["observed_data_sha256"] = observed_guard
        return guarded
    elif table_name == "jobs":
        row = conn.execute(
            "SELECT job_id, task_type, payload, status, retries, error_msg, "
            "created_at, updated_at, available_at, lease_until, lease_owner, "
            "lease_token, lease_generation, idempotency_key, task_packet_path, "
            "completed_at, result_json FROM jobs WHERE job_id = ?",
            (key,),
        ).fetchone()
    elif table_name == "mutation_outbox":
        row = conn.execute(
            "SELECT id, filename, mutation_type, payload_text, status, created_at, "
            "attempt_count, last_error, available_at, started_at, completed_at, "
            "superseded_by, lease_until, lease_owner, lease_token, lease_generation, "
            "idempotency_key, validation_mode, base_version, projection_base_hash "
            "FROM mutation_outbox WHERE id = ?",
            (key,),
        ).fetchone()
    elif table_name == "claim_versions":
        row = conn.execute(
            "SELECT claim_version_id, claim_id, claim_family_id, page_key, "
            "data_json, record_hash, recorded_at, version_no "
            "FROM claim_versions WHERE claim_version_id = ?",
            (key,),
        ).fetchone()
    elif table_name == "evidence_versions":
        row = conn.execute(
            "SELECT evidence_version_id, evidence_id, evidence_family_id, page_key, "
            "data_json, record_hash, recorded_at, version_no "
            "FROM evidence_versions WHERE evidence_version_id = ?",
            (key,),
        ).fetchone()
    else:
        raise ValueError(f"Unsupported history candidate table: {table_name}")
    return dict(row) if row is not None else None


def _history_row_guard(row: dict) -> str:
    return hashlib.sha256(_canonical_json_bytes(row)).hexdigest()


def _history_plan_fingerprint(plan: dict) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "fingerprint"}
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()


def _safe_version_resume_cursor(
    *,
    input_cursor: str,
    stats: dict[str, object],
    trace: list[tuple[str, str | None]],
    finally_selected: set[str],
) -> str:
    """Advance only across a continuous prefix safe after this exact apply."""
    if int(stats.get("blocked_by_unknown_active_work") or 0):
        return input_cursor
    safe_cursor = input_cursor
    for scanned_cursor, eligible_id in trace:
        if eligible_id is not None and eligible_id not in finally_selected:
            break
        safe_cursor = scanned_cursor
    # Keep the last scanned key at end-of-range.  An empty cursor means
    # "start from the beginning", so clearing it here would wrap the next
    # batch back to the first row instead of producing stable exhaustion.
    return safe_cursor


def _version_cursor_policy(
    *,
    plan_as_of: str,
    cutoff: str,
    batch_size: int,
    max_delete_bytes: int,
    keep_change_sets: int,
    keep_terminal_jobs: int,
    keep_terminal_outbox: int,
    keep_versions_per_family: int,
    scan_version_history: bool,
) -> dict[str, object]:
    return {
        "cursor_semantics": "last-safe-key-v2",
        "version_id_invariant": ("storage-class-text-nonempty-no-nul-max1024-utf8-v2"),
        "plan_as_of": plan_as_of,
        "cutoff": cutoff,
        "max_delete_rows": int(batch_size),
        "max_delete_bytes": int(max_delete_bytes),
        "keep_change_sets": int(keep_change_sets),
        "keep_terminal_jobs": int(keep_terminal_jobs),
        "keep_terminal_outbox": int(keep_terminal_outbox),
        "keep_versions_per_family": int(keep_versions_per_family),
        "scan_version_history": bool(scan_version_history),
    }


def _version_cursor_policy_sha256(policy: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(policy)).hexdigest()


def _validate_version_cursor_receipt(
    conn: sqlite3.Connection,
    *,
    claim_version_cursor: str,
    evidence_version_cursor: str,
    version_cursor_receipt: str,
    cursor_policy: dict[str, object],
) -> None:
    cursors = {
        "claim_versions": str(claim_version_cursor or ""),
        "evidence_versions": str(evidence_version_cursor or ""),
    }
    receipt_fingerprint = str(version_cursor_receipt or "")
    if not receipt_fingerprint:
        if any(cursors.values()):
            raise ValueError(
                "Version retention cursors require the successful prior receipt fingerprint"
            )
        return
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt_fingerprint):
        raise ValueError("Version retention cursor receipt fingerprint is malformed")
    row = conn.execute(
        "SELECT receipt_json FROM history_retention_runs_v6 WHERE fingerprint = ?",
        (receipt_fingerprint,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Version retention cursor receipt does not exist")
    receipt = _history_json_object(row["receipt_json"])
    if not receipt or not hmac.compare_digest(
        str(receipt.get("fingerprint") or ""), receipt_fingerprint
    ):
        raise RuntimeError("Version retention cursor receipt is malformed")
    receipt_cursors = receipt.get("version_resume_cursors")
    if (
        not isinstance(receipt_cursors, dict)
        or {
            table_name: str(receipt_cursors.get(table_name) or "")
            for table_name in cursors
        }
        != cursors
    ):
        raise RuntimeError("Version retention cursors do not match the prior receipt")
    receipt_policy = receipt.get("version_cursor_policy")
    receipt_policy_sha256 = str(receipt.get("version_cursor_policy_sha256") or "")
    if (
        not isinstance(receipt_policy, dict)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt_policy_sha256)
        or not hmac.compare_digest(
            _version_cursor_policy_sha256(receipt_policy),
            receipt_policy_sha256,
        )
        or receipt_policy != cursor_policy
    ):
        raise RuntimeError(
            "Version retention cursor policy does not match the prior receipt; "
            "restart the campaign from empty cursors"
        )
    receipt_generations = receipt.get("runtime_generations_after")
    if not isinstance(receipt_generations, dict) or receipt_generations != (
        _history_runtime_generations(conn)
    ):
        raise RuntimeError(
            "Version retention runtime generations drifted after the prior receipt; "
            "restart the campaign from empty cursors"
        )


def plan_history_retention(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    batch_size: int = 500,
    max_delete_bytes: int = 128 * 1024 * 1024,
    keep_change_sets: int = 1000,
    keep_terminal_jobs: int = 1000,
    keep_terminal_outbox: int = 1000,
    keep_versions_per_family: int = 2,
    claim_version_cursor: str = "",
    evidence_version_cursor: str = "",
    version_cursor_receipt: str = "",
    scan_version_history: bool = True,
    plan_as_of: str | None = None,
) -> dict:
    """Select bounded history rows without mutating the supplied database."""
    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size must be between 1 and 500")
    if max_delete_bytes < 1 or max_delete_bytes > _HISTORY_RETENTION_MAX_DELETE_BYTES:
        raise ValueError("max_delete_bytes must be between 1 byte and 128 MiB")
    for name, value in (
        ("keep_change_sets", keep_change_sets),
        ("keep_terminal_jobs", keep_terminal_jobs),
        ("keep_terminal_outbox", keep_terminal_outbox),
    ):
        if int(value) < 0:
            raise ValueError(f"{name} must be zero or positive")
    if (
        keep_versions_per_family < 1
        or keep_versions_per_family > _HISTORY_VERSION_MAX_KEEP_PER_FAMILY
    ):
        raise ValueError(
            "keep_versions_per_family must be between 1 and "
            f"{_HISTORY_VERSION_MAX_KEEP_PER_FAMILY}"
        )
    normalized_as_of = _strict_utc_instant(plan_as_of or _utc_now())
    normalized_cutoff = _strict_utc_instant(cutoff)
    if normalized_as_of is None or normalized_cutoff is None:
        raise ValueError("plan_as_of and cutoff must be timezone-aware ISO-8601")
    cursor_policy = _version_cursor_policy(
        plan_as_of=normalized_as_of,
        cutoff=normalized_cutoff,
        batch_size=batch_size,
        max_delete_bytes=max_delete_bytes,
        keep_change_sets=keep_change_sets,
        keep_terminal_jobs=keep_terminal_jobs,
        keep_terminal_outbox=keep_terminal_outbox,
        keep_versions_per_family=keep_versions_per_family,
        scan_version_history=scan_version_history,
    )

    if not scan_version_history and (
        claim_version_cursor or evidence_version_cursor or version_cursor_receipt
    ):
        raise ValueError("Disabled version retention cannot accept version cursors")
    if scan_version_history:
        _validate_version_cursor_receipt(
            conn,
            claim_version_cursor=claim_version_cursor,
            evidence_version_cursor=evidence_version_cursor,
            version_cursor_receipt=version_cursor_receipt,
            cursor_policy=cursor_policy,
        )

    protected = _history_active_protections(conn)
    proposed_ids: dict[str, list] = {
        "change_sets": _select_change_set_retention_candidates(
            conn,
            normalized_cutoff,
            batch_size,
            int(keep_change_sets),
        ),
        "jobs": _select_job_retention_candidates(
            conn,
            normalized_cutoff,
            batch_size,
            int(keep_terminal_jobs),
        ),
        "mutation_outbox": _select_outbox_retention_candidates(
            conn,
            normalized_cutoff,
            batch_size,
            int(keep_terminal_outbox),
        ),
    }
    if scan_version_history:
        claim_versions, claim_skipped, claim_trace = (
            _select_version_retention_candidates(
                conn,
                table_name="claim_versions",
                version_id_field="claim_version_id",
                id_field="claim_id",
                family_field="claim_family_id",
                canonical_table="claims",
                cutoff=normalized_cutoff,
                batch_size=batch_size,
                keep_per_family=int(keep_versions_per_family),
                protected=protected,
                cursor=claim_version_cursor,
            )
        )
        evidence_versions, evidence_skipped, evidence_trace = (
            _select_version_retention_candidates(
                conn,
                table_name="evidence_versions",
                version_id_field="evidence_version_id",
                id_field="evidence_id",
                family_field="evidence_family_id",
                canonical_table="evidence",
                cutoff=normalized_cutoff,
                batch_size=batch_size,
                keep_per_family=int(keep_versions_per_family),
                protected=protected,
                cursor=evidence_version_cursor,
            )
        )
    else:
        disabled_stats: dict[str, object] = {
            "disabled_by_scope": 1,
            "scanned": 0,
            "scanned_rows": 0,
            "scan_limit": 0,
            "scan_truncated": False,
            "input_cursor": "",
            "last_scanned_cursor": "",
            "blocked_by_unknown_active_work": 0,
        }
        claim_versions, claim_skipped, claim_trace = [], dict(disabled_stats), []
        evidence_versions, evidence_skipped, evidence_trace = (
            [],
            dict(disabled_stats),
            [],
        )
    proposed_ids["claim_versions"] = claim_versions
    proposed_ids["evidence_versions"] = evidence_versions
    metadata: list[dict] = []
    missing_rows = 0
    invalid_business_time: dict[str, int] = {
        table_name: 0 for table_name in _HISTORY_RETENTION_TABLES
    }
    for table_name in _HISTORY_RETENTION_TABLES:
        for key in proposed_ids.get(table_name, []):
            candidate = _history_candidate_metadata(conn, table_name, key)
            if candidate is None:
                missing_rows += 1
                continue
            normalized_business_at = _strict_utc_instant(candidate["business_at"])
            if normalized_business_at is None:
                invalid_business_time[table_name] += 1
                continue
            candidate["business_at"] = normalized_business_at
            metadata.append(candidate)
    metadata.sort(
        key=lambda item: (
            item["business_at"],
            item["table"],
            str(item["key"]),
        )
    )
    candidates: list[dict] = []
    selected_bytes = 0
    oversize_candidates: list[dict] = []
    for candidate in metadata:
        logical_bytes = max(0, int(candidate["logical_bytes"]))
        if logical_bytes > max_delete_bytes:
            oversize_candidates.append(
                {
                    "table": candidate["table"],
                    "key": candidate["key"],
                    "logical_bytes": logical_bytes,
                }
            )
            continue
        if len(candidates) >= batch_size:
            break
        if selected_bytes + logical_bytes > max_delete_bytes:
            continue
        guarded_row = _history_guarded_row(
            conn,
            candidate["table"],
            candidate["key"],
        )
        if guarded_row is None:
            missing_rows += 1
            continue
        candidate["row_guard_sha256"] = _history_row_guard(guarded_row)
        candidates.append(candidate)
        selected_bytes += logical_bytes
    selected = {table_name: [] for table_name in _HISTORY_RETENTION_TABLES}
    for candidate in candidates:
        selected[candidate["table"]].append(candidate["key"])
    version_scan = {}
    for table_name, input_cursor, eligible_ids, stats, trace in (
        (
            "claim_versions",
            str(claim_version_cursor or ""),
            claim_versions,
            claim_skipped,
            claim_trace,
        ),
        (
            "evidence_versions",
            str(evidence_version_cursor or ""),
            evidence_versions,
            evidence_skipped,
            evidence_trace,
        ),
    ):
        finally_selected = {str(value) for value in selected.get(table_name, [])}
        scan_stats = dict(stats)
        scan_stats["eligible_rows"] = len(eligible_ids)
        scan_stats["scheduled_rows"] = len(finally_selected)
        scan_stats["safe_next_cursor"] = _safe_version_resume_cursor(
            input_cursor=input_cursor,
            stats=stats,
            trace=trace,
            finally_selected=finally_selected,
        )
        version_scan[table_name] = scan_stats
    table_counts = {
        table_name: int(
            conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        )
        for table_name in _HISTORY_RETENTION_TABLES
    }
    plan = {
        "contract": "history-retention-plan-v2",
        "schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "schema_cookie": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
        "plan_as_of": normalized_as_of,
        "cutoff": normalized_cutoff,
        "version_cursor_policy_sha256": _version_cursor_policy_sha256(cursor_policy),
        "rules": {
            "max_delete_rows": batch_size,
            "max_delete_bytes": int(max_delete_bytes),
            "keep_change_sets": int(keep_change_sets),
            "keep_terminal_jobs": int(keep_terminal_jobs),
            "keep_terminal_outbox": int(keep_terminal_outbox),
            "keep_versions_per_family": int(keep_versions_per_family),
            "claim_version_cursor": str(claim_version_cursor or ""),
            "evidence_version_cursor": str(evidence_version_cursor or ""),
            "version_cursor_receipt": str(version_cursor_receipt or ""),
            "scan_version_history": bool(scan_version_history),
        },
        "table_counts_before": table_counts,
        "runtime_generations": _history_runtime_generations(conn),
        "active_protection_sha256": _history_protection_digest(protected),
        "candidates": candidates,
        "selected_count_total": len(candidates),
        "selected_bytes_total": selected_bytes,
        "selected_ids": selected,
        "selected_counts": {
            table_name: len(selected.get(table_name, []))
            for table_name in _HISTORY_RETENTION_TABLES
        },
        "active_protection_counts": {
            name: len(values) for name, values in protected.items()
        },
        "candidate_skip_counts": {
            "missing_rows": missing_rows,
            "invalid_business_time": invalid_business_time,
            "oversize_candidates": len(oversize_candidates),
        },
        "oversize_candidate_samples": oversize_candidates[:20],
        "version_skip_counts": {
            "claim_versions": claim_skipped,
            "evidence_versions": evidence_skipped,
        },
        "version_scan": version_scan,
    }
    plan["fingerprint"] = _history_plan_fingerprint(plan)
    return plan


def _delete_history_ids(
    conn: sqlite3.Connection,
    table_name: str,
    key_name: str,
    values: list,
    *,
    extra_predicate: str = "",
) -> int:
    if table_name not in {
        "change_sets",
        "change_set_idempotency",
        "jobs",
        "ingest_task_cleanup",
        "mutation_outbox",
        "claim_versions",
        "evidence_versions",
    }:
        raise ValueError(f"Unsupported history deletion table: {table_name}")
    deleted = 0
    for offset in range(0, len(values), 400):
        chunk = values[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        cursor = conn.execute(
            f"DELETE FROM {table_name} WHERE {key_name} IN ({placeholders}) "
            f"{extra_predicate}",
            tuple(chunk),
        )
        deleted += int(cursor.rowcount or 0)
    return deleted


def apply_history_retention_plan(
    conn: sqlite3.Connection,
    plan: dict,
    *,
    confirmation: str = "",
    plan_as_of: str = "",
) -> dict[str, int]:
    """Apply one exact, fingerprint-confirmed history batch and durable receipt."""
    if not conn.in_transaction:
        raise RuntimeError("History retention apply requires an active transaction")
    if plan.get("contract") != "history-retention-plan-v2":
        raise ValueError("Unsupported history retention plan contract")
    fingerprint = str(plan.get("fingerprint") or "")
    if not fingerprint or not hmac.compare_digest(
        fingerprint,
        _history_plan_fingerprint(plan),
    ):
        raise RuntimeError("History retention plan fingerprint is invalid")
    if not confirmation or not hmac.compare_digest(str(confirmation), fingerprint):
        raise RuntimeError("History retention requires the exact preview fingerprint")
    normalized_as_of = _strict_utc_instant(plan_as_of)
    if normalized_as_of is None or not hmac.compare_digest(
        normalized_as_of,
        str(plan.get("plan_as_of") or ""),
    ):
        raise RuntimeError("History retention plan_as_of does not match the preview")

    rules = plan.get("rules")
    candidates = plan.get("candidates")
    if not isinstance(rules, dict) or not isinstance(candidates, list):
        raise ValueError("History retention plan rules or candidates are missing")
    try:
        max_delete_rows = int(rules.get("max_delete_rows"))
        max_delete_bytes = int(rules.get("max_delete_bytes"))
    except (TypeError, ValueError) as exc:
        raise ValueError("History retention plan bounds are malformed") from exc
    if max_delete_rows < 1 or max_delete_rows > 500:
        raise ValueError("History retention row bound is outside the hard limit")
    if max_delete_bytes < 1 or max_delete_bytes > _HISTORY_RETENTION_MAX_DELETE_BYTES:
        raise ValueError("History retention byte bound is outside the hard limit")
    if not isinstance(rules.get("scan_version_history"), bool):
        raise ValueError("History retention version-scan policy is malformed")
    try:
        cursor_policy = _version_cursor_policy(
            plan_as_of=str(plan["plan_as_of"]),
            cutoff=str(plan["cutoff"]),
            batch_size=max_delete_rows,
            max_delete_bytes=max_delete_bytes,
            keep_change_sets=int(rules["keep_change_sets"]),
            keep_terminal_jobs=int(rules["keep_terminal_jobs"]),
            keep_terminal_outbox=int(rules["keep_terminal_outbox"]),
            keep_versions_per_family=int(rules["keep_versions_per_family"]),
            scan_version_history=rules["scan_version_history"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("History retention cursor policy is malformed") from exc
    if not hmac.compare_digest(
        str(plan.get("version_cursor_policy_sha256") or ""),
        _version_cursor_policy_sha256(cursor_policy),
    ):
        raise RuntimeError("History retention cursor policy drifted after preview")
    if len(candidates) > max_delete_rows:
        raise ValueError("History retention candidates exceed the row bound")
    candidate_bytes = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("History retention candidate is malformed")
        try:
            logical_bytes = int(candidate.get("logical_bytes"))
        except (TypeError, ValueError) as exc:
            raise ValueError("History retention candidate size is malformed") from exc
        if logical_bytes < 0 or logical_bytes > max_delete_bytes:
            raise ValueError("History retention candidate exceeds the byte bound")
        candidate_bytes += logical_bytes
    if candidate_bytes > max_delete_bytes:
        raise ValueError("History retention candidates exceed the batch byte bound")
    if (
        int(plan.get("selected_count_total") or 0) != len(candidates)
        or int(plan.get("selected_bytes_total") or 0) != candidate_bytes
    ):
        raise ValueError("History retention plan totals are inconsistent")

    prior = conn.execute(
        "SELECT plan_sha256, receipt_json FROM history_retention_runs_v6 "
        "WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    if prior is not None:
        if not hmac.compare_digest(str(prior["plan_sha256"]), fingerprint[7:]):
            raise RuntimeError("History retention receipt plan digest is invalid")
        receipt = _history_json_object(prior["receipt_json"])
        deleted = receipt.get("deleted_counts")
        if not isinstance(deleted, dict):
            raise RuntimeError("History retention receipt is malformed")
        return {str(key): int(value) for key, value in deleted.items()}

    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != int(
        plan.get("schema_version") or -1
    ):
        raise RuntimeError("History retention schema version drifted after preview")
    if int(conn.execute("PRAGMA schema_version").fetchone()[0]) != int(
        plan.get("schema_cookie") or -1
    ):
        raise RuntimeError("History retention schema cookie drifted after preview")
    if _history_runtime_generations(conn) != plan.get("runtime_generations"):
        raise RuntimeError(
            "History retention runtime generations drifted after preview"
        )
    protected = _history_active_protections(conn)
    if not hmac.compare_digest(
        _history_protection_digest(protected),
        str(plan.get("active_protection_sha256") or ""),
    ):
        raise RuntimeError("History retention active references drifted after preview")

    deleted_counts = {table_name: 0 for table_name in _HISTORY_RETENTION_TABLES}
    payload_hashes: set[str] = set()
    guarded_rows: list[tuple[dict, dict]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("History retention candidate is malformed")
        table_name = str(candidate.get("table") or "")
        if table_name not in _HISTORY_RETENTION_TABLES:
            raise ValueError(f"Unsupported history plan table: {table_name}")
        guarded_row = _history_guarded_row(conn, table_name, candidate.get("key"))
        if guarded_row is None or not hmac.compare_digest(
            _history_row_guard(guarded_row),
            str(candidate.get("row_guard_sha256") or ""),
        ):
            raise RuntimeError(
                "History retention row changed after preview: "
                f"{table_name}:{candidate.get('key')}"
            )
        guarded_rows.append((candidate, guarded_row))

    for candidate, row in guarded_rows:
        table_name = str(candidate["table"])
        key = candidate["key"]
        if table_name == "change_sets":
            ref = conn.execute(
                "SELECT payload_sha256 FROM change_set_payload_refs "
                "WHERE change_set_id = ?",
                (key,),
            ).fetchone()
            if ref is not None:
                payload_hashes.add(str(ref["payload_sha256"]))
            conn.execute(
                "DELETE FROM change_set_idempotency WHERE change_set_id = ?",
                (key,),
            )
            cursor = conn.execute(
                "DELETE FROM change_sets WHERE change_set_id = ? "
                "AND updated_at IS ? "
                "AND length(CAST(data_json AS BLOB)) = ? "
                "AND EXISTS (SELECT 1 FROM change_set_lifecycle_v6 AS lifecycle "
                "WHERE lifecycle.change_set_id = change_sets.change_set_id "
                "AND lifecycle.status IS ? AND lifecycle.terminal_at IS ? "
                "AND lifecycle.payload_guard_sha256 IS ?)",
                (
                    key,
                    row["updated_at"],
                    row["data_bytes"],
                    row["status"],
                    row["terminal_at"],
                    row["payload_guard_sha256"],
                ),
            )
        elif table_name == "jobs":
            conn.execute(
                "DELETE FROM ingest_task_cleanup WHERE job_id = ? "
                "AND status = 'completed'",
                (key,),
            )
            cursor = conn.execute(
                "DELETE FROM jobs WHERE job_id = ? AND status IS ? "
                "AND completed_at IS ? AND payload IS ? AND updated_at IS ?",
                (
                    key,
                    row["status"],
                    row["completed_at"],
                    row["payload"],
                    row["updated_at"],
                ),
            )
        elif table_name == "mutation_outbox":
            cursor = conn.execute(
                "DELETE FROM mutation_outbox WHERE id = ? AND status IS ? "
                "AND completed_at IS ? AND payload_text IS ? AND filename IS ? "
                "AND superseded_by IS ?",
                (
                    key,
                    row["status"],
                    row["completed_at"],
                    row["payload_text"],
                    row["filename"],
                    row["superseded_by"],
                ),
            )
        elif table_name == "claim_versions":
            cursor = conn.execute(
                "DELETE FROM claim_versions WHERE claim_version_id = ? "
                "AND record_hash IS ? AND recorded_at IS ? AND data_json IS ?",
                (key, row["record_hash"], row["recorded_at"], row["data_json"]),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM evidence_versions WHERE evidence_version_id = ? "
                "AND record_hash IS ? AND recorded_at IS ? AND data_json IS ?",
                (key, row["record_hash"], row["recorded_at"], row["data_json"]),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"History retention CAS delete failed: {table_name}:{key}"
            )
        deleted_counts[table_name] += 1

    payload_rows, payload_bytes = _delete_unreferenced_change_set_payloads(
        conn,
        payload_hashes,
    )
    deleted_counts["change_set_payloads"] = payload_rows
    receipt = {
        "contract": "history-retention-receipt-v2",
        "fingerprint": fingerprint,
        "plan_as_of": plan["plan_as_of"],
        "cutoff": plan["cutoff"],
        "deleted_counts": deleted_counts,
        "deleted_payload_bytes": payload_bytes,
        "selected_bytes_total": int(plan.get("selected_bytes_total") or 0),
        "version_resume_cursors": {
            table_name: str(
                ((plan.get("version_scan") or {}).get(table_name) or {}).get(
                    "safe_next_cursor"
                )
                or ""
            )
            for table_name in ("claim_versions", "evidence_versions")
        },
        "version_cursor_policy": cursor_policy,
        "version_cursor_policy_sha256": _version_cursor_policy_sha256(cursor_policy),
        "runtime_generations_after": _history_runtime_generations(conn),
        "applied_at": _utc_now(),
    }
    conn.execute(
        "INSERT INTO history_retention_runs_v6 "
        "(fingerprint, plan_version, schema_version, plan_as_of, options_json, "
        "plan_sha256, receipt_json, applied_at) VALUES (?, 2, ?, ?, ?, ?, ?, ?)",
        (
            fingerprint,
            int(plan["schema_version"]),
            plan["plan_as_of"],
            _canonical_json_bytes(plan["rules"]).decode("utf-8"),
            fingerprint[7:],
            _canonical_json_bytes(receipt).decode("utf-8"),
            receipt["applied_at"],
        ),
    )
    return deleted_counts


def _prepare_legacy_change_set_compaction(
    change_set: dict,
    *,
    lifecycle_status: str,
    raw_sha256: str,
    raw_bytes: int,
    terminal_at: str | None,
) -> tuple[dict, str, bytes]:
    """Build one bounded legacy rewrite and classify its storage strategy."""
    prepared_change_set = copy.deepcopy(change_set)
    status = _normalized_change_set_status(lifecycle_status)
    prepared_change_set["status"] = status
    if status in _CHANGE_SET_TERMINAL_STATUSES:
        return (
            _legacy_terminal_change_set_manifest(
                prepared_change_set,
                raw_sha256=raw_sha256,
                raw_bytes=raw_bytes,
                terminal_at=terminal_at,
            ),
            "terminal_detached_summary",
            b"",
        )
    prepared = _validate_change_set_batch_limits([prepared_change_set])
    payload_bytes = prepared[0][1]
    return (
        _change_set_manifest(
            prepared_change_set,
            payload_bytes,
            payload_available=True,
            terminal_at=None,
        ),
        "pending_content_addressed_delta",
        payload_bytes,
    )


def _change_set_compaction_cursor_policy() -> dict[str, str]:
    return {
        "cursor_semantics": "last-classified-key-v2",
        "change_set_id_invariant": (
            "storage-class-text-nonempty-no-nul-max1024-utf8-v2"
        ),
    }


def _change_set_compaction_cursor_policy_sha256(
    policy: dict[str, str],
) -> str:
    return hashlib.sha256(_canonical_json_bytes(policy)).hexdigest()


def _assert_change_set_compaction_id_domain(conn: sqlite3.Connection) -> None:
    """Fail before keyset planning when either joined ID domain is unsafe."""
    for table_name in ("change_sets", "change_set_lifecycle_v6"):
        # SQLite orders NULL/numeric < TEXT < BLOB.  Both primary-key
        # boundaries therefore detect every non-TEXT storage class without a
        # full-table validation scan on every compaction batch.
        for direction in ("ASC", "DESC"):
            row = conn.execute(
                f"SELECT change_set_id, typeof(change_set_id) AS storage_class "
                f"FROM {table_name} ORDER BY change_set_id {direction} LIMIT 1"
            ).fetchone()
            if row is not None and (
                str(row["storage_class"]) != "text"
                or not isinstance(row["change_set_id"], str)
                or not row["change_set_id"]
            ):
                raise RuntimeError(
                    f"{table_name} contains an unresumable change-set identifier"
                )


def plan_change_set_history_compaction(
    conn: sqlite3.Connection,
    *,
    max_rows: int = 100,
    max_input_bytes: int = 64 * 1024 * 1024,
    cursor: str = "",
) -> dict:
    """Plan one metadata-first keyset window of legacy snapshot rewrites."""
    if max_rows < 1 or max_rows > 500:
        raise ValueError("max_rows must be between 1 and 500")
    if max_input_bytes < 1 or max_input_bytes > _CHANGE_SET_COMPACTION_MAX_INPUT_BYTES:
        raise ValueError("max_input_bytes must be between 1 byte and 128 MiB")
    normalized_cursor = str(cursor or "")
    if (
        "\x00" in normalized_cursor
        or len(normalized_cursor.encode("utf-8"))
        > _CHANGE_SET_COMPACTION_MAX_CURSOR_BYTES
    ):
        raise ValueError("change-set compaction cursor is malformed")
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 6:
        raise RuntimeError("Change-set history compaction requires schema v6")
    _assert_change_set_compaction_id_domain(conn)
    cursor_policy = _change_set_compaction_cursor_policy()
    cursor_policy_sha256 = _change_set_compaction_cursor_policy_sha256(cursor_policy)
    scan_rows = conn.execute(
        "SELECT change_sets.rowid AS storage_rowid, change_sets.change_set_id, "
        "change_sets.updated_at, "
        "length(CAST(change_sets.data_json AS BLOB)) AS input_bytes, lifecycle.status, "
        "lifecycle.terminal_at, lifecycle.payload_guard_sha256 "
        "FROM change_sets JOIN change_set_lifecycle_v6 AS lifecycle "
        "ON lifecycle.change_set_id = change_sets.change_set_id "
        "WHERE change_sets.change_set_id > ? "
        "ORDER BY change_sets.change_set_id LIMIT ?",
        (
            normalized_cursor,
            _CHANGE_SET_COMPACTION_MAX_SCAN_ROWS + 1,
        ),
    ).fetchall()
    more_after_scan_window = len(scan_rows) > _CHANGE_SET_COMPACTION_MAX_SCAN_ROWS
    rows = scan_rows[:_CHANGE_SET_COMPACTION_MAX_SCAN_ROWS]
    candidates: list[dict] = []
    selected_bytes = 0
    oversize: list[dict] = []
    oversize_count = 0
    uncompactable: list[dict] = []
    uncompactable_count = 0
    skipped_by_batch_bytes = 0
    scanned_rows = 0
    preflight_input_bytes = 0
    preflight_byte_limit_reached = False
    current_manifest_count = 0
    safe_next_cursor = normalized_cursor
    for row in rows:
        if len(candidates) >= max_rows:
            break
        raw_change_set_id = row["change_set_id"]
        if not isinstance(raw_change_set_id, str) or not raw_change_set_id:
            raise RuntimeError(
                "change_sets contains an unresumable change-set identifier"
            )
        change_set_id = raw_change_set_id
        if (
            "\x00" in change_set_id
            or len(change_set_id.encode("utf-8"))
            > _CHANGE_SET_COMPACTION_MAX_CURSOR_BYTES
        ):
            raise RuntimeError(
                "change_sets contains an unresumable change-set identifier"
            )
        input_bytes = int(row["input_bytes"] or 0)
        if input_bytes > max_input_bytes:
            scanned_rows += 1
            safe_next_cursor = change_set_id
            oversize_count += 1
            if len(oversize) < 20:
                oversize.append(
                    {
                        "change_set_id": change_set_id,
                        "input_bytes": input_bytes,
                    }
                )
            continue
        if preflight_input_bytes + input_bytes > max_input_bytes:
            preflight_byte_limit_reached = True
            skipped_by_batch_bytes += 1
            break
        remaining_input_bytes = max_input_bytes - preflight_input_bytes
        raw_guard = _sqlite_text_blob_sha256(
            conn,
            table_name="change_sets",
            column_name="data_json",
            rowid=int(row["storage_rowid"]),
            expected_bytes=input_bytes,
        )
        if not hmac.compare_digest(
            raw_guard,
            str(row["payload_guard_sha256"]),
        ):
            raise ChangeSetPayloadCorrupt(
                "Legacy change-set lifecycle guard drifted before compaction: "
                f"{row['change_set_id']}"
            )
        raw = conn.execute(
            "SELECT data_json FROM change_sets WHERE rowid = ? "
            "AND change_set_id = ? "
            "AND length(CAST(data_json AS BLOB)) = ? "
            "AND length(CAST(data_json AS BLOB)) <= ?",
            (
                row["storage_rowid"],
                change_set_id,
                input_bytes,
                remaining_input_bytes,
            ),
        ).fetchone()
        if raw is None:
            raise RuntimeError(
                "Legacy change-set changed before bounded compaction preflight: "
                f"{change_set_id}"
            )
        raw_text = str(raw["data_json"])
        if len(raw_text.encode("utf-8")) != input_bytes:
            raise RuntimeError(
                "Legacy change-set byte length drifted during compaction preflight: "
                f"{change_set_id}"
            )
        preflight_input_bytes += input_bytes
        scanned_rows += 1
        safe_next_cursor = change_set_id
        change_set = _history_json_object(raw_text)
        if change_set and change_set.get("manifest_version") not in {None, 1}:
            current_manifest_count += 1
            continue
        try:
            if not change_set:
                raise ChangeSetPayloadCorrupt(
                    "legacy snapshot is malformed or no longer uses a legacy manifest"
                )
            _manifest, compaction_kind, _payload_bytes = (
                _prepare_legacy_change_set_compaction(
                    change_set,
                    lifecycle_status=str(row["status"]),
                    raw_sha256=raw_guard,
                    raw_bytes=input_bytes,
                    terminal_at=row["terminal_at"],
                )
            )
        except (
            ChangeSetBatchTooLarge,
            ChangeSetPayloadCorrupt,
            ChangeSetPayloadTooLarge,
            TypeError,
            ValueError,
        ) as exc:
            uncompactable_count += 1
            if len(uncompactable) < 20:
                uncompactable.append(
                    {
                        "change_set_id": change_set_id,
                        "input_bytes": input_bytes,
                        "reason": type(exc).__name__,
                        "detail": str(exc)[:240],
                    }
                )
            continue
        candidates.append(
            {
                "change_set_id": change_set_id,
                "updated_at": row["updated_at"],
                "status": str(row["status"]),
                "terminal_at": row["terminal_at"],
                "input_bytes": input_bytes,
                "row_guard_sha256": raw_guard,
                "compaction_kind": compaction_kind,
            }
        )
        selected_bytes += input_bytes
    classified_rows = scanned_rows
    scan_truncated = (
        classified_rows < len(rows)
        or more_after_scan_window
        or preflight_byte_limit_reached
    )
    compaction_exhausted = bool(
        not scan_truncated
        and not candidates
        and not oversize_count
        and not uncompactable_count
    )
    remaining_legacy_lower_bound = (
        len(candidates) + oversize_count + uncompactable_count + int(scan_truncated)
    )
    plan = {
        "contract": "change-set-history-compaction-plan-v1",
        "schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "schema_cookie": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
        "runtime_generations": _history_runtime_generations(conn),
        "max_rows": int(max_rows),
        "max_input_bytes": int(max_input_bytes),
        "cursor_policy": cursor_policy,
        "cursor_policy_sha256": cursor_policy_sha256,
        "input_cursor": normalized_cursor,
        "safe_next_cursor": safe_next_cursor,
        "selected_rows": len(candidates),
        "selected_input_bytes": selected_bytes,
        "remaining_legacy_before": remaining_legacy_lower_bound,
        "remaining_legacy_exact": not scan_truncated and not oversize_count,
        "malformed_rows": None,
        "malformed_rows_exact": False,
        "scan_limit": _CHANGE_SET_COMPACTION_MAX_SCAN_ROWS,
        "scanned_rows": scanned_rows,
        "scan_truncated": scan_truncated,
        "scan_reached_end": not scan_truncated,
        "compaction_exhausted": compaction_exhausted,
        "preflight_input_bytes": preflight_input_bytes,
        "preflight_byte_limit_reached": preflight_byte_limit_reached,
        "oversize_count": oversize_count,
        "oversize_samples": oversize,
        "uncompactable_count": uncompactable_count,
        "uncompactable_samples": uncompactable,
        "current_manifest_count": current_manifest_count,
        "skipped_by_batch_bytes": skipped_by_batch_bytes,
        "candidates": candidates,
    }
    plan["fingerprint"] = _history_plan_fingerprint(plan)
    return plan


def apply_change_set_history_compaction_plan(
    conn: sqlite3.Connection,
    plan: dict,
    *,
    confirmation: str,
) -> dict:
    """Rewrite only exact legacy rows selected by a confirmed compaction plan."""
    if not conn.in_transaction:
        raise RuntimeError("Change-set compaction requires an active transaction")
    if plan.get("contract") != "change-set-history-compaction-plan-v1":
        raise ValueError("Unsupported change-set compaction plan")
    fingerprint = str(plan.get("fingerprint") or "")
    if not confirmation or not hmac.compare_digest(confirmation, fingerprint):
        raise RuntimeError(
            "Change-set compaction requires the exact preview fingerprint"
        )
    if not hmac.compare_digest(fingerprint, _history_plan_fingerprint(plan)):
        raise RuntimeError("Change-set compaction plan fingerprint is invalid")
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != int(
        plan.get("schema_version") or -1
    ) or int(conn.execute("PRAGMA schema_version").fetchone()[0]) != int(
        plan.get("schema_cookie") or -1
    ):
        raise RuntimeError("Change-set compaction schema drifted after preview")
    if _history_runtime_generations(conn) != plan.get("runtime_generations"):
        raise RuntimeError("Change-set compaction generations drifted after preview")
    _assert_change_set_compaction_id_domain(conn)
    expected_cursor_policy = _change_set_compaction_cursor_policy()
    expected_cursor_policy_sha256 = _change_set_compaction_cursor_policy_sha256(
        expected_cursor_policy
    )
    if plan.get("cursor_policy") != expected_cursor_policy or not hmac.compare_digest(
        str(plan.get("cursor_policy_sha256") or ""),
        expected_cursor_policy_sha256,
    ):
        raise ValueError("Change-set compaction cursor policy is invalid")

    try:
        max_rows = int(plan.get("max_rows"))
        max_input_bytes = int(plan.get("max_input_bytes"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Change-set compaction bounds are malformed") from exc
    if max_rows < 1 or max_rows > 500:
        raise ValueError("Change-set compaction max_rows is outside the hard limit")
    if max_input_bytes < 1 or max_input_bytes > _CHANGE_SET_COMPACTION_MAX_INPUT_BYTES:
        raise ValueError("Change-set compaction byte bound is outside the hard limit")
    input_cursor = str(plan.get("input_cursor") or "")
    safe_next_cursor = str(plan.get("safe_next_cursor") or "")
    for cursor_name, cursor_value in (
        ("input", input_cursor),
        ("safe next", safe_next_cursor),
    ):
        if (
            "\x00" in cursor_value
            or len(cursor_value.encode("utf-8"))
            > _CHANGE_SET_COMPACTION_MAX_CURSOR_BYTES
        ):
            raise ValueError(f"Change-set compaction {cursor_name} cursor is malformed")
    if safe_next_cursor < input_cursor:
        raise ValueError("Change-set compaction cursor moves backwards")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > max_rows:
        raise ValueError("Change-set compaction candidates exceed the row bound")
    expected_input_bytes = 0
    candidate_ids = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Change-set compaction candidate is malformed")
        candidate_id = str(candidate.get("change_set_id") or "")
        if (
            not candidate_id
            or candidate_id <= input_cursor
            or candidate_id > safe_next_cursor
        ):
            raise ValueError(
                "Change-set compaction candidate is outside its cursor window"
            )
        candidate_ids.append(candidate_id)
        try:
            candidate_bytes = int(candidate.get("input_bytes"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Change-set compaction candidate size is malformed"
            ) from exc
        if candidate_bytes < 0 or candidate_bytes > max_input_bytes:
            raise ValueError("Change-set compaction candidate exceeds the byte bound")
        expected_input_bytes += candidate_bytes
    if expected_input_bytes > max_input_bytes:
        raise ValueError("Change-set compaction batch exceeds the byte bound")
    if candidate_ids != sorted(set(candidate_ids)):
        raise ValueError("Change-set compaction candidates are not a unique keyset")
    if (
        int(plan.get("selected_rows") or 0) != len(candidates)
        or int(plan.get("selected_input_bytes") or 0) != expected_input_bytes
    ):
        raise ValueError("Change-set compaction plan totals are inconsistent")
    if int(plan.get("preflight_input_bytes") or 0) > max_input_bytes:
        raise ValueError("Change-set compaction preflight exceeds the byte bound")
    if bool(plan.get("compaction_exhausted")) and (
        bool(plan.get("scan_truncated"))
        or candidates
        or int(plan.get("oversize_count") or 0)
        or int(plan.get("uncompactable_count") or 0)
    ):
        raise ValueError("Change-set compaction exhaustion marker is inconsistent")

    compacted = 0
    input_bytes_total = 0
    output_bytes_total = 0
    pending_payload_bytes = 0
    for candidate in candidates:
        change_set_id = str(candidate.get("change_set_id") or "")
        row = conn.execute(
            "SELECT change_sets.rowid AS storage_rowid, change_sets.updated_at, "
            "length(CAST(change_sets.data_json AS BLOB)) AS input_bytes, "
            "lifecycle.status, "
            "lifecycle.terminal_at, lifecycle.payload_guard_sha256 "
            "FROM change_sets JOIN change_set_lifecycle_v6 AS lifecycle "
            "ON lifecycle.change_set_id = change_sets.change_set_id "
            "WHERE change_sets.change_set_id = ?",
            (change_set_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"Change-set compaction row disappeared: {change_set_id}"
            )
        input_bytes = int(row["input_bytes"] or 0)
        if (
            input_bytes != int(candidate.get("input_bytes") or -1)
            or str(row["status"]) != str(candidate.get("status") or "")
            or row["terminal_at"] != candidate.get("terminal_at")
            or row["updated_at"] != candidate.get("updated_at")
        ):
            raise RuntimeError(
                f"Change-set compaction metadata drifted after preview: {change_set_id}"
            )
        raw_guard = _sqlite_text_blob_sha256(
            conn,
            table_name="change_sets",
            column_name="data_json",
            rowid=int(row["storage_rowid"]),
            expected_bytes=input_bytes,
        )
        if not hmac.compare_digest(
            raw_guard,
            str(candidate.get("row_guard_sha256") or ""),
        ) or not hmac.compare_digest(
            raw_guard,
            str(row["payload_guard_sha256"] or ""),
        ):
            raise RuntimeError(
                f"Change-set compaction row drifted after preview: {change_set_id}"
            )
        raw = conn.execute(
            "SELECT data_json FROM change_sets WHERE rowid = ? "
            "AND change_set_id = ? "
            "AND length(CAST(data_json AS BLOB)) = ? "
            "AND length(CAST(data_json AS BLOB)) <= ?",
            (
                row["storage_rowid"],
                change_set_id,
                input_bytes,
                max_input_bytes,
            ),
        ).fetchone()
        if raw is None:
            raise RuntimeError(
                f"Change-set compaction input changed before bounded load: {change_set_id}"
            )
        raw_text = str(raw["data_json"])
        if len(raw_text.encode("utf-8")) != input_bytes:
            raise RuntimeError(
                f"Change-set compaction byte length drifted: {change_set_id}"
            )
        change_set = _history_json_object(raw_text)
        if not change_set or change_set.get("manifest_version") not in {None, 1}:
            raise ChangeSetPayloadCorrupt(
                f"Change-set compaction input is no longer legacy: {change_set_id}"
            )
        manifest, compaction_kind, payload_bytes = (
            _prepare_legacy_change_set_compaction(
                change_set,
                lifecycle_status=str(row["status"]),
                raw_sha256=raw_guard,
                raw_bytes=input_bytes,
                terminal_at=row["terminal_at"],
            )
        )
        if compaction_kind != str(candidate.get("compaction_kind") or ""):
            raise RuntimeError(
                f"Change-set compaction strategy drifted after preview: {change_set_id}"
            )
        payload_available = bool(payload_bytes)
        manifest_json = _canonical_json_bytes(manifest).decode("utf-8")
        cursor = conn.execute(
            "UPDATE change_sets SET data_json = ?, updated_at = ? "
            "WHERE change_set_id = ? AND updated_at IS ? "
            "AND length(CAST(data_json AS BLOB)) = ? "
            "AND EXISTS (SELECT 1 FROM change_set_lifecycle_v6 AS lifecycle "
            "WHERE lifecycle.change_set_id = change_sets.change_set_id "
            "AND lifecycle.payload_guard_sha256 IS ?)",
            (
                manifest_json,
                _utc_now(),
                change_set_id,
                row["updated_at"],
                input_bytes,
                raw_guard,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Change-set compaction CAS update failed: {change_set_id}"
            )
        if payload_available:
            payload_sha256 = _store_change_set_payload(conn, payload_bytes)
            conn.execute(
                "INSERT INTO change_set_payload_refs "
                "(change_set_id, payload_sha256, created_at) VALUES (?, ?, ?)",
                (change_set_id, payload_sha256, _utc_now()),
            )
            pending_payload_bytes += len(payload_bytes)
        lifecycle_cursor = conn.execute(
            "UPDATE change_set_lifecycle_v6 SET payload_guard_sha256 = ? "
            "WHERE change_set_id = ? AND payload_guard_sha256 IS ?",
            (
                hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
                change_set_id,
                raw_guard,
            ),
        )
        if lifecycle_cursor.rowcount != 1:
            raise RuntimeError(
                f"Change-set compaction lifecycle CAS failed: {change_set_id}"
            )
        compacted += 1
        input_bytes_total += input_bytes
        output_bytes_total += len(manifest_json.encode("utf-8"))
    return {
        "fingerprint": fingerprint,
        "input_cursor": input_cursor,
        "safe_next_cursor": safe_next_cursor,
        "scan_truncated": bool(plan.get("scan_truncated")),
        "scan_reached_end": bool(plan.get("scan_reached_end")),
        "compaction_exhausted": bool(plan.get("compaction_exhausted")),
        "oversize_count": int(plan.get("oversize_count") or 0),
        "uncompactable_count": int(plan.get("uncompactable_count") or 0),
        "compacted_rows": compacted,
        "input_bytes": input_bytes_total,
        "manifest_bytes": output_bytes_total,
        "logical_bytes_removed": max(0, input_bytes_total - output_bytes_total),
        "pending_payload_raw_bytes": pending_payload_bytes,
    }
