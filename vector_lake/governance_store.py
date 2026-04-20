import copy
import json
import logging
import os
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
    get_sources_path,
    get_wiki_dir,
    read_markdown_file,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-governance-store")

SCHEMA_VERSION = "8.0"


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


def create_change_set(page_paths: list[str], origin: str, summary: str | None = None, auto_approve: bool = False) -> dict:
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

    for page_path in page_paths:
        if not os.path.exists(page_path):
            continue
        frontmatter, body, _ = read_markdown_file(page_path)
        extracted = extract_page_objects(page_path, frontmatter, body)
        proposed_entities.extend(extracted["entities"])
        proposed_claims.extend(extracted["claims"])
        proposed_evidence.extend(extracted["evidence"])
        proposed_source_updates.extend(extracted["sources"])
        affected_ids.extend([record["entity_id"] for record in extracted["entities"]])
        affected_ids.extend([record["claim_id"] for record in extracted["claims"]])
        page_summaries.append(extracted["page_key"])

    change_set = {
        "change_set_id": f"changeset_{uuid.uuid4().hex[:12]}",
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

    change_sets = load_change_sets()
    change_sets["items"].append(change_set)
    save_change_sets(change_sets)
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
        from vector_lake import view_builder

        change_set["view_rebuild"] = view_builder.rebuild_views_for_change_set(change_set)
    except Exception as exc:
        log.warning(f"View rebuild failed for {change_set.get('change_set_id')}: {exc}")
    return change_set


def publish_change_sets(limit: int | None = None) -> dict:
    change_sets = load_change_sets()
    published = 0
    published_ids = []
    rebuilt_views = 0
    for change_set in change_sets["items"]:
        if change_set.get("status") != "pending":
            continue
        apply_change_set(change_set)
        change_set["status"] = "published"
        change_set["published_at"] = _utc_now()
        rebuilt_views += int(change_set.get("view_rebuild", {}).get("rebuilt", 0))
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
    return {"published": published, "change_set_ids": published_ids, "views_rebuilt": rebuilt_views}


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
    save_entities(entities)
    save_claims(claims)
    save_evidence(evidence)
    save_sources(sources)
    save_alias_registry(alias_registry)

    change_set = create_change_set(page_paths, origin="migrate-v8", summary="V8 migration", auto_approve=True)
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
    queue = load_governance_queue()
    annotated = annotated_claims()
    claim_index = {claim["claim_id"]: copy.deepcopy(claim) for claim in annotated}
    return {
        "entity_index": copy.deepcopy(entities["items"]),
        "claim_index": claim_index,
        "source_index": copy.deepcopy(sources["items"]),
        "pending_change_set_count": len([item for item in queue["items"] if item.get("status") == "pending"]),
        "claim_graph": build_claim_graph_projection(),
    }

