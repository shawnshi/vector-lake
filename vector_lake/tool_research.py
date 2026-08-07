import os
import logging

from vector_lake import governance_store
from vector_lake.indexer import read_committed_index_snapshot
from vector_lake.wiki_utils import get_index_path
from vector_lake.purpose_contract import PurposeContractError, render_strategy_directive

log = logging.getLogger("vector-lake-tool-research")

def research_vector_lake(dry_run: bool = False):
    index_path = str(get_index_path())
    insights = []
    
    if os.path.exists(index_path):
        try:
            index_data = read_committed_index_snapshot(index_path)
            insights = index_data.get("graph_insights", [])
        except Exception as e:
            return f"Error: committed projection is not ready; run sync. Detail: {e}"

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
        purpose_context = "\nStrategic purpose contract:\n" + render_strategy_directive()
    except PurposeContractError as exc:
        return f"Strategic purpose contract is invalid: {exc}"

    if dry_run:
        directive = f"""[DRY RUN]: The following research topics were identified:
{purpose_context}
{queries_str}

No autonomous search will be triggered.
"""
    else:
        directive = f"""[SYSTEM DIRECTIVE]: Autonomous Deep Research Triggered.
Agent: You must now execute the following steps to heal the knowledge graph:
{purpose_context}

1. Evaluate the following research topics, contradictions, and knowledge gaps:
{queries_str}

2. Use your web search tools (e.g., `google_web_search`, `search_web`, or academic skills) to investigate these topics.
3. Fetch the most authoritative sources (avoid SEO spam).
4. Use `write_file` to save the distilled clean Markdown content to new files in `MEMORY/raw/research/`. Use descriptive filenames like `MEMORY/raw/research/research_gap_xxx.md`.
5. Do NOT just answer the question in the console. You MUST write the files so the lake can sync them.
6. Once the files are written, run `python cli.py sync` to ingest the new knowledge and close the graph gaps.
"""
    return directive
