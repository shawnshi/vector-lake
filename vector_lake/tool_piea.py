import json
import logging
import math
import re
from collections import Counter
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
    candidate_type = candidate_type.strip().lower()
    if candidate_type not in ("vendor", "product", "person", "event", "concept"):
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
        if node.get("type") != candidate_type:
            continue

        existing_title = node.get("title", "").strip('"').strip("'")
        existing_norm = _normalize_memory_key(existing_title)

        # 1. Hard Normalization Match
        if candidate_norm == existing_norm and candidate_norm != "general":
            return json.dumps({
                "is_duplicate": True,
                "existing_key": key,
                "existing_title": existing_title,
                "similarity": 1.0,
                "match_type": "hard_normalization",
                "instruction": f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead."
            })

        # 2. Fallback to Cosine Similarity
        existing_summary = node.get("summary") or existing_title
        sim = _calculate_cosine_similarity(candidate_summary, existing_summary)

        if sim >= threshold:
            return json.dumps({
                "is_duplicate": True,
                "existing_key": key,
                "existing_title": existing_title,
                "similarity": sim,
                "match_type": "cosine_similarity",
                "instruction": f"Do NOT create a new file. Append your content to the Timeline of {key}.md instead."
            })

    return json.dumps({"is_duplicate": False, "instruction": "Safe to create new entity."})
