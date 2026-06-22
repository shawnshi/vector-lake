import os
import json
import hashlib
import logging
import math
import uuid
import time
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
import sys

# Add parent dir to sys.path so we can import vector_lake if run directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from vector_lake.wiki_utils import get_meta_dir, get_index_path
from vector_lake import governance_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("semantic-dedup-daemon")

EMBEDDING_MODEL = "gemini-embedding-2"
SIMILARITY_THRESHOLD = 0.92
ADVANCED_THRESHOLD = 0.94

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def calculate_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(a * a for a in v2))
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0

def llm_semantic_arbiter(client, left_name: str, left_summary: str, right_name: str, right_summary: str) -> bool:
    if not client:
        return True
    prompt = f"""You are a strict Medical Knowledge Graph Ontology Arbiter.
Analyze the following two entities:
Entity 1: [{left_name}] - {left_summary}
Entity 2: [{right_name}] - {right_summary}

Determine if these two entities are SEMANTICALLY EQUIVALENT (meaning they represent the exact same concept, e.g., one is an abbreviation of the other, or they are exact synonyms).
If they are merely related (e.g., cause/effect, platform/paradigm, whole/part, competing frameworks), they are NOT equivalent.

Answer with exactly one word: YES if they are the exact same concept and should be merged. NO if they are distinct concepts.
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt
        )
        return "YES" in response.text.upper()
    except Exception as e:
        log.error(f"LLM Arbiter failed: {e}")
        return True

def _get_cache_path():
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

def strip_name(name: str) -> str:
    name = name.replace('.md', '')
    for prefix in ['Concept_', 'Vendor_', 'Person_', 'Product_', 'Event_', 'Synthesis_', 'Source_']:
        if name.startswith(prefix):
            name = name[len(prefix):]
    for w in ['系统', '架构', '模型', '法则', '理论', '平台', 'System', 'Model', 'Theory', 'Platform', '与', '的']:
        name = name.replace(w, '')
    name = re.sub(r'[\s_\-\(\)]+', '', name)
    return name.lower()

def run_daemon():
    # 1. Check API Key
    has_api_key = bool(os.environ.get("GEMINI_API_KEY"))
    if not has_api_key:
        log.warning("GEMINI_API_KEY not set. Semantic dedup daemon will run in Local-Only mode (Stemming + Topology).")

    client = None
    if has_api_key:
        try:
            from google import genai
            client = genai.Client()
        except ImportError:
            log.warning("google-genai not installed. Falling back to Local-Only mode.")
            has_api_key = False

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

    # Pre-process Stemming, Aliases, and Backlinks
    for key, node in entities.items():
        node['_stripped'] = strip_name(node.get('title', key))
        aliases = node.get('aliases', [])
        if isinstance(aliases, str): aliases = [aliases]
        node['_stripped_aliases'] = [strip_name(a) for a in aliases if a]
        node['backlinks'] = []

    # Build Backlinks from all nodes in index
    for key, node in index_data.get("nodes", {}).items():
        for link in node.get("links", []):
            if link in entities:
                entities[link]["backlinks"].append(key)

    cache = load_cache()
    cached_embeddings = cache.setdefault("embeddings", {})
    updates_made = False
    
    if has_api_key and client:
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

    entity_keys = list(entities.keys())
    entity_keys.sort(key=lambda k: entities[k]['_stripped'])
    
    candidates = []
    
    queue = governance_store.load_governance_queue()
    existing_pairs = {
        item.get("pair_key")
        for item in queue["items"]
        if item.get("type") == "merge"
    }

    log.info("Computing Multi-Layer Pairwise Similarities...")
    window_size = min(150, len(entity_keys))
    
    for i in range(len(entity_keys)):
        left_key = entity_keys[i]
        left_node = entities[left_key]
        left_strip = left_node['_stripped']
        left_title = left_node.get('title', left_key)
        
        # 1. Advanced Local Matching (Stemming & Topology)
        for j in range(i + 1, min(i + window_size, len(entity_keys))):
            right_key = entity_keys[j]
            right_node = entities[right_key]
            right_strip = right_node['_stripped']
            right_title = right_node.get('title', right_key)
            
            pair_key = "::".join(sorted([left_key, right_key]))
            if pair_key in existing_pairs:
                continue
                
            # DO NOT merge raw Sources (they are immutable ingested files, often false positives due to weekly reports)
            if left_key.startswith('Source_') and right_key.startswith('Source_'):
                continue
                
            # Layer A: Stemming / Metadata Collision
            collision = False
            if len(left_strip) > 2 and left_strip == right_strip:
                collision = True
            elif left_strip in right_node['_stripped_aliases'] or right_strip in left_node['_stripped_aliases']:
                collision = True
                
            # Layer B: Topology Jaccard
            b1 = set(left_node.get('backlinks', []))
            b2 = set(right_node.get('backlinks', []))
            jaccard = 0.0
            if b1 or b2:
                inter = len(b1.intersection(b2))
                union = len(b1.union(b2))
                jaccard = inter / union
                
            # Layer C: String Sequence
            str_score = SequenceMatcher(None, left_strip, right_strip).ratio()
            
            final_score = str_score
            reasons = []
            
            if collision:
                final_score = 1.0
                reasons.append("metadata-alias-collision")
            else:
                if jaccard > 0.6:
                    final_score = min(final_score + 0.15, 0.99)
                    reasons.append(f"topology-jaccard:{round(jaccard, 2)}")
            
            # Layer D: Semantic Vector Match (if available)
            if has_api_key and left_key in cached_embeddings and right_key in cached_embeddings:
                sim = calculate_cosine_similarity(cached_embeddings[left_key]["vector"], cached_embeddings[right_key]["vector"])
                if sim >= SIMILARITY_THRESHOLD:
                    if sim > final_score:
                        final_score = sim
                    reasons.append(f"semantic-embedding-match:{round(sim, 3)}")
            
            if final_score >= ADVANCED_THRESHOLD:
                if not reasons:
                    reasons.append(f"lexical-similarity:{round(str_score, 3)}")
                
                if client:
                    is_equiv = llm_semantic_arbiter(client, left_title, left_node.get("summary", ""), right_title, right_node.get("summary", ""))
                    if not is_equiv:
                        log.info(f"LLM Arbiter Rejected (False Alarm): {left_title} <> {right_title}")
                        existing_pairs.add(pair_key)
                        continue
                    reasons.append("llm-arbiter-approved")

                candidates.append({
                    "pair_key": pair_key,
                    "score": round(final_score, 3),
                    "left_entity_id": left_key,
                    "left_name": left_title,
                    "right_entity_id": right_key,
                    "right_name": right_title,
                    "reasons": reasons
                })
                existing_pairs.add(pair_key)

        # 2. Semantic Embedding Global Check
        if has_api_key:
            left_data = cached_embeddings.get(left_key)
            if not left_data or "vector" not in left_data:
                continue
            
            for j in range(i + 1, len(entity_keys)):
                # Skip if within window
                if j < i + window_size:
                    continue
                
                right_key = entity_keys[j]
                right_data = cached_embeddings.get(right_key)
                if not right_data or "vector" not in right_data:
                    continue
                    
                pair_key = "::".join(sorted([left_key, right_key]))
                if pair_key in existing_pairs:
                    continue
                    
                sim = calculate_cosine_similarity(left_data["vector"], right_data["vector"])
                if sim >= SIMILARITY_THRESHOLD:
                    right_title = right_data["title"]
                    if client:
                        l_sum = index_data.get("nodes", {}).get(left_key, {}).get("summary", "")
                        r_sum = index_data.get("nodes", {}).get(right_key, {}).get("summary", "")
                        is_equiv = llm_semantic_arbiter(client, left_title, l_sum, right_title, r_sum)
                        if not is_equiv:
                            log.info(f"LLM Arbiter Rejected (False Alarm): {left_title} <> {right_title}")
                            existing_pairs.add(pair_key)
                            continue
                            
                    candidates.append({
                        "pair_key": pair_key,
                        "score": round(sim, 3),
                        "left_entity_id": left_key,
                        "left_name": left_title,
                        "right_entity_id": right_key,
                        "right_name": right_title,
                        "reasons": [f"semantic-embedding-match:{round(sim, 3)}", "llm-arbiter-approved"] if client else [f"semantic-embedding-match:{round(sim, 3)}"]
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
                "source": "semantic-dedup-daemon",
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

if __name__ == "__main__":
    run_daemon()
