"""Deprecated semantic-dedup operator script, disabled by default."""

import os
import json
import logging
import uuid
import asyncio
from difflib import SequenceMatcher
from datetime import datetime, timezone
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("semantic-dedup-daemon")

_LEGACY_DAEMON_ENV = "VECTOR_LAKE_ENABLE_LEGACY_UNSAFE_DAEMONS"
_DISABLED_EXIT_CODE = 78

EMBEDDING_MODEL = "gemini-embedding-2"
SIMILARITY_THRESHOLD = 0.92
ADVANCED_THRESHOLD = 0.94
CONCURRENCY_LIMIT = 20

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_daemon_enabled() -> bool:
    return os.environ.get(_LEGACY_DAEMON_ENV) == "1"


def _require_legacy_daemon() -> None:
    if not _legacy_daemon_enabled():
        raise PermissionError(
            "Deprecated/unsupported semantic dedup daemon is disabled; set "
            f"{_LEGACY_DAEMON_ENV}=1 only in a trusted operator process"
        )


def _enable_repo_imports() -> None:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def _get_cache_path():
    _require_legacy_daemon()
    _enable_repo_imports()
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "embeddings.pkl"

def load_cache() -> dict:
    import pickle
    cache_path = _get_cache_path()
    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            log.warning(f"Failed to load embeddings cache: {e}")
    return {"schema_version": "1.0", "embeddings": {}}

def save_cache(cache: dict):
    import pickle
    cache_path = _get_cache_path()
    with open(cache_path, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)


async def _unsafe_async_run_daemon():
    _require_legacy_daemon()
    _enable_repo_imports()
    from vector_lake import governance_store
    from vector_lake.wiki_utils import (
        calculate_cosine_similarity,
        get_index_path,
        normalize_memory_key as strip_name,
    )

    if os.environ.get("GEMINI_API_KEY"):
        log.info("Semantic dedup never calls the provider directly; use embedding-backfill for missing vectors.")

    index_path = get_index_path()
    if not index_path.exists():
        log.info("No index.json found. Skipping deduplication.")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    nodes = index_data.get("nodes", {})
    entities = {
        key: node for key, node in nodes.items()
        if node.get("type") in ("vendor", "product", "person", "event", "concept", "synthesis", "source")
    }

    for key, node in entities.items():
        node['_stripped'] = strip_name(node.get('title', key))
        aliases = node.get('aliases', [])
        if isinstance(aliases, str):
            aliases = [aliases]
        node['_stripped_aliases'] = [strip_name(a) for a in aliases if a]
        node['backlinks'] = []

    for key, node in index_data.get("nodes", {}).items():
        for link in node.get("links", []):
            if link in entities:
                entities[link]["backlinks"].append(key)

    cached_embeddings = {}
    try:
        from array import array
        from vector_lake.db_store import get_connection

        for row in get_connection().execute("SELECT entity_id, embedding FROM vec_embeddings"):
            key = str(row["entity_id"])
            if key not in entities:
                continue
            values = array("f")
            values.frombytes(bytes(row["embedding"]))
            cached_embeddings[key] = {
                "vector": list(values),
                "title": entities[key].get("title", key),
            }
    except Exception as exc:
        log.warning("Could not load SQLite embeddings; continuing with lexical/topology dedup: %s", exc)

    entity_keys = list(entities.keys())
    entity_keys.sort(key=lambda k: entities[k]['_stripped'])
    
    candidates = []
    queue = governance_store.load_governance_queue()
    existing_pairs = {item.get("pair_key") for item in queue["items"] if item.get("type") == "merge"}

    log.info("Computing Multi-Layer Pairwise Similarities (Async)...")
    window_size = min(150, len(entity_keys))
    
    for i in range(len(entity_keys)):
        if i % 10 == 0:
            await asyncio.sleep(0)  # Yield to prevent blocking
        left_key = entity_keys[i]
        left_node = entities[left_key]
        left_strip = left_node['_stripped']
        left_title = left_node.get('title', left_key)
        
        for j in range(i + 1, min(i + window_size, len(entity_keys))):
            right_key = entity_keys[j]
            right_node = entities[right_key]
            right_strip = right_node['_stripped']
            right_title = right_node.get('title', right_key)
            
            pair_key = "::".join(sorted([left_key, right_key]))
            if pair_key in existing_pairs:
                continue
            if left_key.startswith('Source_') and right_key.startswith('Source_'):
                continue
                
            collision = False
            if len(left_strip) > 2 and left_strip == right_strip:
                collision = True
            elif (
                left_strip in right_node['_stripped_aliases']
                or right_strip in left_node['_stripped_aliases']
            ):
                collision = True
                
            b1, b2 = set(left_node.get('backlinks', [])), set(right_node.get('backlinks', []))
            jaccard = (len(b1.intersection(b2)) / len(b1.union(b2))) if b1 or b2 else 0.0
                
            str_score = SequenceMatcher(None, left_strip, right_strip).ratio()
            final_score, reasons = str_score, []
            
            if collision:
                final_score = 1.0
                reasons.append("metadata-alias-collision")
            elif jaccard > 0.6:
                final_score = min(final_score + 0.15, 0.99)
                reasons.append(f"topology-jaccard:{round(jaccard, 2)}")
            
            if left_key in cached_embeddings and right_key in cached_embeddings:
                sim = calculate_cosine_similarity(cached_embeddings[left_key]["vector"], cached_embeddings[right_key]["vector"])
                if sim >= SIMILARITY_THRESHOLD:
                    if sim > final_score:
                        final_score = sim
                    reasons.append(f"semantic-embedding-match:{round(sim, 3)}")
            
            if final_score >= ADVANCED_THRESHOLD:
                if not reasons:
                    reasons.append(f"lexical-similarity:{round(str_score, 3)}")
                reasons.append("local-review-required")
                candidates.append({
                    "pair_key": pair_key, "score": round(final_score, 3), "left_entity_id": left_key, "left_name": left_title,
                    "right_entity_id": right_key, "right_name": right_title, "reasons": reasons
                })
                existing_pairs.add(pair_key)

        if cached_embeddings:
            left_data = cached_embeddings.get(left_key)
            if not left_data or "vector" not in left_data:
                continue
            
            for j in range(i + 1, len(entity_keys)):
                if j < i + window_size:
                    continue
                
                right_key = entity_keys[j]
                right_data = cached_embeddings.get(right_key)
                if not right_data or "vector" not in right_data:
                    continue
                
                pair_key = "::".join(sorted([left_key, right_key]))
                if pair_key in existing_pairs:
                    continue
                if left_key.startswith('Source_') and right_key.startswith('Source_'):
                    continue
                    
                sim = calculate_cosine_similarity(left_data["vector"], right_data["vector"])
                if sim >= ADVANCED_THRESHOLD:
                    right_node = entities[right_key]
                    right_title = right_node.get('title', right_key)
                    candidates.append({
                        "pair_key": pair_key, "score": round(sim, 3), "left_entity_id": left_key, "left_name": left_title,
                        "right_entity_id": right_key, "right_name": right_title, "reasons": [f"semantic-embedding-match:{round(sim, 3)}", "local-review-required"]
                    })
                    existing_pairs.add(pair_key)

    if candidates:
        log.info(f"Found {len(candidates)} new merge candidates. Enqueueing...")
        created = 0
        for suggestion in candidates:
            queue["items"].append({
                "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                "type": "merge",
                "title": f"Merge candidate: {suggestion['left_name']} <> {suggestion['right_name']}",
                "description": "; ".join(suggestion["reasons"]),
                "created_at": _utc_now(),
                "status": "pending",
                "source": "semantic-dedup-daemon-async",
                "pair_key": suggestion["pair_key"],
                "affected_ids": [suggestion["left_entity_id"], suggestion["right_entity_id"]],
                "search_queries": [suggestion["left_name"], suggestion["right_name"]],
                "affected_pages": [],
                "merge_candidate": suggestion,
            })
            created += 1
            
        governance_store.save_governance_queue(queue)
        log.info(f"Enqueued {created} multi-layer merge candidates.")
    else:
        log.info("No new merge candidates found.")

def _run_legacy_daemon() -> None:
    asyncio.run(_unsafe_async_run_daemon())


def main() -> int:
    if not _legacy_daemon_enabled():
        print(
            "DEPRECATED/UNSUPPORTED semantic dedup daemon is disabled by default; "
            f"set {_LEGACY_DAEMON_ENV}=1 only for isolated operator recovery.",
            file=sys.stderr,
        )
        return _DISABLED_EXIT_CODE
    print(
        "DEPRECATED/UNSUPPORTED semantic dedup daemon override enabled; never run "
        "it alongside watchdog_sync.py or vector_lake.watchdog_app.",
        file=sys.stderr,
    )
    _run_legacy_daemon()
    return 0


def run_daemon() -> int:
    """Compatibility entrypoint retaining the same fail-closed operator gate."""
    return main()

if __name__ == "__main__":
    raise SystemExit(main())
