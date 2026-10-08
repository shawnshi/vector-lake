import json
import os
import logging
from filelock import FileLock

from vector_lake import governance_store
from vector_lake.template_loader import render_template
from vector_lake.wiki_utils import get_index_path
from vector_lake.purpose_contract import PurposeContractError, render_strategy_directive

log = logging.getLogger("vector-lake-tool-research")

def research_vector_lake(dry_run: bool = False):
    index_path = str(get_index_path())
    lock_path = index_path + ".lock"
    insights = []
    
    if os.path.exists(index_path):
        try:
            with FileLock(lock_path, timeout=5):
                with open(index_path, "r", encoding="utf-8") as handle:
                    index_data = json.load(handle)
                insights = index_data.get("graph_insights", [])
        except Exception as e:
            return f"Error reading index.json: {e}"

    pending_items = governance_store.pending_governance_items()
    review_queries = []
    for item in pending_items:
        queries = item.get("search_queries", [])
        if queries:
            review_queries.extend(queries)

    # Extract gap queries from graph insights
    gap_queries = []
    for insight in insights:
        if insight.get("type") == "sparse_community":
            nodes = insight.get("nodes", [])[:3]
            gap_queries.append(f"Connection between {' and '.join(nodes)}")
        elif insight.get("type") == "isolated_node":
            gap_queries.append(insight.get("node"))
    
    # Deduplicate while preserving order, limit to 5 of each type to prevent overwhelming context
    all_queries = []
    seen = set()
    for q in (review_queries[:5] + gap_queries[:5]):
        if q not in seen:
            seen.add(q)
            all_queries.append(q)
    
    if not all_queries:
        return "[SYSTEM REPORT]: No research required. Knowledge graph is well-connected and governance queue has no pending queries."

    queries_str = "\n".join([f"- {q}" for q in all_queries])

    purpose_context = ""
    try:
        purpose_context = render_template("prompts/research_purpose.md", purpose=render_strategy_directive())
    except PurposeContractError as exc:
        return f"Strategic purpose contract is invalid: {exc}"

    if dry_run:
        directive = render_template("prompts/research_preview.md", purpose_context=purpose_context, queries=queries_str)
    else:
        directive = render_template("prompts/research.md", purpose_context=purpose_context, queries=queries_str)
    return directive
