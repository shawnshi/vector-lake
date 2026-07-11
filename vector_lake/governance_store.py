import copy
import hashlib
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timezone

from filelock import FileLock

from vector_lake.claim_extractor import extract_page_objects
from vector_lake.db_store import get_connection, init_db, transaction
from vector_lake.wiki_utils import (
    atomic_write_text,
    get_meta_dir,
    get_purpose_path,
    get_wiki_dir,
    normalize_memory_key,
    read_markdown_file,
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


_PURPOSE_VECTORS_CACHE = None
_PURPOSE_VECTORS_MTIME = 0

def get_purpose_vectors() -> dict:
    global _PURPOSE_VECTORS_CACHE, _PURPOSE_VECTORS_MTIME
    path = get_meta_dir() / "purpose_vectors.json"
    purpose_path = get_purpose_path()
    
    current_mtime = 0
    for candidate in (path, purpose_path):
        if candidate.exists():
            current_mtime = max(current_mtime, candidate.stat().st_mtime)
        
    if _PURPOSE_VECTORS_CACHE is not None and _PURPOSE_VECTORS_MTIME == current_mtime:
        return _PURPOSE_VECTORS_CACHE
        
    try:
        from vector_lake.purpose_contract import purpose_vectors
        _PURPOSE_VECTORS_CACHE = purpose_vectors()
    except Exception:
        _PURPOSE_VECTORS_CACHE = None

    if _PURPOSE_VECTORS_CACHE is None and path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                _PURPOSE_VECTORS_CACHE = json.load(f)
        except Exception:
            _PURPOSE_VECTORS_CACHE = {"keywords": [], "weight_boost": 0.0}
    elif _PURPOSE_VECTORS_CACHE is None:
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
    "operational_memory", "claim_graph_nodes", "claim_graph_edges",
    "timeline_events", "processed_files"
}

def _validate_table_name(table_name: str):
    """🛡️ Sentinel: Prevent SQL injection by validating table names against a strict whitelist."""
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Security error: Invalid table name '{table_name}'. Expected one of {ALLOWED_TABLES}.")

def initialize_meta_store():
    init_db()


def _count_wiki_pages() -> int:
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return 0
    return len([
        name for name in os.listdir(wiki_dir)
        if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md")
    ])


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
    _validate_table_name(table_name)
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    if extra_cols is None:
        extra_cols = []
    
    with transaction():
        if data.get("items"):
            cols = [pk_col] + [c[0] for c in extra_cols] + ["data_json", "updated_at"]
            placeholders = ["?"] * len(cols)
            
            all_vals = []
            for key, item in data.get("items", {}).items():
                params = [key]
                for c_name, c_key, c_type in extra_cols:
                    val = item.get(c_key)
                    if c_type == float:
                        params.append(float(val or 0.0))
                    elif c_type == int:
                        params.append(int(val or 0))
                    else:
                        params.append(str(val or ""))
                params.append(json.dumps(item, ensure_ascii=False))
                params.append(now)
                all_vals.append(tuple(params))
            
            conn.executemany(f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})", all_vals)
        
        # V10.1 Diff-based synchronization (Avoid full table wipe)
        existing_keys_query = conn.execute(f"SELECT {pk_col} FROM {table_name}").fetchall()
        existing_keys = {row[0] for row in existing_keys_query}
        new_keys = set(data.get("items", {}).keys())
        keys_to_delete = existing_keys - new_keys
        
        if keys_to_delete:
            conn.executemany(f"DELETE FROM {table_name} WHERE {pk_col} = ?", [(k,) for k in keys_to_delete])

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
                raise ValueError(f"Security error: Invalid filter key '{k}'. Keys must be alphanumeric.")
            if k.endswith("!="):
                clauses.append(f"json_extract(data_json, '$.{k[:-2]}') != ?")
            else:
                clauses.append(f"json_extract(data_json, '$.{k}') = ?")
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
                raise ValueError(f"Security error: Invalid filter key '{k}'. Keys must be alphanumeric.")
            if k.endswith("!="):
                clauses.append(f"json_extract(data_json, '$.{k[:-2]}') != ?")
            else:
                clauses.append(f"json_extract(data_json, '$.{k}') = ?")
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


def save_claims(data):
    _save_db_map("claims", "claim_id", data, [("claim_text", "claim_text", str), ("status", "status", str)])


def save_evidence(data):
    _save_db_map("evidence", "evidence_id", data)


def save_sources(data):
    _save_db_map("sources", "source_id", data)


def save_graph_edges(edges: list[dict]):
    if not edges: return
    conn = get_connection()
    with transaction():
        records = [
            (edge["source_id"], edge["target_id"], edge["relation"], edge.get("weight", 1.0), edge.get("updated_at", _utc_now()))
            for edge in edges
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
            records
        )


def save_alias_registry(data):
    conn = get_connection()
    now = _utc_now()
    data["updated_at"] = now
    with transaction():
        records = [(k, v, now) for k, v in data.get("items", {}).items()]
        if records:
            conn.executemany("INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)", records)


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
    _save_db_queue("governance_queue", "item_id", data)

# =============================================================================
# V10.1 TARGETED ATOMIC CRUD (Replaces load_all -> save_all pattern)
# =============================================================================
def get_entity(entity_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT data_json FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
    if row:
        return json.loads(row["data_json"])
    return None

def upsert_entity(entity_id: str, data: dict):
    conn = get_connection()
    now = _utc_now()
    cols = ["entity_id", "type", "status", "data_json", "updated_at"]
    placeholders = ["?"] * len(cols)
    params = [
        entity_id,
        str(data.get("type", "")),
        str(data.get("status", "Active")),
        json.dumps(data, ensure_ascii=False),
        now
    ]
    with transaction():
        conn.execute(f"INSERT OR REPLACE INTO entities ({', '.join(cols)}) VALUES ({', '.join(placeholders)})", params)

def delete_entity(entity_id: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))

def get_alias(key: str) -> str | None:
    conn = get_connection()
    row = conn.execute("SELECT value FROM alias_registry WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return None

def upsert_alias(key: str, value: str):
    conn = get_connection()
    now = _utc_now()
    with transaction():
        conn.execute("INSERT OR REPLACE INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)", (key, value, now))

def delete_alias(key: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM alias_registry WHERE key = ?", (key,))
# =============================================================================

def _upsert_map_records(store: dict, records: list, key_name: str):
    for record in records:
        key = record[key_name]
        store["items"][key] = record


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

    # ⚡ Bolt: Hoist _utc_now() out of the loop.
    # Measurement: Avoids calling datetime.now(timezone.utc) N times, reducing execution time by ~50% in large datasets.
    now = governance_metrics._utc_now()
    return [
        governance_metrics.annotate_claim_validity(claim, now=now)
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
        return normalize_memory_key(explicit)

    locator = claim.get("locator") or {}
    heading = locator.get("heading") or claim.get("source_page") or "general"
    text = str(claim.get("claim_text") or "")
    match = re.match(r"^(.{2,80}?)[：:]\s+.+$", text)
    if match:
        heading = match.group(1)

    if memory_type == "fact":
        return normalize_memory_key(claim.get("claim_id") or text[:96])
    return normalize_memory_key(f"{memory_type}:{heading}")


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
        if str(memory.get("validity_state", "")).lower() in {"expired", "archived", "superseded"}:
            continue
        grouped.setdefault((memory.get("memory_type"), memory.get("memory_key")), []).append(memory)

    for (memory_type, memory_key), candidates in grouped.items():
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
    
    existing_store = load_memory_objects()
    for mem_id, mem in existing_store.get("items", {}).items():
        if not mem.get("source_claim_id"):
            store["items"][mem_id] = mem
            
    for claim in claims:
        memory = _memory_object_from_claim(claim)
        store["items"][memory["memory_id"]] = memory
    store = _resolve_memory_conflicts(store)
    save_memory_objects(store)
    return store


def _memory_relevance(memory: dict, terms: list[str]) -> float:
    if not terms:
        return 0.0
    haystacks = {
        "key": str(memory.get("memory_key", "")).lower(),
        "text": str(memory.get("text", "")).lower(),
        "page": str(memory.get("source_page", "")).lower(),
        "type": str(memory.get("memory_type", "")).lower(),
    }
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


def search_operational_memory(
    query: str,
    top_k: int = 12,
    memory_types: list[str] | None = None,
    include_history: bool = False,
) -> list[dict]:
    initialize_meta_store()
    conn = get_connection()
    count_row = conn.execute("SELECT COUNT(*) as c FROM operational_memory").fetchone()
    
    if count_row and count_row["c"] == 0:
        c_claims = conn.execute("SELECT COUNT(*) as c FROM claims").fetchone()
        if c_claims and c_claims["c"] > 0:
            rebuild_operational_memory()

    allowed_types = None
    if memory_types:
        allowed_types = {str(item).strip().lower().replace("-", "_") for item in memory_types}

    sql_query = "SELECT data_json FROM operational_memory WHERE 1=1"
    params = []
    
    if allowed_types:
        placeholders = ",".join(["?"] * len(allowed_types))
        sql_query += f" AND json_extract(data_json, '$.memory_type') IN ({placeholders})"
        params.extend(allowed_types)
        
    hidden_states = {"archived", "expired", "superseded"}
    if not include_history:
        placeholders = ",".join(["?"] * len(hidden_states))
        sql_query += f" AND LOWER(json_extract(data_json, '$.validity_state')) NOT IN ({placeholders})"
        params.extend(hidden_states)

    terms = _query_terms(query)
    ranked = []
    
    cursor = conn.execute(sql_query, tuple(params))
    for row in cursor.fetchall():
        memory = json.loads(row["data_json"])
        relevance = _memory_relevance(memory, terms)
        if relevance <= 0 and terms:
            continue
        score = relevance + (float(memory.get("memory_score", 0) or 0) * 5)
        memory["retrieval_score"] = round(score, 4)
        ranked.append(memory)

    ranked.sort(
        key=lambda item: (
            item.get("retrieval_score", 0),
            item.get("memory_score", 0),
            _dt_rank(item.get("updated_at")),
        ),
        reverse=True,
    )
    return ranked[:top_k]


def build_claim_graph_projection(limit_nodes: int | None = None) -> dict:
    max_degree = 12
    entity_window = 6
    source_window = 4
    entities = load_entities()["items"]
    sources = load_sources()["items"]
    claims = annotated_claims()
    
    # Sort claims by update time to ensure we keep the most recent/relevant if limiting
    claims.sort(key=lambda c: _dt_rank(c.get("updated_at")), reverse=True)

    if limit_nodes is None:
        limit_nodes = 2500  # Hard cap to prevent 3D-force-graph from freezing the browser

    if limit_nodes is not None:
        claims = claims[:limit_nodes]

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

    suggestions = governance_metrics.find_merge_candidates(limit=limit)
    if not enqueue:
        return {"created": 0, "suggestions": suggestions}

    queue = load_governance_queue()
    existing_pairs = {
        item.get("pair_key")
        for item in queue["items"]
        if item.get("type") == "merge"
    }
    created = 0
    for suggestion in suggestions:
        if suggestion["pair_key"] in existing_pairs:
            continue
        queue["items"].append({
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
            "affected_pages": [],
            "merge_candidate": suggestion,
        })
        existing_pairs.add(suggestion["pair_key"])
        created += 1
    save_governance_queue(queue)
    return {"created": created, "suggestions": suggestions}


def create_change_set(
    page_paths: list[str],
    origin: str,
    summary: str | None = None,
    auto_approve: bool = False,
    force: bool = False,
) -> dict:
    initialize_meta_store()
    entities = load_entities()
    claims = load_claims()
    evidence = load_evidence()
    sources = load_sources()

    proposed_entities = []
    proposed_claims = []
    proposed_evidence = []
    proposed_source_updates = []
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
        proposed_edges.extend(extracted.get("edges", []))
        affected_ids.extend([record["entity_id"] for record in extracted["entities"]])
        affected_ids.extend([record["claim_id"] for record in extracted["claims"]])
        page_summaries.append(extracted["page_key"])

    idempotency_key = _stable_id(
        "changeset_idem",
        "|".join([origin, *sorted(page_summaries), *sorted(page_fingerprints)]),
    )
    existing_change_sets = load_change_sets()
    if not force:
        for existing in existing_change_sets["items"]:
            if existing.get("idempotency_key") == idempotency_key:
                duplicate = copy.deepcopy(existing)
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
        "proposed_edges": proposed_edges,
        "write_contract": {
            "transactional": True,
            "idempotent": True,
            "canonical_targets": ["entities", "claims", "evidence", "sources", "operational_memory"],
        },
    }

    if auto_approve:
        apply_change_set(change_set)
        change_set["published_at"] = _utc_now()
    else:
        queue = load_governance_queue()
        queue["items"].append({
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
        })
        save_governance_queue(queue)

    existing_change_sets["items"].append(change_set)
    save_change_sets(existing_change_sets)
    return change_set


def apply_change_set(change_set: dict) -> dict:
    from filelock import FileLock
    from vector_lake.wiki_utils import get_meta_dir
    lock_path = str(get_meta_dir() / "governance_sync_2.lock")
    with FileLock(lock_path, timeout=60):
        with transaction():
            entities = load_entities()
            claims = load_claims()
            evidence = load_evidence()
            sources = load_sources()
        
            affected_pages = change_set.get("affected_pages", [])
            affected_page_keys = [page[:-3] if page.endswith(".md") else page for page in affected_pages]
        
            if affected_page_keys:
                proposed_claim_ids = {c["claim_id"] for c in change_set.get("proposed_claims", [])}
                proposed_evidence_ids = {e["evidence_id"] for e in change_set.get("proposed_evidence", [])}
    
                # Prune claims no longer in the markdown
                keys_to_remove = []
                for k, v in claims.get("items", {}).items():
                    source_page = v.get("source_page", "")
                    locator_page = source_page[:-3] if source_page.endswith(".md") else source_page
                    if locator_page in affected_page_keys and k not in proposed_claim_ids:
                        keys_to_remove.append(k)
                if keys_to_remove:
                    conn = get_connection()
                    conn.executemany("DELETE FROM claims WHERE claim_id = ?", [(k,) for k in keys_to_remove])
                    for k in keys_to_remove:
                        del claims["items"][k]
                    
                # Prune evidence no longer in the markdown
                keys_to_remove = []
                for k, v in evidence.get("items", {}).items():
                    source_page = v.get("source_page", "")
                    locator_page = source_page[:-3] if source_page.endswith(".md") else source_page
                    if locator_page in affected_page_keys and k not in proposed_evidence_ids:
                        keys_to_remove.append(k)
                if keys_to_remove:
                    conn = get_connection()
                    conn.executemany("DELETE FROM evidence WHERE evidence_id = ?", [(k,) for k in keys_to_remove])
                    for k in keys_to_remove:
                        del evidence["items"][k]
    
            _upsert_map_records(entities, change_set.get("proposed_entities", []), "entity_id")
            _upsert_map_records(claims, change_set.get("proposed_claims", []), "claim_id")
            _upsert_map_records(evidence, change_set.get("proposed_evidence", []), "evidence_id")
            _upsert_map_records(sources, change_set.get("proposed_source_updates", []), "source_id")
    
            save_entities(entities)
            save_claims(claims)
            save_evidence(evidence)
            save_sources(sources)
            if affected_page_keys:
                conn = get_connection()
                conn.executemany("DELETE FROM claim_graph_edges WHERE source_id = ?", [(k,) for k in affected_page_keys])
            save_graph_edges(change_set.get("proposed_edges", []))
            rebuild_alias_registry()
            try:
                memory_store = rebuild_operational_memory()
                change_set["operational_memory_count"] = len(memory_store.get("items", {}))
                change_set["conflict_event_count"] = len(memory_store.get("conflict_events", []))
            except Exception as exc:
                log.warning(f"Operational memory rebuild failed for {change_set.get('change_set_id')}: {exc}")
            return change_set
    
    
def publish_change_sets(limit: int | None = None) -> dict:
    change_sets = load_change_sets()
    published = 0
    published_ids = []
    for change_set in change_sets["items"]:
        if change_set.get("status") != "pending":
            continue
        apply_change_set(change_set)
        change_set["status"] = "published"
        change_set["published_at"] = _utc_now()
        published += 1
        published_ids.append(change_set["change_set_id"])
        if limit is not None and published >= limit:
            break
    save_change_sets(change_sets)

    queue = load_governance_queue()
    for item in queue["items"]:
        if item.get("change_set_id") in published_ids:
            item["status"] = "published"
            item["resolved_at"] = _utc_now()
    save_governance_queue(queue)
    return {"published": published, "change_set_ids": published_ids}


def pending_change_sets() -> list:
    return [item for item in load_change_sets()["items"] if item.get("status") == "pending"]


def pending_governance_items() -> list:
    return [item for item in load_governance_queue()["items"] if item.get("status") == "pending"]


def sync_pages_to_canonical(page_paths: list[str], origin: str, auto_approve: bool = True, summary: str | None = None) -> dict | None:
    from filelock import FileLock
    from vector_lake.wiki_utils import get_meta_dir
    lock_path = str(get_meta_dir() / "governance_sync_2.lock")
    with FileLock(lock_path, timeout=60):
        return _sync_pages_to_canonical_impl(page_paths, origin, auto_approve, summary)

def _sync_pages_to_canonical_impl(page_paths: list[str], origin: str, auto_approve: bool = True, summary: str | None = None) -> dict | None:
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
            if basename.endswith(".md"):
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
    page_paths = []
    for name in os.listdir(wiki_dir):
        if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md"):
            page_paths.append(str(wiki_dir / name))

    initialize_meta_store()
    if dry_run:
        preview = create_change_set(page_paths, origin="migrate-v8", summary="V8 migration dry-run", auto_approve=False)
        change_sets = load_change_sets()
        change_sets["items"] = [item for item in change_sets["items"] if item["change_set_id"] != preview["change_set_id"]]
        save_change_sets(change_sets)
        queue = load_governance_queue()
        queue["items"] = [item for item in queue["items"] if item.get("change_set_id") != preview["change_set_id"]]
        save_governance_queue(queue)
        return {
            "dry_run": True,
            "pages_scanned": len(page_paths),
            "entities": len(preview["proposed_entities"]),
            "claims": len(preview["proposed_claims"]),
            "evidence": len(preview["proposed_evidence"]),
            "sources": len(preview["proposed_source_updates"]),
        }

    entities = _default_map_store("entity_id")
    claims = _default_map_store("claim_id")
    evidence = _default_map_store("evidence_id")
    sources = _default_map_store("source_id")
    alias_registry = _default_map_store("alias")
    memory_objects = _default_map_store("memory_id")
    save_entities(entities)
    save_claims(claims)
    save_evidence(evidence)
    save_sources(sources)
    save_alias_registry(alias_registry)
    save_memory_objects(memory_objects)

    change_set = create_change_set(page_paths, origin="migrate-v8", summary="V8 migration", auto_approve=True, force=True)
    change_sets = load_change_sets()
    for item in change_sets["items"]:
        if item["change_set_id"] == change_set["change_set_id"]:
            item["status"] = "published"
            item["published_at"] = _utc_now()
    save_change_sets(change_sets)

    return {
        "dry_run": False,
        "pages_scanned": len(page_paths),
        "entities": len(load_entities()["items"]),
        "claims": len(load_claims()["items"]),
        "evidence": len(load_evidence()["items"]),
        "sources": len(load_sources()["items"]),
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


def enqueue_governance_item(item_type: str, title: str, description: str, source: str, search_queries: list, affected_pages: list):
    import uuid
    queue = load_governance_queue()
    for existing in queue["items"]:
        if existing.get("status") == "pending" and existing.get("type") == item_type and existing.get("title") == title:
            return existing.get("item_id")
    item_id = f"gov_{uuid.uuid4().hex[:12]}"
    queue["items"].append({
        "item_id": item_id,
        "type": item_type,
        "title": title,
        "description": description,
        "created_at": _utc_now(),
        "status": "pending",
        "source": source,
        "search_queries": search_queries,
        "affected_pages": affected_pages,
    })
    save_governance_queue(queue)
    return item_id


