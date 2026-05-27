import os
import json
import hashlib
import logging
import math
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

# Add parent dir to sys.path so we can import vector_lake if run directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from google import genai
from vector_lake.wiki_utils import get_meta_dir, get_index_path
from vector_lake import governance_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("semantic-dedup-daemon")

EMBEDDING_MODEL = "gemini-embedding-2"
SIMILARITY_THRESHOLD = 0.85

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def calculate_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(a * a for a in v2))
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0

def _get_cache_path():
    return get_meta_dir() / "embeddings.json"

def load_cache() -> dict:
    cache_path = _get_cache_path()
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Failed to load embeddings cache: {e}")
    return {"schema_version": "1.0", "embeddings": {}}

def save_cache(cache: dict):
    cache_path = _get_cache_path()
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def run_daemon():
    if not os.environ.get("GEMINI_API_KEY"):
        log.warning("GEMINI_API_KEY not set. Semantic dedup daemon skipping.")
        return

    try:
        from google import genai
        client = genai.Client()
    except ImportError:
        log.warning("google-genai not installed. Semantic dedup daemon skipping.")
        return

    index_path = get_index_path()
    if not index_path.exists():
        log.info("No index.json found. Skipping deduplication.")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    nodes = index_data.get("nodes", {})
    entities = {
        key: node for key, node in nodes.items()
        if node.get("type") in ("vendor", "product", "person", "event", "concept")
    }

    cache = load_cache()
    cached_embeddings = cache.setdefault("embeddings", {})

    updates_made = False
    
    # 1. Update Embeddings
    log.info(f"Checking embeddings for {len(entities)} entities...")
    for key, node in entities.items():
        title = node.get("title", key)
        summary = node.get("summary") or title
        text_to_embed = f"Title: {title}\nSummary: {summary}"
        
        text_hash = hashlib.sha256(text_to_embed.encode("utf-8")).hexdigest()
        
        cached = cached_embeddings.get(key)
        if cached and cached.get("hash") == text_hash and "vector" in cached:
            continue
            
        try:
            log.info(f"Generating new embedding for: {title}")
            response = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text_to_embed
            )
            # Handle possible response structures
            if hasattr(response, 'embeddings') and len(response.embeddings) > 0:
                vector = response.embeddings[0].values
            else:
                log.warning(f"Unexpected embed response for {title}")
                continue
                
            cached_embeddings[key] = {
                "hash": text_hash,
                "vector": vector,
                "title": title
            }
            updates_made = True
            time.sleep(0.5) # rate limiting
        except Exception as e:
            log.error(f"Failed to generate embedding for {title}: {e}")

    if updates_made:
        save_cache(cache)

    # 2. Compute Pairwise Similarities
    entity_keys = list(entities.keys())
    candidates = []
    
    # Ensure governance store has queue
    queue = governance_store.load_governance_queue()
    existing_pairs = {
        item.get("pair_key")
        for item in queue["items"]
        if item.get("type") == "merge"
    }

    log.info("Computing pairwise semantic similarities...")
    for i in range(len(entity_keys)):
        left_key = entity_keys[i]
        left_data = cached_embeddings.get(left_key)
        if not left_data or "vector" not in left_data:
            continue
            
        for j in range(i + 1, len(entity_keys)):
            right_key = entity_keys[j]
            right_data = cached_embeddings.get(right_key)
            if not right_data or "vector" not in right_data:
                continue
                
            pair_key = "::".join(sorted([left_key, right_key]))
            if pair_key in existing_pairs:
                continue # Already suggested
                
            sim = calculate_cosine_similarity(left_data["vector"], right_data["vector"])
            if sim >= SIMILARITY_THRESHOLD:
                candidates.append({
                    "pair_key": pair_key,
                    "score": round(sim, 3),
                    "left_entity_id": left_key,
                    "left_name": left_data["title"],
                    "right_entity_id": right_key,
                    "right_name": right_data["title"],
                    "reasons": [f"semantic-embedding-match:{round(sim, 3)}"]
                })

    if candidates:
        log.info(f"Found {len(candidates)} new semantic merge candidates. Enqueueing...")
        created = 0
        for suggestion in candidates:
            if suggestion["pair_key"] in existing_pairs:
                continue
            queue["items"].append({
                "item_id": f"gov_{uuid.uuid4().hex[:12]}",
                "type": "merge",
                "title": f"Merge candidate: {suggestion['left_name']} <> {suggestion['right_name']}",
                "description": "; ".join(suggestion["reasons"]),
                "created_at": _utc_now(),
                "status": "pending",
                "source": "semantic-dedup-daemon",
                "pair_key": suggestion["pair_key"],
                "affected_ids": [suggestion["left_entity_id"], suggestion["right_entity_id"]],
                "search_queries": [suggestion["left_name"], suggestion["right_name"]],
                "affected_pages": [],
                "merge_candidate": suggestion,
            })
            existing_pairs.add(suggestion["pair_key"])
            created += 1
            
        governance_store.save_governance_queue(queue)
        log.info(f"Enqueued {created} semantic merge candidates.")
    else:
        log.info("No new semantic merge candidates found.")

if __name__ == "__main__":
    run_daemon()
