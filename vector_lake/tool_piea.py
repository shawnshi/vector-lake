import json
import logging
import math
import re
from collections import Counter
import os
from filelock import FileLock
from vector_lake import get_extension_root
from vector_lake.wiki_utils import get_index_path

log = logging.getLogger("vector-lake-piea")

def _calculate_cosine_similarity(text1: str, text2: str) -> float:
    def get_tokens(text):
        tokens = Counter()
        text = text.lower()
        cjk_chars = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text)
        for char in cjk_chars:
            tokens[char] += 1
        for i in range(len(cjk_chars) - 1):
            tokens[cjk_chars[i] + cjk_chars[i+1]] += 1
        latin_words = re.findall(r"[a-z0-9]+", text)
        for word in latin_words:
            tokens[word] += 2
        return tokens

    vec1 = get_tokens(text1)
    vec2 = get_tokens(text2)
    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum([vec1[x] * vec2[x] for x in intersection])
    sum1 = sum([vec1[x] ** 2 for x in vec1.keys()])
    sum2 = sum([vec2[x] ** 2 for x in vec2.keys()])
    denominator = math.sqrt(sum1) * math.sqrt(sum2)
    if not denominator:
        return 0.0
    return float(numerator) / denominator


def _normalize_memory_key(value: str) -> str:
    """Normalize a string by converting to lowercase and replacing non-alphanumeric/CJK chars with underscores."""
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:96] or "general"


def check_duplicate_entity(candidate_title: str, candidate_type: str, candidate_summary: str = "") -> str:
    """PIEA Hook: Check if an entity or concept already exists in the graph using hard normalization and cosine similarity.
    
    Args:
        candidate_title: The title of the entity to create.
        candidate_type: The type of the entity (e.g. 'entity', 'concept').
        candidate_summary: A brief summary of the entity to use for similarity matching.
    """
    # 1. Clean nested prefixes from candidate_title (e.g., "Concept_Person_XYZ" -> "XYZ")
    candidate_title = re.sub(r'^(Concept|Vendor|Product|Person|Event|Source|Synthesis)[_-]+', '', candidate_title, flags=re.IGNORECASE).strip()
    
    # 2. Normalize candidate_type (extract core type if nested, e.g., "concept_synthesis" -> "synthesis")
    candidate_type = candidate_type.strip().lower()
    if "synthesis" in candidate_type: candidate_type = "synthesis"
    elif "person" in candidate_type: candidate_type = "person"
    elif "event" in candidate_type: candidate_type = "event"
    elif "vendor" in candidate_type: candidate_type = "vendor"
    elif "product" in candidate_type: candidate_type = "product"
    elif "concept" in candidate_type: candidate_type = "concept"
    
    if candidate_type not in ("vendor", "product", "person", "event", "concept", "synthesis"):
        return json.dumps({"is_duplicate": False, "reason": f"Type '{candidate_type}' does not require deduplication check."})

    index_path = get_index_path()
    if not index_path.exists():
        return json.dumps({"is_duplicate": False, "reason": "No index exists yet."})

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except Exception as e:
        log.warning(f"Could not load index for PIEA check: {e}")
        return json.dumps({"is_duplicate": False, "reason": "Could not read index."})

    CONFIG_PATH = get_extension_root() / "config.json"
    threshold = 0.92
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
            threshold = config.get("piea", {}).get("threshold", 0.92)
    except Exception:
        pass

    nodes = index_data.get("nodes", {})
    candidate_norm = _normalize_memory_key(candidate_title)
    if not candidate_summary:
        candidate_summary = candidate_title

    for key, node in nodes.items():
        existing_title = node.get("title", "").strip('"').strip("'")
        existing_norm = _normalize_memory_key(existing_title)
        existing_type = node.get("type", "unknown")

        # 1. Hard Normalization Match
        if candidate_norm == existing_norm and candidate_norm != "general":
            instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead."
            if existing_type != candidate_type:
                instruction += f" Note: This entity is already registered as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
            return json.dumps({
                "is_duplicate": True,
                "existing_key": key,
                "existing_title": existing_title,
                "similarity": 1.0,
                "match_type": "hard_normalization_cross_type" if existing_type != candidate_type else "hard_normalization",
                "instruction": instruction
            })

        # 2. Fallback to Cosine Similarity
        existing_summary = node.get("summary") or existing_title
        sim = _calculate_cosine_similarity(candidate_summary, existing_summary)

        if sim >= threshold:
            instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead."
            if existing_type != candidate_type:
                instruction += f" Note: This entity is already registered as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
            return json.dumps({
                "is_duplicate": True,
                "existing_key": key,
                "existing_title": existing_title,
                "similarity": sim,
                "match_type": "cosine_similarity_cross_type" if existing_type != candidate_type else "cosine_similarity",
                "instruction": instruction
            })

    # --- NEW CONCURRENCY LOGIC (Pending Entities Registry) ---
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pending_path = tmp_dir / "pending_entities.json"
    lock_path = tmp_dir / "pending_entities.lock"
    
    try:
        with FileLock(lock_path, timeout=15):
            pending_data = {}
            if pending_path.exists():
                try:
                    with open(pending_path, "r", encoding="utf-8") as f:
                        pending_data = json.load(f)
                except Exception:
                    pending_data = {}
            
            # Check against pending entities being created by other concurrent workers
            for key, node in pending_data.items():
                existing_title = node.get("title", "")
                existing_norm = _normalize_memory_key(existing_title)
                existing_type = node.get("type", "unknown")
                
                # 1. Hard Normalization Match (Pending)
                if candidate_norm == existing_norm and candidate_norm != "general":
                    instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead (currently being built by another worker)."
                    if existing_type != candidate_type:
                        instruction += f" Note: This entity is being created as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
                    return json.dumps({
                        "is_duplicate": True,
                        "existing_key": key,
                        "existing_title": existing_title,
                        "similarity": 1.0,
                        "match_type": "hard_normalization_pending",
                        "instruction": instruction
                    })
                
                # 2. Fallback to Cosine Similarity (Pending)
                existing_summary = node.get("summary") or existing_title
                sim = _calculate_cosine_similarity(candidate_summary, existing_summary)
                if sim >= threshold:
                    instruction = f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead (currently being built by another worker)."
                    if existing_type != candidate_type:
                        instruction += f" Note: This entity is being created as a '{existing_type}'. Do NOT create a '{candidate_type}' variant."
                    return json.dumps({
                        "is_duplicate": True,
                        "existing_key": key,
                        "existing_title": existing_title,
                        "similarity": sim,
                        "match_type": "cosine_similarity_pending",
                        "instruction": instruction
                    })

            # If not found in index OR pending, register it as pending for other workers
            safe_title = re.sub(r'[\\/*?:"<>|]', "", candidate_title).strip()
            type_capitalized = candidate_type.capitalize()
            new_key = f"{type_capitalized}_{safe_title.replace(' ', '-').replace('_', '-')}"
            
            pending_data[new_key] = {
                "title": candidate_title,
                "type": candidate_type,
                "summary": candidate_summary
            }
            
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump(pending_data, f, ensure_ascii=False)
                
            return json.dumps({
                "is_duplicate": False, 
                "instruction": f"Safe to create new entity. You MUST use the exact filename: {new_key}.md"
            })
                
    except Exception as e:
        log.error(f"Error accessing pending entities lock: {e}")

    # Fallback return if lock fails
    safe_title = re.sub(r'[\\/*?:"<>|]', "", candidate_title).strip()
    type_capitalized = candidate_type.capitalize()
    new_key = f"{type_capitalized}_{safe_title.replace(' ', '-').replace('_', '-')}"
    return json.dumps({
        "is_duplicate": False, 
        "instruction": f"Safe to create new entity. You MUST use the exact filename: {new_key}.md"
    })

