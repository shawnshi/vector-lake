#!/usr/bin/env python3
import json
import logging
import os
import datetime
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
    cross_domain_counts = defaultdict(lambda: defaultdict(int))

    # Phase 1: Group nodes into domains and track cross-domain links
    for node_key, node in nodes.items():
        domain_raw = node.get("domain")
        if not domain_raw:
            continue
        score = node.get("node_score", node.get("decay_weight", 1.0))
        try:
            score = float(score) if score is not None else 0.0
        except ValueError:
            score = 0.0
            
        domain_list = domain_raw if isinstance(domain_raw, list) else [domain_raw]
        safe_domains = []
        for d in domain_list:
            sd = "".join(c for c in str(d) if c.isalnum() or c in ("_", "-"))
            if not sd:
                sd = "Uncategorized"
            domains[sd].append((score, node_key, node))
            safe_domains.append(sd)

        # Count cross-domain edges
        links = node.get("links", [])
        for link in links:
            target_node = nodes.get(link)
            if target_node:
                t_domain_raw = target_node.get("domain")
                if not t_domain_raw:
                    continue
                t_domain_list = t_domain_raw if isinstance(t_domain_raw, list) else [t_domain_raw]
                for sd in safe_domains:
                    for td in t_domain_list:
                        safe_td = "".join(c for c in str(td) if c.isalnum() or c in ("_", "-"))
                        if not safe_td:
                            safe_td = "Uncategorized"
                        if sd != safe_td:
                            cross_domain_counts[sd][safe_td] += 1

    today = datetime.date.today()

    for domain, domain_nodes in domains.items():
        # Double sort key: score desc, updated desc
        domain_nodes.sort(key=lambda x: (x[0], x[2].get('updated', '0')), reverse=True)
        
        top_nodes = domain_nodes[:50]
        tail_nodes = domain_nodes[50:]

        overview_filename = f"Concept_Overview_{domain}.md"
        overview_path = os.path.join(wiki_dir, overview_filename)

        content = [
            f"# Domain Overview: {domain}",
            "",
            "*[System Directive: This is an automatically compiled read model of the domain based on node_score (Centrality * Freshness). Do not manually edit this file.]*",
        ]

        # Inject Cross-Domain Gravity Anchors
        if domain in cross_domain_counts and cross_domain_counts[domain]:
            top_related = sorted(cross_domain_counts[domain].items(), key=lambda x: x[1], reverse=True)[:3]
            links_str = ", ".join([f"[[Concept_Overview_{rd[0]}]] ({rd[1]}次跨域握手)" for rd in top_related])
            content.append(f"> 🔄 **强关联领域**: {links_str}")
            content.append("")

        content.append(f"**Total Nodes in Domain:** {len(domain_nodes)}")
        content.append("")
        
        # Inject Rising Stars (updated within 7 days)
        recent_nodes = []
        for score, key, node in domain_nodes:
            updated_str = node.get('updated', '')
            try:
                up_date = datetime.datetime.strptime(updated_str, "%Y-%m-%d").date()
                if (today - up_date).days <= 7:
                    recent_nodes.append((score, key, node))
            except ValueError:
                pass
                
        if recent_nodes:
            recent_nodes.sort(key=lambda x: x[0], reverse=True)
            rising_stars = recent_nodes[:5]
            content.append("## 🚀 异动榜 (Rising Stars)")
            for score, key, node in rising_stars:
                summary = node.get("summary", "")
                content.append(f"- [[{key}]] (Score: {score:.2f}) - {summary}")
            content.append("")

        content.append("## 📌 核心实体排行 (Top Entities by Network Relevance)")
        content.append("")

        # Group by type for Top 50
        grouped_top = defaultdict(list)
        for score, key, node in top_nodes:
            node_type = node.get('type', 'Concept')
            if not isinstance(node_type, str) or not node_type.strip():
                node_type = 'Concept'
            node_type = node_type.strip().capitalize()
            grouped_top[node_type].append((score, key, node))
            
        # Render top 50
        for node_type, nodes_in_type in grouped_top.items():
            content.append(f"### 🧩 类别: {node_type}")
            for score, key, node in nodes_in_type:
                summary = node.get("summary", "")
                content.append(f"#### [[{key}]]")
                content.append(f"- **Score**: {score:.2f}")
                if summary:
                    content.append(f"- **Summary**: {summary}")
                content.append("")

        # Render tail nodes in a collapsible block to prevent orphan islands
        if tail_nodes:
            content.append(f"## 📦 领域归档字典 ({len(tail_nodes)} Nodes)")
            content.append("<details><summary>点击展开查看长尾归档实体</summary>\n")
            for score, key, node in tail_nodes:
                content.append(f"- [[{key}]] (Score: {score:.2f})")
            content.append("\n</details>")

        try:
            from vector_lake.wiki_utils import safe_write_markdown
            safe_write_markdown(overview_path, "\n".join(content))
            log.info(f"Compiled {overview_filename} with {len(top_nodes)} top nodes and {len(tail_nodes)} tail nodes.")
        except Exception as e:
            log.error(f"Failed to write {overview_filename}: {e}")

if __name__ == "__main__":
    compile_overviews()
