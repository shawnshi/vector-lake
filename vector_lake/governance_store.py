import copy
import heapq
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from filelock import FileLock

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - minimal installations use stdlib JSON
    _orjson = None

from vector_lake.claim_extractor import classify_non_claim_text, extract_page_objects
from vector_lake.db_store import get_connection, init_db, peek_db_path, transaction
from vector_lake.evidence_foundation import version_family_id
from vector_lake.wiki_utils import (
    get_meta_dir,
    get_wiki_dir,
    iter_markdown_files,
    normalize_semantic_text,
    read_markdown_file,
    split_frontmatter,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(
            f"Operational-memory projection unavailable: {self.reason}"
        )



class CanonicalIdOwnershipError(ValueError):
    """Reject reuse or relocation of a globally unique canonical identifier."""


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
    "entities", "claims", "evidence", "sources", "change_sets",
    "governance_queue", "wiki_search_index", "alias_registry",
    "operational_memory", "claim_graph_nodes", "claim_graph_edges", "page_graph_edges",
    "timeline_events", "processed_files", "mutation_outbox"
}

def _validate_table_name(table_name: str):
    """🛡️ Sentinel: Prevent SQL injection by validating table names against a strict whitelist."""
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Security error: Invalid table name '{table_name}'. Expected one of {ALLOWED_TABLES}.")

def initialize_meta_store():
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
    initialize_meta_store()
    conn = get_connection()
    store = _default_queue_store()
    rows = conn.execute(f"SELECT data_json FROM {table_name} ORDER BY updated_at ASC").fetchall()
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
        existing_rows = conn.execute(f"SELECT {pk_col}, data_json FROM {table_name}").fetchall()
        existing_map = {row[0]: row["data_json"] for row in existing_rows}
        new_keys = set(data.get("items", {}).keys())
        keys_to_delete = set(existing_map.keys()) - new_keys
        
        if keys_to_delete:
            conn.executemany(f"DELETE FROM {table_name} WHERE {pk_col} = ?", [(k,) for k in keys_to_delete])
            
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
                conn.executemany(f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})", all_vals)


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
                
        existing_keys_query = conn.execute(f"SELECT {pk_col} FROM {table_name}").fetchall()
        existing_keys = {row[0] for row in existing_keys_query}
        new_keys = {item[pk_col] for item in data.get("items", [])}
        keys_to_delete = existing_keys - new_keys
        
        if keys_to_delete:
            conn.executemany(f"DELETE FROM {table_name} WHERE {pk_col} = ?", [(k,) for k in keys_to_delete])
            
        if data.get("items"):
            all_vals = []
            for item in data.get("items", []):
                k = item.get(pk_col)
                all_vals.append((k, json.dumps(item, ensure_ascii=False), now))
            conn.executemany(f"INSERT OR REPLACE INTO {table_name} ({pk_col}, data_json, updated_at) VALUES (?, ?, ?)", all_vals)


def load_entities():
    return _load_db_map("entities", "entity_id")


def query_entities(filters: dict = None) -> dict:
    initialize_meta_store()
    conn = get_connection()
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
    rows = conn.execute(query, tuple(params)).fetchall()
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
    row = get_connection().execute(
        "SELECT value FROM alias_registry WHERE key = ?",
        (key,),
    ).fetchone()
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


def load_change_sets():
    return _load_db_queue("change_sets", "change_set_id")


def load_governance_queue():
    return _load_db_queue("governance_queue", "item_id")


def save_entities(data):
    _save_db_map("entities", "entity_id", data, [
        ("canonical_name", "canonical_name", str),
        ("type", "type", str),
        ("status", "status", str),
        ("ttl", "ttl", float),
        ("decay_weight", "decay_weight", float)
    ])


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
            params = (edge["source_id"], edge["target_id"], edge["relation"], edge.get("weight", 1.0), edge.get("updated_at", _utc_now()))
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
        existing_keys = {row["key"] for row in conn.execute("SELECT key FROM alias_registry")}
        new_keys = set(data.get("items", {}))
        stale_keys = existing_keys - new_keys
        if stale_keys:
            conn.executemany("DELETE FROM alias_registry WHERE key = ?", [(key,) for key in stale_keys])
        for k, v in data.get("items", {}).items():
            conn.execute("INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)", (k, v, now))


def save_memory_objects(data):
    _save_db_map("operational_memory", "memory_id", data, [
        ("memory_type", "memory_type", str), 
        ("score", "memory_score", float),
        ("status", "status", str),
        ("ttl", "ttl", float)
    ])


def save_change_sets(data):
    _save_db_queue("change_sets", "change_set_id", data)

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
    row = get_connection().execute(
        "SELECT data_json FROM governance_queue WHERE item_id = ?",
        (str(item_id),),
    ).fetchone()
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
        row = get_connection().execute(
            "SELECT data_json FROM governance_queue WHERE item_id = ?",
            (str(item_id),),
        ).fetchone()
        if row is None:
            return None
        item = json.loads(row["data_json"])
        if expected_statuses is not None and str(item.get("status")) not in expected_statuses:
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
        priority = _GOVERNANCE_DEFAULT_PRIORITY.get(str(normalized.get("type") or ""), "P2")
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
            clauses = [f"json_extract(data_json, '$.{field}') = ?" for field in dedup_fields]
            values = [item[field] for field in dedup_fields]
            existing = get_connection().execute(
                "SELECT 1 FROM governance_queue WHERE " + " AND ".join(clauses) + " LIMIT 1",
                values,
            ).fetchone()
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
        rows = get_connection().execute(
            f"SELECT item_id, data_json FROM governance_queue WHERE json_extract(data_json, '$.{field}') = ?",
            (value,),
        ).fetchall()
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
    row = conn.execute("SELECT data_json FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
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
        (str(record["entity_id"]), record)
        for record in extracted.get("entities", [])
    ]
    if not records:
        return ""
    return _canonical_entity_records_version(records)


def canonical_page_versions(page_keys: set[str] | None = None) -> dict[str, str]:
    """Return deterministic version tokens for the current canonical page state."""
    init_db()
    requested = set(page_keys) if page_keys is not None else None
    records_by_page: dict[str, list[tuple[str, dict]]] = {}
    conn = get_connection()
    if requested:
        rows = []
        ordered = sorted(requested)
        for offset in range(0, len(ordered), 500):
            batch = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                conn.execute(
                    "SELECT entity_id, data_json FROM entities "
                    f"WHERE json_extract(data_json, '$.page_key') IN ({placeholders}) "
                    "ORDER BY entity_id",
                    tuple(batch),
                ).fetchall()
            )
    else:
        rows = conn.execute(
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
    cols = ["entity_id", "canonical_name", "type", "status", "ttl", "decay_weight", "data_json", "updated_at"]
    placeholders = ["?"] * len(cols)
    params = [
        entity_id,
        str(data.get("canonical_name") or data.get("title") or data.get("page_key") or entity_id),
        str(data.get("type", "")),
        str(data.get("status", "Active")),
        float(data.get("ttl") or 0.0),
        float(data.get("decay_weight") or 0.0),
        json.dumps(data, ensure_ascii=False),
        now
    ]
    with transaction():
        conn.execute(f"INSERT OR REPLACE INTO entities ({', '.join(cols)}) VALUES ({', '.join(placeholders)})", params)

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
                    str(record.get("canonical_name") or record.get("title") or record.get("page_key") or record[key_name]),
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


def _record_family(record: dict, family_field: str, family_prefix: str) -> tuple[str, str]:
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
            family_id = _stable_id(family_prefix, str(record.get("claim_id") or record.get("evidence_id")))
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
    added = 0
    for record in records:
        record_id = str(record.get(id_field) or "")
        if not record_id:
            continue
        family_id, page_key = _record_family(record, family_field, family_prefix)
        serialized = _canonical_record_json(record)
        record_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        version_id = _stable_id(version_prefix, f"{family_id}:{record_hash}")
        row = conn.execute(
            f"SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version FROM {table_name} "
            f"WHERE {family_field} = ?",
            (family_id,),
        ).fetchone()
        result = conn.execute(
            f"INSERT OR IGNORE INTO {table_name} "
            f"({version_prefix}_id, {id_field}, {family_field}, page_key, version_no, "
            "record_hash, data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                record_id,
                family_id,
                page_key,
                int(row["next_version"]),
                record_hash,
                serialized,
                _utc_now(),
            ),
        )
        added += int(bool(result.rowcount))
    return added


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


def _merge_missing_record_fields(current: dict, proposed: dict, fields: tuple[str, ...]) -> bool:
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
        and len(current_hash) != 64
    ):
        if current_hash and "legacy_content_hash" not in current:
            current["legacy_content_hash"] = current_hash
        current["content_hash"] = proposed_hash
        current["hash_algorithm"] = "sha256"
        current["integrity_status"] = "verified"
        changed = True
    return changed


def backfill_evidence_foundation_records(extracted: dict) -> dict:
    """Merge one extracted page's foundation metadata into existing canonical rows.

    The caller owns the transaction.  Missing canonical claim/evidence/source IDs
    abort the page so an extraction run can never mark a partial backfill complete.
    """
    conn = get_connection()
    page_key = str(extracted.get("page_key") or "")
    runs = list(extracted.get("extraction_runs") or [])
    if not page_key or len(runs) != 1:
        raise ValueError("Evidence-foundation backfill requires one page and one extraction run.")

    record_specs = (
        ("claims", "claim_id", _CLAIM_FOUNDATION_FIELDS, list(extracted.get("claims") or [])),
        ("evidence", "evidence_id", _EVIDENCE_FOUNDATION_FIELDS, list(extracted.get("evidence") or [])),
        ("sources", "source_id", _SOURCE_FOUNDATION_FIELDS, list(extracted.get("sources") or [])),
    )
    merged_by_table: dict[str, list[dict]] = {"claims": [], "evidence": [], "sources": []}
    changed_by_table = {"claims": 0, "evidence": 0, "sources": 0}
    for table_name, key_field, fields, proposed_records in record_specs:
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
            if (
                _merge_source_foundation_fields(current, proposed)
                if table_name == "sources"
                else _merge_missing_record_fields(current, proposed, fields)
            ):
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
            "claim_versions", "claim_id", "claim_family_id", "claimfamily",
            "claim_version", old_claims,
        )
    if changed_evidence:
        current_evidence_ids = tuple(record["evidence_id"] for record in changed_evidence)
        placeholders = ",".join("?" for _ in current_evidence_ids)
        old_evidence = [
            json.loads(row["data_json"])
            for row in conn.execute(
                f"SELECT data_json FROM evidence WHERE evidence_id IN ({placeholders})",
                current_evidence_ids,
            )
        ]
        _append_version_records(
            "evidence_versions", "evidence_id", "evidence_family_id", "evidencefamily",
            "evidence_version", old_evidence,
        )

    for table_name, key_field, _, _ in record_specs:
        _upsert_canonical_records(table_name, key_field, merged_by_table[table_name])
    _upsert_foundation_records(
        list(extracted.get("entities") or []),
        list(extracted.get("source_artifacts") or []),
        runs,
    )
    _append_version_records(
        "claim_versions", "claim_id", "claim_family_id", "claimfamily",
        "claim_version", changed_claims,
    )
    _append_version_records(
        "evidence_versions", "evidence_id", "evidence_family_id", "evidencefamily",
        "evidence_version", changed_evidence,
    )
    return {
        "page_key": page_key,
        "run_id": runs[0]["run_id"],
        "updated_claims": changed_by_table["claims"],
        "updated_evidence": changed_by_table["evidence"],
        "updated_sources": changed_by_table["sources"],
        "source_artifacts": len(extracted.get("source_artifacts") or []),
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


def _refresh_operational_memory_delta(old_claim_ids: set[str], proposed_claims: list[dict]):
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
        aliases.append((str(entity.get("canonical_name") or entity.get("title") or entity_id), entity_id, now))
        aliases.extend((str(alias), entity_id, now) for alias in entity.get("aliases", []) if alias)
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


def _coerce_float(value, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
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


def _query_terms(query: str) -> list[str]:
    text = str(query or "").lower()
    terms = {token for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text) if token}
    cjk_chars = [char for char in text if "\u4e00" <= char <= "\u9fff"]
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
    if any(token in text for token in ("preference", "preferred", "用户偏好", "偏好", "首选", "不要", "倾向")):
        return "preference"
    if any(token in text for token in ("decision", "decided", "approved", "决策", "决定", "方案", "采用", "选型")):
        return "decision"
    if any(token in text for token in ("task", "todo", "pending", "blocked", "open item", "待办", "未完成", "阻塞", "状态")):
        return "task_state"
    return "fact"


def _infer_memory_key(claim: dict, memory_type: str) -> str:
    explicit = claim.get("memory_key") or claim.get("preference_key") or claim.get("decision_key") or claim.get("task_key")
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

    updated_at = _parse_dt(record.get("updated_at")) or _parse_dt(record.get("created_at"))
    if not updated_at:
        return 0.55

    age_days = max(0, (now - updated_at).days)
    ttl_days = record.get("ttl_days") or MEMORY_TTL_DAYS.get(record.get("memory_type", "fact"), 365)
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
    reinforcement_score = min(1.0, math.log1p(max(0, reinforcement_count)) / math.log(8))
    validity_factor = VALIDITY_FACTORS.get(str(memory.get("validity_state", "active")).lower(), 0.5)
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


def _rank_memory_for_conflict(memory: dict, explicit_contradiction: bool = False) -> tuple:
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
                conflict_events.append({
                    "type": "unresolved-explicit-contradiction",
                    "memory_ids": sorted([memory["memory_id"], other["memory_id"]]),
                })
            elif left_rank > right_rank:
                _mark_superseded(other, memory, "explicit-contradiction:authority-confidence-recency")
            else:
                _mark_superseded(memory, other, "explicit-contradiction:authority-confidence-recency")

    grouped = {}
    for memory in items.values():
        if memory.get("memory_type") == "fact":
            continue
        if str(memory.get("validity_state", "")).lower() in {"expired", "archived"}:
            continue
        grouped.setdefault((memory.get("memory_type"), memory.get("memory_key")), []).append(memory)

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
        conflict_events.append({
            "type": "typed-memory-supersession",
            "memory_type": memory_type,
            "memory_key": memory_key,
            "winner": winner["memory_id"],
            "losers": [item["memory_id"] for item in ordered[1:]],
        })

    store["conflict_events"] = conflict_events
    store["memory_type_counts"] = {}
    for memory in items.values():
        memory_type = memory.get("memory_type", "fact")
        store["memory_type_counts"][memory_type] = store["memory_type_counts"].get(memory_type, 0) + 1
    return store


def rebuild_operational_memory() -> dict:
    claims = annotated_claims()
    store = _default_map_store("memory_id")
    existing = load_memory_objects()
    for memory_id, memory in existing.get("items", {}).items():
        reasons = memory.get("validity_reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        if (
            str(memory.get("validity_state") or "").lower() == "archived"
            and any(str(reason).startswith("infrastructure_artifact:") for reason in reasons)
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
            term_masks
            if any(len(term) <= 2 for term in term_masks)
            else None
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
_MEMORY_SEARCH_INDEX_DEFAULT_BATCH = 256
_MEMORY_SEARCH_INDEX_MAX_BATCH = 10_000
_MEMORY_SEARCH_INDEX_TABLES = frozenset({
    "operational_memory_search_fts",
    "operational_memory_search_docs",
    "operational_memory_search_pending",
    "operational_memory_search_state",
})


def _operational_memory_search_index_enabled() -> bool:
    value = os.environ.get("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


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
    return {str(row[0]) for row in rows} == _MEMORY_SEARCH_INDEX_TABLES


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
        decoded.append((
            memory_id,
            updated_at,
            str(memory.get("memory_key", "")).lower(),
            str(memory.get("text", "")).lower(),
            str(memory.get("source_page", "")).lower(),
            str(memory.get("memory_type", "fact")).lower(),
        ))

    memory_ids = [row[0] for row in decoded]
    existing = _memory_search_document_ids(conn, memory_ids)
    conn.executemany(
        "INSERT OR IGNORE INTO operational_memory_search_docs "
        "(memory_id, source_updated_at) VALUES (?, ?)",
        [(row[0], row[1]) for row in decoded],
    )
    documents = _memory_search_document_ids(conn, memory_ids)
    if len(documents) != len(set(memory_ids)):
        raise sqlite3.IntegrityError(
            "operational-memory search document mapping is incomplete"
        )

    if existing:
        conn.executemany(
            "DELETE FROM operational_memory_search_fts WHERE rowid = ?",
            [(doc_id,) for doc_id in existing.values()],
        )
    conn.executemany(
        "UPDATE operational_memory_search_docs SET source_updated_at = ? "
        "WHERE doc_id = ?",
        [(row[1], documents[row[0]]) for row in decoded],
    )
    conn.executemany(
        "INSERT INTO operational_memory_search_fts "
        "(rowid, key_text, memory_text, page_text, type_text) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (documents[row[0]], row[2], row[3], row[4], row[5])
            for row in decoded
        ],
    )


def _advance_operational_memory_search_index(
    conn: sqlite3.Connection,
    batch_size: int | None = None,
) -> tuple[int, int]:
    """Apply a bounded pending/backfill slice and return its durable cursor."""
    if batch_size is None:
        batch_size = _operational_memory_search_batch_size()
    else:
        batch_size = max(0, min(_MEMORY_SEARCH_INDEX_MAX_BATCH, int(batch_size)))
    with transaction(max_wait_seconds=0.1):
        state = conn.execute(
            "SELECT backfill_cursor, backfill_target "
            "FROM operational_memory_search_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            raise sqlite3.OperationalError("operational-memory search state is missing")
        cursor = int(state[0])
        target = int(state[1])

        pending_rows = conn.execute(
            "SELECT p.memory_id, p.operation, om.data_json, om.updated_at "
            "FROM operational_memory_search_pending AS p "
            "LEFT JOIN operational_memory AS om ON om.memory_id = p.memory_id "
            "ORDER BY p.rowid LIMIT ?",
            (batch_size,),
        ).fetchall()
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
            conn.executemany(
                "DELETE FROM operational_memory_search_pending WHERE memory_id = ?",
                [(str(row[0]),) for row in pending_rows],
            )

        remaining = max(0, batch_size - len(pending_rows))
        backfill_rows = []
        if remaining and cursor < target:
            backfill_rows = conn.execute(
                "SELECT rowid, memory_id, data_json, updated_at "
                "FROM operational_memory WHERE rowid > ? AND rowid <= ? "
                "ORDER BY rowid LIMIT ?",
                (cursor, target, remaining),
            ).fetchall()
            _upsert_memory_search_documents(
                conn,
                [(str(row[1]), row[2], row[3]) for row in backfill_rows],
            )
            cursor = int(backfill_rows[-1][0]) if backfill_rows else target

        conn.execute(
            "UPDATE operational_memory_search_state SET "
            "backfill_cursor = ?, updated_at = ? WHERE singleton = 1",
            (cursor, _utc_now()),
        )
    return cursor, target


def _memory_fts_expression(terms: list[str]) -> str:
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"'
        for term in terms
    )


def operational_memory_search_index_status() -> dict:
    """Inspect derived-index progress without creating schema or changing state."""
    configured = _operational_memory_search_index_enabled()
    if not peek_db_path().exists():
        return {
            "configured": configured,
            "available": False,
            "ready": False,
            "canonical_documents": 0,
            "indexed_documents": 0,
            "pending": 0,
        }
    conn = get_connection()
    if not _memory_search_index_schema_available(conn):
        canonical_documents = 0
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'operational_memory'"
        ).fetchone():
            canonical_documents = int(conn.execute(
                "SELECT COUNT(*) FROM operational_memory"
            ).fetchone()[0])
        return {
            "configured": configured,
            "available": False,
            "ready": False,
            "canonical_documents": canonical_documents,
            "indexed_documents": 0,
            "pending": 0,
        }
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, schema_version "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone()
    cursor = int(state[0]) if state is not None else 0
    target = int(state[1]) if state is not None else 0
    pending = int(conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0])
    indexed_documents = int(conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0])
    canonical_documents = int(conn.execute(
        "SELECT COUNT(*) FROM operational_memory"
    ).fetchone()[0])
    return {
        "configured": configured,
        "available": True,
        "ready": cursor >= target and pending == 0,
        "schema_version": int(state[2]) if state is not None else None,
        "backfill_cursor": cursor,
        "backfill_target": target,
        "canonical_documents": canonical_documents,
        "indexed_documents": indexed_documents,
        "pending": pending,
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
    pending = int(conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0])
    indexed_documents = int(conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0])
    return {
        "available": True,
        "ready": cursor >= target and pending == 0,
        "backfill_cursor": cursor,
        "backfill_target": target,
        "indexed_documents": indexed_documents,
        "pending": pending,
    }


def _memory_sql_term_filter(terms: list[str], alias: str = "om") -> tuple[str, list[str]]:
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


def _indexed_operational_memory_rows(
    conn: sqlite3.Connection,
    terms: list[str],
    allowed_types: set[str] | None,
):
    """Return exact-recall read-only candidates, or None without FTS schema."""
    if (
        not terms
        or not _operational_memory_search_index_enabled()
        or not _memory_search_index_schema_available(conn)
    ):
        return None

    try:
        state = conn.execute(
            "SELECT backfill_cursor, backfill_target "
            "FROM operational_memory_search_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            return None
        cursor = int(state[0])
        target = int(state[1])
        type_sql = ""
        type_params: list[object] = []
        if allowed_types:
            placeholders = ", ".join("?" for _ in allowed_types)
            type_sql = (
                " AND lower(COALESCE(om.memory_type, 'fact')) "
                f"IN ({placeholders})"
            )
            type_params = sorted(allowed_types)

        candidate_queries: list[str] = []
        candidate_params: list[object] = []
        long_terms = [term for term in terms if len(term) >= 3]
        short_terms = [term for term in terms if len(term) <= 2]
        if long_terms:
            candidate_queries.append(
                "SELECT om.rowid AS source_rowid, om.data_json AS data_json "
                "FROM operational_memory_search_fts "
                "JOIN operational_memory_search_docs AS docs "
                "ON docs.doc_id = operational_memory_search_fts.rowid "
                "JOIN operational_memory AS om ON om.memory_id = docs.memory_id "
                "WHERE operational_memory_search_fts MATCH ?" + type_sql
            )
            candidate_params.extend((_memory_fts_expression(long_terms), *type_params))
        if short_terms:
            short_filter, short_params = _memory_sql_term_filter(short_terms)
            candidate_queries.append(
                "SELECT om.rowid AS source_rowid, om.data_json AS data_json "
                "FROM operational_memory AS om WHERE " + short_filter + type_sql
            )
            candidate_params.extend((*short_params, *type_params))

        residual_filter, residual_params = _memory_sql_term_filter(terms)
        candidate_queries.append(
            "SELECT om.rowid AS source_rowid, om.data_json AS data_json "
            "FROM operational_memory AS om WHERE ((om.rowid > ? AND om.rowid <= ?) "
            "OR EXISTS (SELECT 1 FROM operational_memory_search_pending AS pending "
            "WHERE pending.memory_id = om.memory_id "
            "AND pending.operation = 'upsert')) AND "
            + residual_filter
            + type_sql
        )
        candidate_params.extend((
            cursor,
            target,
            *residual_params,
            *type_params,
        ))
        return conn.execute(
            "SELECT data_json FROM (" + " UNION ".join(candidate_queries) + ") "
            "ORDER BY source_rowid",
            tuple(candidate_params),
        )
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
            "operational-memory query exceeds "
            f"{_MEMORY_QUERY_CHAR_LIMIT} characters"
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
        filters.append(
            "lower(COALESCE(memory_type, 'fact')) "
            f"IN ({placeholders})"
        )
        params.extend(sorted(allowed_types))

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = f"SELECT data_json FROM operational_memory {where_sql}"
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
    for row in conn.execute("SELECT data_json FROM operational_memory"):
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
    if not conn.execute("SELECT 1 FROM operational_memory LIMIT 1").fetchone():
        if conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
            raise OperationalMemoryNotReady("projection_empty")
        return [], []

    allowed_types = None
    if memory_types:
        allowed_types = {
            str(item).strip().lower().replace("-", "_")
            for item in memory_types
        }
    terms = _bounded_memory_query_terms(query)
    matcher = _memory_term_matcher(terms)
    indexed_rows = _indexed_operational_memory_rows(conn, terms, allowed_types)
    candidate_sql = None
    candidate_params: list[object] = []
    if indexed_rows is None:
        candidate_terms = _memory_candidate_terms(query, terms)
        prefilter_terms = (
            candidate_terms if len(candidate_terms) == len(terms) else []
        )
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
    except sqlite3.OperationalError as exc:
        log.warning(
            "Operational-memory candidate query unavailable; using compatibility "
            "scan: %s",
            exc,
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


def build_claim_graph_projection(limit_nodes: int | None = None) -> dict:
    max_degree = 12
    entity_window = 6
    source_window = 4
    if limit_nodes is None:
        limit_nodes = 2500  # Hard cap to prevent 3D-force-graph from freezing the browser
    from vector_lake import governance_metrics

    claim_rows = get_connection().execute(
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
        str(source_id)
        for claim in claims
        for source_id in claim.get("source_ids", [])
    }

    def load_referenced(table_name: str, id_column: str, ids: set[str]) -> dict[str, dict]:
        records = {}
        ordered = sorted(ids)
        for offset in range(0, len(ordered), 500):
            batch = ordered[offset:offset + 500]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = get_connection().execute(
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
        nodes.append({
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
        })
        node_lookup[claim["claim_id"]] = nodes[-1]
        degree_map[claim["claim_id"]] = 0

    edge_records = {}

    def _record_edge(left_id: str, right_id: str, relation: str, weight: float, force: bool = False):
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

        if not force and (degree_map[source_id] >= max_degree or degree_map[target_id] >= max_degree):
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

    for (source_id, target_id), shared_count in sorted(entity_pair_counts.items(), key=lambda item: (-item[1], item[0])):
        weight = 2.5 + min(shared_count, 3) * 0.5
        _record_edge(source_id, target_id, "shared-entity", weight)

    source_pair_counts = {}
    for claim_ids_for_source in source_buckets.values():
        ordered_ids = sorted(set(claim_ids_for_source))
        for index, left_id in enumerate(ordered_ids):
            for right_id in ordered_ids[index + 1 : index + 1 + source_window]:
                edge_key = tuple(sorted((left_id, right_id)))
                source_pair_counts[edge_key] = source_pair_counts.get(edge_key, 0) + 1

    for (source_id, target_id), shared_count in sorted(source_pair_counts.items(), key=lambda item: (-item[1], item[0])):
        if (source_id, target_id) in edge_records:
            continue
        weight = 1.5 + min(shared_count, 3) * 0.5
        _record_edge(source_id, target_id, "shared-source", weight)

    edges = sorted(edge_records.values(), key=lambda edge: (-edge["weight"], edge["source"], edge["target"]))
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
            "affected_ids": [suggestion["left_entity_id"], suggestion["right_entity_id"]],
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


def create_change_set(
    page_paths: list[str],
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
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
        from vector_lake.db_store import get_connection
        conn = get_connection()
        row = conn.execute("SELECT data_json FROM change_sets WHERE json_extract(data_json, '$.idempotency_key') = ?", (idempotency_key,)).fetchone()
        if row:
            duplicate = json.loads(row[0])
            duplicate["deduplicated"] = True
            return duplicate

    change_set = {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
        "idempotency_key": idempotency_key,
        "origin": origin,
        "created_at": _utc_now(),
        "status": "published" if auto_approve else "pending",
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
                "entities", "claims", "evidence", "sources", "operational_memory",
                "source_artifacts", "extraction_runs", "claim_versions",
                "evidence_versions", "entity_identities", "canonical_identities",
            ],
        },
    }

    with transaction():
        if auto_approve:
            apply_change_set(change_set)
            change_set["published_at"] = _utc_now()
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
    idempotency_key = _stable_id("changeset_idem", "|".join([origin, page_key, fingerprint]))

    proposed_entities = extracted.get("entities", [])
    proposed_claims = extracted.get("claims", [])
    return {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
        "idempotency_key": idempotency_key,
        "origin": origin,
        "created_at": _utc_now(),
        "status": "published" if auto_approve else "pending",
        "summary": summary or f"Sync page: {page_key}",
        "risk_level": "low",
        "requires_human_review": not auto_approve,
        "affected_ids": sorted({
            *[record["entity_id"] for record in proposed_entities],
            *[record["claim_id"] for record in proposed_claims],
        }),
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
                "entities", "claims", "evidence", "sources", "operational_memory",
                "source_artifacts", "extraction_runs", "claim_versions",
                "evidence_versions", "entity_identities", "canonical_identities",
            ],
        },
    }


def record_prepared_change_sets(change_sets: list[dict]) -> int:
    """Persist prepared change sets once without scanning the JSON history."""
    if not change_sets:
        return 0
    conn = get_connection()
    now = _utc_now()
    added = 0
    with transaction():
        for change_set in change_sets:
            idempotency_key = str(change_set.get("idempotency_key") or change_set["change_set_id"])
            reserved = conn.execute(
                "INSERT OR IGNORE INTO change_set_idempotency "
                "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
                (idempotency_key, change_set["change_set_id"], now),
            )
            if not reserved.rowcount:
                continue
            conn.execute(
                "INSERT INTO change_sets (change_set_id, data_json, updated_at) VALUES (?, ?, ?)",
                (change_set["change_set_id"], json.dumps(change_set, ensure_ascii=False), now),
            )
            added += 1
    return added


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
    existing = get_connection().execute(
        "SELECT data_json FROM change_sets "
        "WHERE json_extract(data_json, '$.idempotency_key') = ? LIMIT 1",
        (change_set["idempotency_key"],),
    ).fetchone()
    if existing:
        duplicate = json.loads(existing["data_json"])
        duplicate["deduplicated"] = True
        return duplicate

    with transaction():
        if auto_approve:
            apply_change_sets_batch([change_set])
            change_set["published_at"] = _utc_now()
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
        raise RuntimeError("Canonical identity registration requires an active transaction")
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
    affected_pages = {
        page
        for change_set in change_sets
        for page in change_set.get("affected_pages", [])
    }
    affected_page_keys = {
        _normalized_owner_page(page)
        for page in affected_pages
    }
    proposed_entities = [record for item in change_sets for record in item.get("proposed_entities", [])]
    proposed_claims = [record for item in change_sets for record in item.get("proposed_claims", [])]
    proposed_evidence = [record for item in change_sets for record in item.get("proposed_evidence", [])]
    proposed_sources = [record for item in change_sets for record in item.get("proposed_source_updates", [])]
    proposed_source_artifacts = [
        record for item in change_sets for record in item.get("proposed_source_artifacts", [])
    ]
    proposed_extraction_runs = [
        record for item in change_sets for record in item.get("proposed_extraction_runs", [])
    ]
    proposed_edges = [record for item in change_sets for record in item.get("proposed_edges", [])]

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
        old_evidence_records = [json.loads(row["data_json"]) for row in old_evidence_rows]
        _append_version_records(
            "claim_versions", "claim_id", "claim_family_id", "claimfamily",
            "claim_version", old_claim_records,
        )
        _append_version_records(
            "evidence_versions", "evidence_id", "evidence_family_id", "evidencefamily",
            "evidence_version", old_evidence_records,
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
        "claim_versions", "claim_id", "claim_family_id", "claimfamily",
        "claim_version", proposed_claims,
    )
    _append_version_records(
        "evidence_versions", "evidence_id", "evidence_family_id", "evidencefamily",
        "evidence_version", proposed_evidence,
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
    """Atomically apply page-scoped canonical and derived projection deltas."""
    with transaction():
        return _apply_change_sets_batch_unchecked(change_sets)


def apply_change_set(change_set: dict) -> dict:
    apply_change_sets_batch([change_set])
    return change_set


def publish_change_sets(limit: int | None = None) -> dict:
    from vector_lake.db_store import get_connection, transaction
    conn = get_connection()
    rows = conn.execute("SELECT change_set_id, data_json FROM change_sets WHERE json_extract(data_json, '$.status') = 'pending'").fetchall()
    
    published = 0
    published_ids = []
    
    for row in rows:
        change_set = json.loads(row[1])
        apply_change_set(change_set)
        change_set["status"] = "published"
        change_set["published_at"] = _utc_now()
        
        with transaction():
            conn.execute("UPDATE change_sets SET data_json = ?, updated_at = ? WHERE change_set_id = ?", 
                         (json.dumps(change_set, ensure_ascii=False), _utc_now(), change_set["change_set_id"]))
            
        published += 1
        published_ids.append(change_set["change_set_id"])
        if limit is not None and published >= limit:
            break

    for change_set_id in published_ids:
        update_governance_items_by_field(
            "change_set_id",
            change_set_id,
            {"status": "published", "resolved_at": _utc_now()},
        )
    return {"published": published, "change_set_ids": published_ids}

def pending_change_sets() -> list:
    from vector_lake.db_store import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT data_json FROM change_sets WHERE json_extract(data_json, '$.status') = 'pending'").fetchall()
    return [json.loads(r[0]) for r in rows]


def pending_governance_items() -> list:
    return [item for item in load_governance_queue()["items"] if item.get("status") == "pending"]


def sync_pages_to_canonical(page_paths: list[str], origin: str, auto_approve: bool = True, summary: str | None = None) -> dict | None:
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
                logging.getLogger("governance").info(f"Deleted orphan entity {entity_id} ({page_key}) from SQLite due to missing markdown file.")

    if not existing_paths:
        return None
    return create_change_set(existing_paths, origin=origin, summary=summary, auto_approve=auto_approve)


def migrate_existing_wiki(dry_run: bool = False) -> dict:
    wiki_dir = get_wiki_dir()
    excluded = {"index.md", "log.md", "overview.md"}
    page_paths = [
        str(path)
        for path in sorted(iter_markdown_files(wiki_dir), key=lambda item: item.name)
        if path.name.casefold() not in excluded
    ]

    if dry_run:
        counts = {"entities": 0, "claims": 0, "evidence": 0, "sources": 0, "valid_pages": 0}
        for page_path in page_paths:
            frontmatter, body, _ = read_markdown_file(page_path)
            extracted = extract_page_objects(page_path, frontmatter, body)
            if extracted.get("entities") or os.path.basename(page_path).startswith("System_"):
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
    change_set = create_change_set(page_paths, origin="migrate-v8", summary="V8 migration", auto_approve=True, force=True)
    migrated_page_keys = {item.get("page_key") for item in change_set.get("proposed_entities", []) if item.get("page_key")}
    canonical_page_keys = {item.get("page_key") for item in load_entities()["items"].values() if item.get("page_key")}
    stale_entities = canonical_page_keys - migrated_page_keys

    return {
        "dry_run": False,
        "change_set_id": change_set["change_set_id"],
        "pages_scanned": len(page_paths),
        "entities": len(load_entities()["items"]),
        "claims": len(load_claims()["items"]),
        "evidence": len(load_evidence()["items"]),
        "sources": len(load_sources()["items"]),
        "stale_entities_preserved": len(stale_entities),
    }


def ensure_canonical_store_populated() -> dict:
    initialize_meta_store()
    entities = load_entities()["items"]
    claims = load_claims()["items"]
    sources = load_sources()["items"]
    wiki_pages = _count_wiki_pages()

    if claims or entities or sources or wiki_pages == 0:
        return {
            "bootstrapped": False,
            "entities": len(entities),
            "claims": len(claims),
            "sources": len(sources),
            "pages_scanned": wiki_pages,
        }

    log.info("Canonical store is empty; bootstrapping V8 objects from existing wiki pages.")
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
        "memory_type_counts": copy.deepcopy(memory_objects.get("memory_type_counts", {})),
        "source_index": copy.deepcopy(sources["items"]),
        "pending_change_set_count": len([item for item in queue["items"] if item.get("status") == "pending"]),
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


def _history_page_key(value: object) -> str:
    page_key = os.path.basename(str(value or "").strip())
    return page_key[:-3] if page_key.casefold().endswith(".md") else page_key


def _history_json_object(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _history_active_protections(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Collect identifiers that unfinished work may still need."""
    protected = {
        "claim_ids": set(),
        "claim_family_ids": set(),
        "evidence_ids": set(),
        "evidence_family_ids": set(),
        "page_keys": set(),
        "block_version_retention": set(),
    }
    terminal_change = ",".join("?" for _ in _HISTORY_TERMINAL_CHANGE_SET_STATUSES)
    rows = conn.execute(
        "SELECT data_json FROM change_sets WHERE "
        "CASE WHEN json_valid(data_json) THEN "
        "LOWER(COALESCE(json_extract(data_json, '$.status'), '')) ELSE '' END "
        f"NOT IN ({terminal_change})",
        _HISTORY_TERMINAL_CHANGE_SET_STATUSES,
    ).fetchall()
    for row in rows:
        change_set = _history_json_object(row["data_json"])
        if not change_set:
            protected["block_version_retention"].add("malformed_change_set")
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
    for row in conn.execute(
        "SELECT filename FROM mutation_outbox "
        f"WHERE COALESCE(status, '') NOT IN ({terminal_outbox})",
        _HISTORY_TERMINAL_OUTBOX_STATUSES,
    ).fetchall():
        if page_key := _history_page_key(row["filename"]):
            protected["page_keys"].add(page_key)
        else:
            protected["block_version_retention"].add(
                "missing_active_outbox_filename"
            )

    terminal_jobs = ",".join("?" for _ in _HISTORY_TERMINAL_JOB_STATUSES)
    rows = conn.execute(
        "SELECT payload FROM jobs WHERE status IS NULL OR "
        "(status = 'failed' AND COALESCE(retries, 0) < 3) OR "
        f"(status != 'failed' AND status NOT IN ({terminal_jobs}))",
        _HISTORY_TERMINAL_JOB_STATUSES,
    ).fetchall()
    for row in rows:
        payload = _history_json_object(row["payload"])
        if not payload:
            protected["block_version_retention"].add(
                "malformed_active_job_payload"
            )
            continue
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
    terminal = ",".join("?" for _ in _HISTORY_TERMINAL_CHANGE_SET_STATUSES)
    queue_terminal = ",".join("?" for _ in _HISTORY_TERMINAL_QUEUE_STATUSES)
    rows = conn.execute(
        "WITH terminal AS ("
        " SELECT change_set_id, COALESCE(updated_at, '') AS retained_at"
        " FROM change_sets WHERE json_valid(data_json) "
        " AND LOWER(COALESCE(json_extract(data_json, '$.status'), '')) "
        f" IN ({terminal})"
        "), ranked AS ("
        " SELECT change_set_id, retained_at,"
        " ROW_NUMBER() OVER (ORDER BY retained_at DESC, change_set_id DESC) AS retain_rank"
        " FROM terminal"
        ") SELECT candidate.change_set_id FROM ranked AS candidate "
        "WHERE julianday(candidate.retained_at) IS NOT NULL "
        "AND julianday(candidate.retained_at) < julianday(?) "
        "AND candidate.retain_rank > ? "
        "AND NOT EXISTS ("
        " SELECT 1 FROM governance_queue "
        " WHERE COALESCE(json_valid(data_json), 0) = 0"
        ") AND NOT EXISTS ("
        " SELECT 1 FROM governance_queue AS queue "
        " WHERE json_valid(queue.data_json) "
        " AND json_extract(queue.data_json, '$.change_set_id') = candidate.change_set_id "
        " AND LOWER(COALESCE(json_extract(queue.data_json, '$.status'), '')) "
        f" NOT IN ({queue_terminal})"
        ") ORDER BY candidate.retained_at ASC, candidate.change_set_id ASC LIMIT ?",
        (
            *_HISTORY_TERMINAL_CHANGE_SET_STATUSES,
            cutoff,
            keep_latest,
            *_HISTORY_TERMINAL_QUEUE_STATUSES,
            batch_size,
        ),
    ).fetchall()
    return [str(row["change_set_id"]) for row in rows]

def _select_job_retention_candidates(
    conn: sqlite3.Connection,
    cutoff: str,
    batch_size: int,
    keep_latest: int,
) -> list[str]:
    rows = conn.execute(
        "WITH terminal AS ("
        " SELECT job_id, task_packet_path,"
        " COALESCE(completed_at, updated_at, created_at, '') AS retained_at"
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
        " SELECT id, COALESCE(completed_at, available_at, created_at, '') AS retained_at"
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
) -> tuple[list[str], dict[str, int]]:
    allowed = {
        (
            "claim_versions",
            "claim_version_id",
            "claim_id",
            "claim_family_id",
            "claims",
        ),
        (
            "evidence_versions",
            "evidence_version_id",
            "evidence_id",
            "evidence_family_id",
            "evidence",
        ),
    }
    identity = (
        table_name,
        version_id_field,
        id_field,
        family_field,
        canonical_table,
    )
    if identity not in allowed:
        raise ValueError(f"Unsupported history table: {table_name}")
    if protected["block_version_retention"]:
        return [], {
            "active_work": 0,
            "current_canonical": 0,
            "malformed_canonical": 0,
            "scanned": 0,
            "blocked_by_unknown_active_work": len(
                protected["block_version_retention"]
            ),
        }
    rows = conn.execute(
        f"WITH ranked AS ("
        f" SELECT {version_id_field}, {id_field}, {family_field}, page_key, "
        f" record_hash, recorded_at, version_no, "
        f" ROW_NUMBER() OVER (PARTITION BY {family_field} "
        f" ORDER BY version_no DESC, recorded_at DESC, {version_id_field} DESC) AS family_rank "
        f" FROM {table_name}"
        f") SELECT candidate.*, canonical.data_json AS current_data_json "
        f"FROM ranked AS candidate LEFT JOIN {canonical_table} AS canonical "
        f"ON canonical.{id_field} = candidate.{id_field} "
        f"WHERE julianday(candidate.recorded_at) IS NOT NULL "
        f"AND julianday(candidate.recorded_at) < julianday(?) "
        f"AND candidate.family_rank > ? "
        f"ORDER BY candidate.recorded_at ASC, candidate.{version_id_field} ASC",
        (cutoff, keep_per_family),
    )
    protected_ids = protected[f"{id_field}s"]
    protected_families = protected[f"{family_field}s"]
    protected_pages = protected["page_keys"]
    selected: list[str] = []
    skipped = {
        "active_work": 0,
        "current_canonical": 0,
        "malformed_canonical": 0,
        "scanned": 0,
        "blocked_by_unknown_active_work": 0,
    }
    for row in rows:
        skipped["scanned"] += 1
        page_key = _history_page_key(row["page_key"])
        if (
            str(row[id_field]) in protected_ids
            or str(row[family_field]) in protected_families
            or (page_key and page_key in protected_pages)
        ):
            skipped["active_work"] += 1
            continue
        current_data = row["current_data_json"]
        if current_data is not None:
            current_record = _history_json_object(current_data)
            if not current_record:
                skipped["malformed_canonical"] += 1
                continue
            current_hash = hashlib.sha256(
                _canonical_record_json(current_record).encode("utf-8")
            ).hexdigest()
            if current_hash == str(row["record_hash"] or ""):
                skipped["current_canonical"] += 1
                continue
        selected.append(str(row[version_id_field]))
        if len(selected) >= batch_size:
            break
    return selected, skipped

def plan_history_retention(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    batch_size: int = 500,
    keep_change_sets: int = 1000,
    keep_terminal_jobs: int = 1000,
    keep_terminal_outbox: int = 1000,
    keep_versions_per_family: int = 2,
) -> dict:
    """Select bounded history rows without mutating the supplied database."""
    if batch_size < 1 or batch_size > 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    for name, value in (
        ("keep_change_sets", keep_change_sets),
        ("keep_terminal_jobs", keep_terminal_jobs),
        ("keep_terminal_outbox", keep_terminal_outbox),
    ):
        if int(value) < 0:
            raise ValueError(f"{name} must be zero or positive")
    if keep_versions_per_family < 1:
        raise ValueError("keep_versions_per_family must be positive")

    protected = _history_active_protections(conn)
    selected: dict[str, list] = {
        "change_sets": _select_change_set_retention_candidates(
            conn,
            cutoff,
            batch_size,
            int(keep_change_sets),
        ),
        "jobs": _select_job_retention_candidates(
            conn,
            cutoff,
            batch_size,
            int(keep_terminal_jobs),
        ),
        "mutation_outbox": _select_outbox_retention_candidates(
            conn,
            cutoff,
            batch_size,
            int(keep_terminal_outbox),
        ),
    }
    claim_versions, claim_skipped = _select_version_retention_candidates(
        conn,
        table_name="claim_versions",
        version_id_field="claim_version_id",
        id_field="claim_id",
        family_field="claim_family_id",
        canonical_table="claims",
        cutoff=cutoff,
        batch_size=batch_size,
        keep_per_family=int(keep_versions_per_family),
        protected=protected,
    )
    evidence_versions, evidence_skipped = _select_version_retention_candidates(
        conn,
        table_name="evidence_versions",
        version_id_field="evidence_version_id",
        id_field="evidence_id",
        family_field="evidence_family_id",
        canonical_table="evidence",
        cutoff=cutoff,
        batch_size=batch_size,
        keep_per_family=int(keep_versions_per_family),
        protected=protected,
    )
    selected["claim_versions"] = claim_versions
    selected["evidence_versions"] = evidence_versions
    table_counts = {
        table_name: int(
            conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        )
        for table_name in _HISTORY_RETENTION_TABLES
    }
    return {
        "cutoff": cutoff,
        "rules": {
            "batch_size_per_table": batch_size,
            "keep_change_sets": int(keep_change_sets),
            "keep_terminal_jobs": int(keep_terminal_jobs),
            "keep_terminal_outbox": int(keep_terminal_outbox),
            "keep_versions_per_family": int(keep_versions_per_family),
        },
        "table_counts_before": table_counts,
        "selected_ids": selected,
        "selected_counts": {
            table_name: len(selected.get(table_name, []))
            for table_name in _HISTORY_RETENTION_TABLES
        },
        "active_protection_counts": {
            name: len(values) for name, values in protected.items()
        },
        "version_skip_counts": {
            "claim_versions": claim_skipped,
            "evidence_versions": evidence_skipped,
        },
    }


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
) -> dict[str, int]:
    """Delete a plan computed under the caller's current write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("History retention apply requires an active transaction")
    selected = plan.get("selected_ids") or {}
    unknown = set(selected) - set(_HISTORY_RETENTION_TABLES)
    if unknown:
        raise ValueError(f"Unsupported history plan tables: {sorted(unknown)}")

    change_set_ids = list(selected.get("change_sets") or [])
    job_ids = list(selected.get("jobs") or [])
    _delete_history_ids(
        conn,
        "change_set_idempotency",
        "change_set_id",
        change_set_ids,
    )
    _delete_history_ids(
        conn,
        "ingest_task_cleanup",
        "job_id",
        job_ids,
        extra_predicate="AND status = 'completed'",
    )
    return {
        "change_sets": _delete_history_ids(
            conn,
            "change_sets",
            "change_set_id",
            change_set_ids,
        ),
        "jobs": _delete_history_ids(conn, "jobs", "job_id", job_ids),
        "mutation_outbox": _delete_history_ids(
            conn,
            "mutation_outbox",
            "id",
            list(selected.get("mutation_outbox") or []),
        ),
        "claim_versions": _delete_history_ids(
            conn,
            "claim_versions",
            "claim_version_id",
            list(selected.get("claim_versions") or []),
        ),
        "evidence_versions": _delete_history_ids(
            conn,
            "evidence_versions",
            "evidence_version_id",
            list(selected.get("evidence_versions") or []),
        ),
    }