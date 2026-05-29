import datetime
import logging
import os
import re
from google import genai
from google.genai import types
from google.genai.errors import APIError

from vector_lake import governance_store
from vector_lake import indexer
from vector_lake import provenance
from vector_lake.tool_search import assemble_context
from vector_lake.wiki_utils import get_wiki_dir, sanitize_wiki_node, write_markdown_file, normalize_entity_name


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-query")


import time
import json
import hashlib
from vector_lake import get_extension_root

def prepare_query_context(query_str: str, dry_run: bool = False):
    wiki_dir = str(get_wiki_dir())
    
    context = assemble_context(query_str)
    context_block = ""
    if context.get("memory_packet"):
        context_block += (
            f"\n\n--- OPERATIONAL MEMORY PACKET "
            f"({context.get('memory_count', 0)} items, {context.get('memory_warning_count', 0)} warnings) ---\n"
            f"{context['memory_packet']}"
        )
    if context["wiki_context"]:
        context_block += (
            f"\n\n--- RELEVANT WIKI PAGES ({context['wiki_page_count']} pages, "
            f"{context['budget_used']}/{context['budget_max']} chars) ---\n{context['wiki_context']}"
        )
    if context["purpose"]:
        context_block += f"\n\n--- PURPOSE ---\n{context['purpose']}"

    # Write context to a temporary payload file
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a unique hash for this query
    query_hash = hashlib.md5(query_str.encode("utf-8")).hexdigest()[:8]
    payload_path = tmp_dir / f"query_context_{query_hash}.md"
    
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(context_block)
        
    if dry_run:
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return f"[DRY RUN] Context assembled at {payload_path}\n\nTrace:\n{trace}"

    instructions = f"""[SUBAGENT DELEGATION REQUIRED]
Context successfully assembled and saved to: {payload_path}

Please execute the following workflow:
1. Invoke the subagent `vector-lake-synthesizer` with the exact prompt below.
2. Wait for the subagent to finish writing the file(s) (it must use its write_file tool).
3. Once the subagent finishes, find out which Synthesis_*.md files were created or modified.
4. Call the MCP tool `finalize_query_synthesis` with those filenames (comma-separated, e.g., 'Synthesis_A.md,Synthesis_B.md') and the original query string.

--- SUBAGENT PROMPT ---
Query: {query_str}

Instructions:
Read the context from {payload_path}. 
Perform bounded logical synthesis and generate the resulting Markdown synthesis page(s).
You MUST use your native `write_to_file` / `multi_replace_file_content` tools to write directly to `{wiki_dir}`.
Make sure the filename starts with `Synthesis_`.

[CRITICAL REQUIREMENT: GAP ANALYSIS]
You MUST include a section titled "## 盲区与缺失度分析 (Gap Analysis)" at the end of your synthesis.
In this section, explicitly state:
1. What crucial evidence is MISSING to definitively answer the query.
2. The staleness of the retrieved context.
3. Unresolved contradictions flagged in the Operational Memory warnings.
-----------------------
"""
    return instructions

def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    if not files_written_str.strip():
        return "No files were written."
        
    wiki_dir = str(get_wiki_dir())
    changed_node_files = set([f.strip() for f in files_written_str.split(",") if f.strip()])
    
    valid_files = set()
    for filename in changed_node_files:
        if not filename.startswith(("Concept_", "Vendor_", "Product_", "Person_", "Event_", "Source_", "Synthesis_")):
            log.warning(f"Ignoring non-wiki file: {filename}")
            continue
        file_path = os.path.join(wiki_dir, filename)
        if os.path.exists(file_path):
            valid_files.add(filename)
            sanitize_wiki_node(file_path)
            
    if valid_files:
        indexer.generate_index()
        stubs_created = _generate_stubs_for_broken_links(wiki_dir, valid_files)
        if stubs_created:
            indexer.generate_index()
        change_set = governance_store.sync_pages_to_canonical(
            [os.path.join(wiki_dir, filename) for filename in valid_files],
            origin="query",
            auto_approve=True,
            summary=f"Query synthesis for: {query_str[:80]}",
        )
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return (
            f"Query finalization completed. {len(valid_files)} page(s) synced. {stubs_created} stub(s) generated.\n"
            f"Canonical change set: {change_set['change_set_id'] if change_set else 'none'}\n\n{trace}"
        )
    return "Query finalization completed with no valid wiki files synced."


def _generate_stubs_for_broken_links(wiki_dir: str, files_to_scan: set) -> int:
    existing_files = {name.replace(".md", "") for name in os.listdir(wiki_dir) if name.endswith(".md")}
    normalized_existing = {normalize_entity_name(f) for f in existing_files}
    broken_targets = set()

    for filename in files_to_scan:
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                content = handle.read()
        except Exception:
            continue

        for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
            raw_target = match.group(1).strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            if target and target not in normalized_existing and target not in existing_files:
                broken_targets.add(target)
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            raw_target = match.group(1).strip().split("|")[0].strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            if target and target not in normalized_existing and target not in existing_files:
                broken_targets.add(target)

    if not broken_targets:
        return 0

    stubs = 0
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    for target in broken_targets:
        node_type = target.split("_")[0].lower() if target.startswith(("Concept_", "Vendor_", "Product_", "Person_", "Event_", "Source_", "Synthesis_")) else "concept"
        frontmatter = {
            "title": target.replace("_", " "),
            "type": node_type,
            "domain": "General",
            "topic_cluster": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "tags": ["auto-stub"],
            "created": today,
            "updated": today,
            "sources": [],
        }
        body = (
            f"# {target.replace('_', ' ')}\n\n"
            "> This is an auto-generated stub page. It was referenced by another wiki page but did not exist.\n"
            "> Please expand with real content when information becomes available.\n"
        )
        try:
            write_markdown_file(os.path.join(wiki_dir, f"{target}.md"), frontmatter, body)
            stubs += 1
            existing_files.add(target)
            log.info(f"[Stub] Created seed page: {target}.md")
        except Exception as e:
            log.warning(f"Failed to create stub {target}.md: {e}")

    return stubs

