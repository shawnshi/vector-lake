#!/usr/bin/env python3
import json
import logging
import os
from collections import defaultdict

# Add parent dir to sys.path so we can import vector_lake if run directly
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from vector_lake.wiki_utils import get_index_path, get_wiki_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("compile_domain_overviews")

def compile_overviews():
    index_path = get_index_path()
    wiki_dir = get_wiki_dir()

    if not os.path.exists(index_path):
        log.warning(f"Index not found at {index_path}. Cannot compile overviews.")
        return

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except json.JSONDecodeError as e:
        log.error(f"Failed to read index.json: {e}")
        return

    nodes = index_data.get("nodes", {})
    domains = defaultdict(list)

    for node_key, node in nodes.items():
        domain_raw = node.get("domain")
        if domain_raw:
            score = node.get("node_score", node.get("decay_weight", 1.0))
            domain_list = domain_raw if isinstance(domain_raw, list) else [domain_raw]
            for d in domain_list:
                domains[str(d)].append((score, node_key, node))

    for domain, domain_nodes in domains.items():
        # Sort by score descending
        domain_nodes.sort(key=lambda x: x[0], reverse=True)
        top_nodes = domain_nodes[:50]  # Take top 50 for the overview

        safe_domain = "".join(c for c in domain if c.isalnum() or c in ("_", "-"))
        overview_filename = f"Overview_{safe_domain}.md"
        overview_path = os.path.join(wiki_dir, overview_filename)

        content = [
            f"# Domain Overview: {domain}",
            "",
            "*[System Directive: This is an automatically compiled read model of the domain based on node_score (Centrality * Freshness). Do not manually edit this file.]*",
            "",
            f"**Total Nodes in Domain:** {len(domain_nodes)}",
            "",
            "## Top Entities by Network Relevance",
            ""
        ]

        for score, key, node in top_nodes:
            title = node.get("title", key)
            summary = node.get("summary", "")
            type_str = node.get("type", "concept").upper()
            content.append(f"### [[{key}]]")
            content.append(f"- **Type**: {type_str}")
            content.append(f"- **Score**: {score:.2f}")
            content.append(f"- **Summary**: {summary}")
            content.append("")

        try:
            with open(overview_path, "w", encoding="utf-8") as f:
                f.write("\n".join(content))
            log.info(f"Compiled {overview_filename} with {len(top_nodes)} top nodes.")
        except Exception as e:
            log.error(f"Failed to write {overview_filename}: {e}")

if __name__ == "__main__":
    compile_overviews()
