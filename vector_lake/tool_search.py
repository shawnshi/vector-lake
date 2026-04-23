import json
import logging
import os
import re

from filelock import FileLock, Timeout

from vector_lake.wiki_utils import get_index_path, get_purpose_path, get_wiki_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-search")

TOKEN_BUDGET = {
    "wiki_pages": 0.60,
    "chat_history": 0.20,
    "index_summary": 0.05,
    "system_prompt": 0.15,
}
DEFAULT_MAX_CHARS = 200000

CJK_REGEX = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "with", "by", "from", "as", "it", "this", "that",
}

QUERY_EXPANSION_DICT = {
    "医疗信息化": ["HIT", "卫宁", "电子病历", "医疗IT"],
    "大模型": ["LLM", "大语言模型", "Agent", "智能体"],
    "医疗AI": ["临床Agent", "大模型医疗落地", "电子病历 智能化"],
}


def _tokenize_query(query: str) -> list:
    tokens = set()
    expanded_terms = {query}
    for key, expansions in QUERY_EXPANSION_DICT.items():
        if key in query:
            expanded_terms.update(expansions)

    for term in expanded_terms:
        for word in term.strip().split():
            word_lower = word.lower()
            if word_lower in STOP_WORDS:
                continue
            if CJK_REGEX.search(word):
                chars = list(word)
                for index in range(len(chars) - 1):
                    tokens.add(chars[index] + chars[index + 1])
                for char in chars:
                    if CJK_REGEX.match(char):
                        tokens.add(char)
                tokens.add(word)
            else:
                tokens.add(word_lower)
    return list(tokens)


def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str = None, cluster: str = None, include_history: bool = False):
    wiki_dir = str(get_wiki_dir())
    index_path = str(get_index_path())
    if not os.path.exists(index_path):
        return "Lake is drying. No index.json found, please ingest sources first."
    lock_path = index_path + ".lock"

    try:
        with FileLock(lock_path, timeout=5):
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
    except Timeout:
        log.warning("Timeout acquiring lock for index.json during search. System is busy.")
        return "System is currently busy syncing the knowledge base. Please try again in a few seconds."
    except Exception:
        from vector_lake import indexer

        indexer.generate_index()
        try:
            with FileLock(lock_path, timeout=5):
                with open(index_path, "r", encoding="utf-8") as handle:
                    index_data = json.load(handle)
        except Timeout:
            return "System is currently busy generating the index. Please try again later."

    nodes = [{"_key": key, **value} for key, value in index_data.get("nodes", {}).items()]
    tokens = _tokenize_query(query)
    if not tokens:
        return "No valid search tokens."

    scored = []
    for node in nodes:
        if domain and node.get("domain", "").lower() != domain.lower():
            continue
        if cluster and node.get("topic_cluster", "").lower() != cluster.lower():
            continue
        if not include_history and node.get("status", "").lower() in ("deprecated", "archived"):
            continue

        score = 0
        title = (node.get("title") or "").lower()
        summary = (node.get("summary") or "").lower()

        for term in tokens:
            if term in title:
                score += 10
            if term in summary:
                score += 3

        if score == 0:
            filepath = os.path.join(wiki_dir, f"{node['_key']}.md")
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                        body_preview = handle.read(2000).lower()
                    body_preview = re.sub(r"^---.*?---\s*", "", body_preview, flags=re.DOTALL)
                    for term in tokens:
                        if term in body_preview:
                            score += 1
                except Exception:
                    pass

        if score > 0:
            scored.append((score, node))

    scored.sort(key=lambda item: item[0], reverse=True)

    top_keys = {node["_key"] for _, node in scored[:3]}
    if top_keys and index_data.get("weighted_edges"):
        expansion_candidates = {}
        for edge in index_data["weighted_edges"]:
            source = edge["source"]
            target = edge["target"]
            weight = edge["weight"]
            if source in top_keys and target not in top_keys:
                expansion_candidates[target] = max(expansion_candidates.get(target, 0), weight)
            elif target in top_keys and source not in top_keys:
                expansion_candidates[source] = max(expansion_candidates.get(source, 0), weight)

        existing_keys = {node["_key"] for _, node in scored}
        for expanded_key, expanded_weight in sorted(expansion_candidates.items(), key=lambda item: item[1], reverse=True)[:3]:
            if expanded_key not in existing_keys:
                expanded_node = index_data["nodes"].get(expanded_key)
                if expanded_node:
                    scored.append((expanded_weight, {"_key": expanded_key, **expanded_node}))

    scored.sort(key=lambda item: item[0], reverse=True)

    final_scored = []
    source_count = 0
    max_sources = int(top_k * 0.6)
    for score, node in scored:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources:
                final_scored.append((score, node))
                source_count += 1
        else:
            final_scored.append((score, node))
        if len(final_scored) >= top_k:
            break

    result = ""
    for index, (score, node) in enumerate(final_scored):
        filepath = os.path.join(wiki_dir, f"{node['_key']}.md")
        snippet = ""
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            snippet = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)[:300]
        if as_xml:
            result += f"<Evidence_Node ID='Wiki_{index}' Source='{node['_key']}.md'>{snippet}</Evidence_Node>\n"
        else:
            result += f"- **{node.get('title', node['_key'])}** (score: {score:.1f})\n  {snippet}...\n\n"
    return result


def assemble_context(query: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    wiki_budget = int(max_chars * TOKEN_BUDGET["wiki_pages"])
    index_budget = int(max_chars * TOKEN_BUDGET["index_summary"])

    search_results = search_vector_lake(query, top_k=15, as_xml=False)
    wiki_context = ""
    page_count = 0
    for match in re.finditer(r"\*\*(.+?)\*\*.*?\n\s+(.*?)\.\.\.\n", search_results, re.DOTALL):
        page_content = match.group(0)
        if len(wiki_context) + len(page_content) > wiki_budget:
            break
        wiki_context += page_content
        page_count += 1

    index_summary = ""
    index_path = str(get_index_path())
    lock_path = index_path + ".lock"
    if os.path.exists(index_path):
        try:
            with FileLock(lock_path, timeout=5):
                with open(index_path, "r", encoding="utf-8") as handle:
                    index_data = json.load(handle)
            lines = []
            for key, node in list(index_data.get("nodes", {}).items())[:50]:
                lines.append(f"[{node.get('type', '?')}] {node.get('title', key)}")
            index_summary = "\n".join(lines)[:index_budget]
        except Timeout:
            log.warning("Timeout acquiring lock for index.json during context assembly.")
            index_summary = "[Index currently locked for update]"
        except Exception:
            pass

    purpose = ""
    try:
        with open(get_purpose_path(), "r", encoding="utf-8") as handle:
            purpose = handle.read()
    except Exception:
        pass

    return {
        "wiki_context": wiki_context,
        "wiki_page_count": page_count,
        "index_summary": index_summary,
        "purpose": purpose,
        "budget_used": len(wiki_context) + len(index_summary) + len(purpose),
        "budget_max": max_chars,
    }

