import json
import logging
import os
import re
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from vector_lake import governance_store
from vector_lake import db_store
from vector_lake.wiki_utils import get_index_path, get_purpose_path, get_wiki_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-search")

TOKEN_BUDGET = {
    "operational_memory": 0.30,
    "wiki_pages": 0.45,
    "chat_history": 0.05,
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


def _classify_intent(query: str) -> str:
    temporal_keywords = {"上周", "去年", "昨天", "最近", "历史", "last week", "yesterday", "202"}
    entity_keywords = {"是谁", "哪里", "谁在", "who is", "where is", "公司", "人员", "关联", "图谱", "网络"}
    for kw in temporal_keywords:
        if kw in query.lower(): return "temporal"
    for kw in entity_keywords:
        if kw in query.lower(): return "entity"
    return "general"


def _expand_query_with_llm(query: str) -> list[str]:
    expanded_terms = set([query])
    for key, expansions in QUERY_EXPANSION_DICT.items():
        if key in query:
            expanded_terms.update(expansions)
            
    try:
        import subprocess, json
        from vector_lake import get_extension_root
        
        models_to_try = ["gemini-flash-latest"]
        try:
            cfg_path = get_extension_root() / "config.json"
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                cascade = cfg.get("llm", {}).get("model_cascade", [])
                for m in cascade:
                    if m and m not in models_to_try:
                        models_to_try.append(m)
        except Exception:
            pass
            
        prompt = f"Expand the following search query into 5 to 8 precise, distinct keywords or synonyms (including English/Chinese terms if relevant). Output ONLY a JSON array of strings. Query: '{query}'"
        gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
        
        for model in models_to_try:
            cmd_args = [gemini_exec]
            if model and model not in ("default", ""):
                cmd_args.extend(["-m", model])
            cmd_args.extend(["-p", "You are an expert search engine query expander.", "--approval-mode", "yolo"])

            try:
                result = subprocess.run(
                    cmd_args,
                    input=prompt.encode('utf-8'),
                    capture_output=True,
                    timeout=10
                )
                if result.returncode == 0:
                    stdout_str = result.stdout.decode('utf-8', errors='replace').strip()
                    match = re.search(r"\[.*?\]", stdout_str, re.DOTALL)
                    if match:
                        terms = json.loads(match.group(0))
                        expanded_terms.update([str(t) for t in terms])
                        break
            except Exception as e:
                log.warning(f"Model {model} failed via subprocess: {e}")
                
    except Exception as e:
        log.warning(f"LLM query expansion failed: {e}")

    tokens = set()
    try:
        import jieba
        for term in QUERY_EXPANSION_DICT.keys():
            jieba.add_word(term)
        for expansions in QUERY_EXPANSION_DICT.values():
            for exp in expansions:
                jieba.add_word(exp)
    except ImportError:
        jieba = None

    for term in expanded_terms:
        if jieba and CJK_REGEX.search(term):
            for word in jieba.lcut(term):
                word_lower = word.lower()
                if word_lower not in STOP_WORDS and word_lower.strip():
                    tokens.add(word_lower)
        else:
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


def _format_memory_result(memory: dict, as_xml: bool = False, index: int = 0) -> str:
    state = memory.get("validity_state", "active")
    memory_type = memory.get("memory_type", "fact")
    score = memory.get("retrieval_score", memory.get("memory_score", 0))
    text = " ".join(str(memory.get("text", "")).split())[:420]
    source = memory.get("source_page") or memory.get("source_claim_id") or "operational_memory"
    if as_xml:
        attrs = (
            f"ID='Memory_{index}' Type='{memory_type}' State='{state}' "
            f"Score='{score}' Source='{source}'"
        )
        return f"<Memory_Item {attrs}>{text}</Memory_Item>\n"
    return (
        f"- **{memory_type}:{memory.get('memory_key', memory.get('memory_id'))}** "
        f"(score: {score:.2f}, state: {state})\n"
        f"  {text}\n"
        f"  Source: {source}\n\n"
    )


def format_operational_memory_results(query: str, top_k: int = 8, as_xml: bool = False, include_history: bool = False, memory_types: list[str] | None = None) -> str:
    memories = governance_store.search_operational_memory(
        query,
        top_k=top_k,
        include_history=include_history,
        memory_types=memory_types,
    )
    if not memories:
        return "No operational memory matched the query."
    return "".join(_format_memory_result(memory, as_xml=as_xml, index=index) for index, memory in enumerate(memories))


def build_memory_packet(query: str, max_chars: int = 60000) -> dict:
    memories = governance_store.search_operational_memory(query, top_k=24, include_history=False)
    historical = governance_store.search_operational_memory(query, top_k=12, include_history=True)
    stale_or_conflicted = [
        item for item in historical
        if str(item.get("validity_state", "")).lower() in {"conflicted", "review-due", "needs-review", "superseded", "expired"}
    ][:6]

    sections = {
        "Current Preferences": [],
        "Open Decisions": [],
        "Task State": [],
        "Relevant Facts": [],
    }
    type_to_section = {
        "preference": "Current Preferences",
        "decision": "Open Decisions",
        "task_state": "Task State",
        "fact": "Relevant Facts",
    }

    evidence_pointers = []
    for memory in memories:
        section = type_to_section.get(memory.get("memory_type", "fact"), "Relevant Facts")
        text = " ".join(str(memory.get("text", "")).split())
        line = (
            f"- [{memory.get('memory_score', 0):.2f}/{memory.get('validity_state', 'active')}] "
            f"{text[:420]}"
        )
        if memory.get("source_page"):
            line += f" ({memory['source_page']})"
        sections[section].append(line)
        if memory.get("source_claim_id"):
            evidence_pointers.append(
                f"- {memory.get('source_claim_id')} -> {memory.get('source_page', 'unknown')}"
            )

    lines = [
        "<MEMORY_PACKET>",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Query: {query}",
        "Policy: Use this packet as the machine-facing runtime memory. If it conflicts with wiki prose, prefer active non-conflicted memory items and surface the conflict.",
        "",
    ]
    for title in ("Current Preferences", "Open Decisions", "Task State", "Relevant Facts"):
        lines.append(f"## {title}")
        lines.extend(sections[title] or ["- None matched."])
        lines.append("")

    lines.append("## Conflicts / Stale Warnings")
    if stale_or_conflicted:
        for memory in stale_or_conflicted:
            lines.append(
                f"- [{memory.get('validity_state')}] {memory.get('memory_type')}:{memory.get('memory_key')} "
                f"-> {str(memory.get('text', ''))[:260]}"
            )
    else:
        lines.append("- None matched.")
    lines.append("")

    lines.append("## Evidence Pointers")
    lines.extend(evidence_pointers[:12] or ["- None matched."])
    lines.append("</MEMORY_PACKET>")

    packet = "\n".join(lines)
    omitted = 0
    if len(packet) > max_chars:
        packet = packet[: max(0, max_chars - 80)].rstrip() + "\n...[memory packet truncated]\n</MEMORY_PACKET>"
        omitted = max(0, len(memories) - 12)
    return {
        "packet": packet,
        "memory_count": len(memories),
        "warning_count": len(stale_or_conflicted),
        "omitted_count": omitted,
    }


def _rerank_candidates_with_llm(query: str, candidates: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    if not candidates or len(candidates) <= 3:
        return candidates
        
    wiki_dir = str(get_wiki_dir())
    candidate_prompts = []
    
    for idx, (score, node) in enumerate(candidates):
        filepath = os.path.join(wiki_dir, f"{node['_key']}.md")
        snippet = ""
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
                snippet = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)[:150].strip()
            except Exception:
                pass
        title = node.get("title", node["_key"])
        candidate_prompts.append(f"[{idx}] {title}: {snippet}")
        
    prompt = (
        f"You are a relevance ranker. Score each of the following candidate documents from 0 to 10 "
        f"based on its relevance to the query: '{query}'.\n"
        f"Output ONLY a JSON dict where keys are the string IDs (e.g., '0', '1') and values are the integer scores.\n\n"
        + "\n".join(candidate_prompts)
    )
    
    try:
        import subprocess, json
        from vector_lake import get_extension_root
        gemini_exec = "gemini.cmd" if os.name == "nt" else "gemini"
        
        models_to_try = ["gemini-flash-latest"]
        try:
            cfg_path = get_extension_root() / "config.json"
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                cascade = cfg.get("llm", {}).get("model_cascade", [])
                for m in cascade:
                    if m and m not in models_to_try:
                        models_to_try.append(m)
        except Exception:
            pass
            
        for model in models_to_try:
            cmd_args = [gemini_exec]
            if model and model not in ("default", ""):
                cmd_args.extend(["-m", model])
            cmd_args.extend(["-p", "You are an expert search engine reranker.", "--approval-mode", "yolo"])

            try:
                result = subprocess.run(
                    cmd_args,
                    input=prompt.encode('utf-8'),
                    capture_output=True,
                    timeout=15
                )
                if result.returncode == 0:
                    stdout_str = result.stdout.decode('utf-8', errors='replace').strip()
                    match = re.search(r"\{.*?\}", stdout_str, re.DOTALL)
                    if match:
                        scores_dict = json.loads(match.group(0))
                        new_scored = []
                        for idx, (score, node) in enumerate(candidates):
                            llm_score = float(scores_dict.get(str(idx), scores_dict.get(idx, 0)))
                            new_score = score * 0.1 + llm_score * 10
                            new_scored.append((new_score, node))
                        new_scored.sort(key=lambda item: item[0], reverse=True)
                        return new_scored
            except Exception as e:
                log.warning(f"Model {model} failed via subprocess: {e}")
                
    except Exception as e:
        log.warning(f"LLM Reranking failed: {e}")
        
    return candidates


def search_vector_lake(query: str, top_k: int = 5, as_xml: bool = False, domain: str = None, cluster: str = None, include_history: bool = False, mode: str = "page"):
    normalized_mode = str(mode or "page").lower()
    if normalized_mode in {"memory", "operational-memory", "operational_memory"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history)
    if normalized_mode in {"claim", "claims"}:
        return format_operational_memory_results(query, top_k=top_k, as_xml=as_xml, include_history=include_history, memory_types=["fact"])

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
    except Exception as e:
        log.error(f"Failed to read index.json: {e}")
        return "Error reading the knowledge base index. Please ensure the index exists and is not corrupted."

    nodes = [{"_key": key, **value} for key, value in index_data.get("nodes", {}).items()]
    intent = _classify_intent(query)
    tokens = _expand_query_with_llm(query)
    if not tokens:
        return "No valid search tokens."

    scored = []
    
    # PHASE 2 FTS5 QUERY
    try:
        fts_results = db_store.search_wiki(' '.join(tokens), limit=top_k * 5)
        for row in fts_results:
            key = row['node_key']
            score = row['rank'] * -1.0  # SQLite BM25 is negative
            if key in index_data.get('nodes', {}):
                node = {'_key': key, **index_data['nodes'][key]}
                if domain and node.get('domain', '').lower() != domain.lower(): continue
                if cluster and node.get('topic_cluster', '').lower() != cluster.lower(): continue
                if not include_history and node.get('status', '').lower() in ('deprecated', 'archived'): continue
                
                if not include_history and node.get('status', '').lower() == 'decayed' and intent != 'temporal':
                    score *= 0.2
                scored.append((score, node))
    except Exception as e:
        log.error(f"FTS5 Search failed: {e}")

    scored.sort(key=lambda item: item[0], reverse=True)

    # P2-1: Dynamic Graph Expansion via Multi-hop PPR (Personalized PageRank)
    top_keys = {node["_key"] for _, node in scored[:5]}
    if top_keys and index_data.get("weighted_edges"):
        # Build adjacency list
        adj = {}
        for edge in index_data["weighted_edges"]:
            s, t, w = edge["source"], edge["target"], edge.get("weight", 1.0)
            adj.setdefault(s, []).append((t, w))
            adj.setdefault(t, []).append((s, w))
            
        # PPR parameters
        ppr_scores = {k: 1.0 for k in top_keys}
        alpha = 0.85
        
        # 2-Hop Random Walk
        for _ in range(2):
            next_scores = {k: (1 - alpha) * 1.0 if k in top_keys else 0.0 for k in ppr_scores.keys()}
            for node, current_score in ppr_scores.items():
                neighbors = adj.get(node, [])
                if neighbors:
                    total_weight = sum(w for _, w in neighbors)
                    for neighbor, w in neighbors:
                        next_scores[neighbor] = next_scores.get(neighbor, 0.0) + alpha * current_score * (w / total_weight)
            ppr_scores = next_scores

        existing_keys = {node["_key"] for _, node in scored}
        expansion_limit = 12 if intent == "entity" else 5
        
        sorted_expansions = sorted(
            [(k, v) for k, v in ppr_scores.items() if k not in existing_keys], 
            key=lambda x: x[1], 
            reverse=True
        )
        
        for expanded_key, ppr_weight in sorted_expansions[:expansion_limit]:
            expanded_node = index_data["nodes"].get(expanded_key)
            if expanded_node:
                # Scale PPR weight to match BM25 range approximately
                scored.append((ppr_weight * 15.0, {"_key": expanded_key, **expanded_node}))

    scored.sort(key=lambda item: item[0], reverse=True)

    # Phase 1: Expand candidate pool for reranking
    candidate_pool = []
    source_count = 0
    pool_size = max(40, top_k * 3)
    max_sources_pool = int(pool_size * 0.6)
    for score, node in scored:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources_pool:
                candidate_pool.append((score, node))
                source_count += 1
        else:
            candidate_pool.append((score, node))
        if len(candidate_pool) >= pool_size:
            break
            
    # Phase 2: Lightweight LLM-as-a-Judge Reranking
    reranked = _rerank_candidates_with_llm(query, candidate_pool)

    # Phase 3: Final top_k extraction
    final_scored = []
    source_count = 0
    max_sources_final = int(top_k * 0.6)
    for score, node in reranked:
        node_type = node.get("type", "").lower()
        if node_type == "source":
            if source_count < max_sources_final:
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
            
        tension_edges = node.get("tension_edges", [])
        tension_info = ""
        if tension_edges:
            tension_info = "  [Tension Edges]:\n"
            for te in tension_edges:
                tension_info += f"    -> {te.get('target')} (Polarity: {te.get('polarity')}, Intensity: {te.get('intensity')}): {te.get('context')}\n"
                
        if as_xml:
            result += f"<Evidence_Node ID='Wiki_{index}' Source='{node['_key']}.md'>\n{tension_info}{snippet}\n</Evidence_Node>\n"
        else:
            result += f"- **{node.get('title', node['_key'])}** (score: {score:.1f})\n{tension_info}  {snippet}...\n\n"
    return result


def assemble_context(query: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    base_memory_budget = int(max_chars * TOKEN_BUDGET["operational_memory"])
    index_budget = int(max_chars * TOKEN_BUDGET["index_summary"])
    
    # P2-2: Dynamic Sliding Window for Budget
    # Allow memory to burst up to 50% if there are critical alerts
    memory_packet = build_memory_packet(query, max_chars=int(max_chars * 0.50))
    actual_memory_used = len(memory_packet["packet"])
    
    # Wiki dynamically eats the remaining budget
    wiki_budget = max_chars - actual_memory_used - index_budget

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
        "memory_packet": memory_packet["packet"],
        "memory_count": memory_packet["memory_count"],
        "memory_warning_count": memory_packet["warning_count"],
        "memory_omitted_count": memory_packet["omitted_count"],
        "wiki_context": wiki_context,
        "wiki_page_count": page_count,
        "index_summary": index_summary,
        "purpose": purpose,
        "budget_used": len(memory_packet["packet"]) + len(wiki_context) + len(index_summary) + len(purpose),
        "budget_max": max_chars,
    }

