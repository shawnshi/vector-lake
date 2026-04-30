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
from vector_lake.wiki_utils import (
    atomic_write_text,
    get_alias_registry_path,
    get_change_sets_path,
    get_claims_path,
    get_entities_path,
    get_evidence_path,
    get_governance_queue_path,
    get_meta_dir,
    get_memory_objects_path,
    get_sources_path,
    get_wiki_dir,
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


def initialize_meta_store():
    get_meta_dir().mkdir(parents=True, exist_ok=True)
    for path, default in [
        (get_entities_path(), _default_map_store("entity_id")),
        (get_claims_path(), _default_map_store("claim_id")),
        (get_evidence_path(), _default_map_store("evidence_id")),
        (get_sources_path(), _default_map_store("source_id")),
        (get_alias_registry_path(), _default_map_store("alias")),
        (get_memory_objects_path(), _default_map_store("memory_id")),
        (get_change_sets_path(), _default_queue_store()),
        (get_governance_queue_path(), _default_queue_store()),
    ]:
        if not path.exists():
            atomic_write_text(path, json.dumps(default, ensure_ascii=False, indent=2))


def _count_wiki_pages() -> int:
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return 0
    return len([
        name for name in os.listdir(wiki_dir)
        if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md")
    ])


def _load_json(path, default_factory):
    initialize_meta_store()
    with _lock_for(path):
        if not path.exists():
            data = default_factory()
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
            return data
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            data = default_factory()
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
            return data


def _save_json(path, data):
    data["updated_at"] = _utc_now()
    with _lock_for(path):
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_entities():
    return _load_json(get_entities_path(), lambda: _default_map_store("entity_id"))


def load_claims():
    return _load_json(get_claims_path(), lambda: _default_map_store("claim_id"))


def load_evidence():
    return _load_json(get_evidence_path(), lambda: _default_map_store("evidence_id"))


def load_sources():
    return _load_json(get_sources_path(), lambda: _default_map_store("source_id"))


def load_alias_registry():
    return _load_json(get_alias_registry_path(), lambda: _default_map_store("alias"))


def load_memory_objects():
    return _load_json(get_memory_objects_path(), lambda: _default_map_store("memory_id"))


def load_change_sets():
    return _load_json(get_change_sets_path(), _default_queue_store)


def load_governance_queue():
    return _load_json(get_governance_queue_path(), _default_queue_store)


def save_entities(data):
    _save_json(get_entities_path(), data)


def save_claims(data):
    _save_json(get_claims_path(), data)


def save_evidence(data):
    _save_json(get_evidence_path(), data)


def save_sources(data):
    _save_json(get_sources_path(), data)


def save_alias_registry(data):
    _save_json(get_alias_registry_path(), data)


def save_memory_objects(data):
    _save_json(get_memory_objects_path(), data)


def save_change_sets(data):
    _save_json(get_change_sets_path(), data)


def save_governance_queue(data):
    _save_json(get_governance_queue_path(), data)


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
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
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
    store = load_memory_objects()
    if not store.get("items") and load_claims().get("items"):
        store = rebuild_operational_memory()

    allowed_types = None
    if memory_types:
        allowed_types = {str(item).strip().lower().replace("-", "_") for item in memory_types}

    terms = _query_terms(query)
    ranked = []
    hidden_states = {"archived", "expired", "superseded"}
    for memory in store.get("items", {}).values():
        memory_type = str(memory.get("memory_type", "fact")).lower()
        if allowed_types and memory_type not in allowed_types:
            continue
        state = str(memory.get("validity_state", "active")).lower()
        if not include_history and state in hidden_states:
            continue
        relevance = _memory_relevance(memory, terms)
        if relevance <= 0 and terms:
            continue
        score = relevance + (float(memory.get("memory_score", 0) or 0) * 5)
        item = copy.deepcopy(memory)
        item["retrieval_score"] = round(score, 4)
        ranked.append(item)

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
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
        "proposed_evidence": proposed_evidence,
        "proposed_source_updates": proposed_source_updates,
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
    entities = load_entities()
    claims = load_claims()
    evidence = load_evidence()
    sources = load_sources()

    _upsert_map_records(entities, change_set.get("proposed_entities", []), "entity_id")
    _upsert_map_records(claims, change_set.get("proposed_claims", []), "claim_id")
    _upsert_map_records(evidence, change_set.get("proposed_evidence", []), "evidence_id")
    _upsert_map_records(sources, change_set.get("proposed_source_updates", []), "source_id")

    save_entities(entities)
    save_claims(claims)
    save_evidence(evidence)
    save_sources(sources)
    rebuild_alias_registry()
    try:
        memory_store = rebuild_operational_memory()
        change_set["operational_memory_count"] = len(memory_store.get("items", {}))
        change_set["conflict_event_count"] = len(memory_store.get("conflict_events", []))
    except Exception as exc:
        log.warning(f"Operational memory rebuild failed for {change_set.get('change_set_id')}: {exc}")
    try:
        from vector_lake import view_builder

        change_set["view_rebuild"] = view_builder.rebuild_views_for_change_set(change_set)
    except Exception as exc:
        log.warning(f"View rebuild failed for {change_set.get('change_set_id')}: {exc}")
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


def resolve_governance_item(item_id: str, resolution: str = "skip") -> dict | None:
    queue = load_governance_queue()
    for item in queue["items"]:
        if item.get("item_id") != item_id:
            continue
        # We allow re-resolving if it's already resolved but we need to re-apply the merge
        if item.get("status") != "pending" and not (item.get("status") == "resolved" and resolution == "merge"):
            continue
        
        # ACTUALLY PERFORM THE MERGE LOGIC HERE IF RESOLUTION IS MERGE
        if resolution == "merge" and item.get("type") == "merge":
            candidate = item.get("merge_candidate")
            if candidate:
                left_id = candidate.get("left_entity_id")
                right_id = candidate.get("right_entity_id")
                if left_id and right_id:
                    # Update the alias registry to map right to left
                    registry = load_alias_registry()
                    registry["items"][right_id] = left_id
                    save_alias_registry(registry)
                    
                    # Update entities file to mark right_id as merged/deprecated
                    entities = load_entities()
                    if right_id in entities["items"]:
                        entities["items"][right_id]["status"] = "Merged"
                        entities["items"][right_id]["merged_into"] = left_id
                        save_entities(entities)

        item["status"] = "resolved"
        item["resolution"] = resolution
        item["resolved_at"] = _utc_now()
        save_governance_queue(queue)
        return item
    return None


def sync_pages_to_canonical(page_paths: list[str], origin: str, auto_approve: bool = True, summary: str | None = None) -> dict | None:
    existing_paths = [str(path) for path in page_paths if path and os.path.exists(path)]
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
    queue["items"].append({
        "item_id": f"gov_{uuid.uuid4().hex[:12]}",
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


