import datetime
import logging
import os
import re

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
    
    # V11.2 Multi-Hop Parallel Retrieval detection
    is_comparative = "vs" in query_str.lower() or "对比" in query_str
    
    context = assemble_context(query_str)
    context_block = ""
    
    if is_comparative:
        context_block += "\n[SYSTEM NOTE: This is a comparative query. Ensure equal retrieval weighting for both sides to avoid skew.]\n"
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
    
    # Create a unique hash for this query with anti-collision
    import time
    import uuid
    unique_str = f"{query_str}_{time.time()}_{uuid.uuid4().hex}"
    query_hash = hashlib.md5(unique_str.encode("utf-8")).hexdigest()[:12]
    payload_path = tmp_dir / f"query_context_{query_hash}.md"
    
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(context_block)
        
    if dry_run:
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return f"[DRY RUN] Context assembled at {payload_path}\n\nTrace:\n{trace}"

    templates_dir = get_extension_root() / "templates"
    prompt_path = templates_dir / "query_prompt.md"
    if prompt_path.exists():
        prompt_template = prompt_path.read_text(encoding="utf-8")
    else:
        prompt_template = "Error: templates/query_prompt.md not found."
        
    instructions = prompt_template.replace("{{payload_path}}", str(payload_path)) \
        .replace("{{query_str}}", query_str) \
        .replace("{{wiki_dir}}", wiki_dir)
        
    return instructions

def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    if not files_written_str.strip():
        return "No files were written."
        
    wiki_dir = str(get_wiki_dir())
    changed_node_files = set([f.strip() for f in files_written_str.split(",") if f.strip()])
    
    valid_files = set()
    import pathlib
    wiki_path = pathlib.Path(wiki_dir).resolve()
    
    for filename in changed_node_files:
        # Boundary check to prevent path traversal
        try:
            target_path = (wiki_path / filename).resolve()
            if not target_path.is_relative_to(wiki_path):
                log.warning(f"Security: Path traversal attempt detected: {filename}")
                continue
        except Exception as e:
            log.warning(f"Security: Invalid path {filename}: {e}")
            continue

        # P1-3: Dynamic Ontology Prefix Checking
        prefix = filename.split('_')[0] + "_" if "_" in filename else ""
        if not prefix or not prefix[0].isupper() or not filename.endswith(".md"):
            log.warning(f"File {filename} missing standard prefix. Treating as Orphan.")
            new_filename = f"Orphan_{filename}" if not filename.startswith("Orphan_") else filename
            new_target_path = (wiki_path / new_filename).resolve()
            if not new_target_path.is_relative_to(wiki_path):
                log.warning(f"Security: Path traversal attempt in renamed file: {new_filename}")
                continue
                
            if filename != new_filename and target_path.exists():
                os.rename(target_path, new_target_path)
                filename = new_filename
        
        file_path = os.path.join(wiki_dir, filename)
        if os.path.exists(file_path):
            # P1-2: Quality Gate for Gap Analysis
            if filename.startswith("Synthesis_"):
                # Synthesis structure is already validated by execute_mutation_plan during subagent write_wiki_page
                pass
            valid_files.add(filename)
            sanitize_wiki_node(file_path)
            
    if valid_files:
        # Subagent already wrote them via write_wiki_page which calls execute_mutation_plan.
        # We only need to generate stubs.
            
        stubs_created = _generate_stubs_for_broken_links(wiki_dir, valid_files)
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return (
            f"Query finalization completed. {len(valid_files)} page(s) synced. {stubs_created} stub(s) generated.\n"
            f"Canonical change set: mutation_coordinator_handled\n\n{trace}"
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

        # P1-1: Pre-strip code blocks to avoid fragile stubbing
        content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        content = re.sub(r'`.*?`', '', content)

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
        node_type = target.split("_")[0].lower() if target.startswith(("Concept_", "Vendor_", "Institution_", "Product_", "Person_", "Event_", "Policy_", "Standard_", "Source_", "Synthesis_")) else "concept"
        frontmatter = {
            "id": target,
            "title": target.replace("_", " "),
            "type": node_type,
            "domain": "General",
            "topic_cluster": "General",
            "status": "Active",
            "epistemic-status": "seed",
            "categories": ["Uncategorized"],
            "tags": ["auto-stub"],
            "created": f"{today}T00:00:00Z",
            "updated": f"{today}T00:00:00Z",
            "sources": [],
        }
        body = (
            f"# {target.replace('_', ' ')}\n\n"
            f"## 1. 编译事实\n"
            f"*[System Directive: This section represents the LATEST consensus.]*\n\n"
            f"Auto-generated stub page for {target}. (Last Reshaped: [[{today}]])\n\n"
        )
        
        # Add the first valid slot for the type
        from vector_lake.schema_validator import VALID_H3_SLOTS
        slots = VALID_H3_SLOTS.get(node_type, [])
        if slots:
            body += f"{slots[0]}\n- [[{target}]] Auto-generated stub.\n\n"
        else:
            body += f"### 基本信息 (General Information)\n- [[{target}]] Auto-generated stub.\n\n"
            
        body += (
            f"---\n\n"
            f"## 2. 证据时间线\n"
            f"*[System Directive: This is the immutable event ledger.]*\n\n"
            f"- [{today}] [Observation] Created stub.\n"
        )
        try:
            import yaml
            from vector_lake.mutation_coordinator import execute_mutation_plan
            frontmatter_str = "---\n" + yaml.dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n"
            execute_mutation_plan(f"{target}.md", content=frontmatter_str + body, is_delete=False)
            stubs += 1
            existing_files.add(target)
            log.info(f"[Stub] Created seed page: {target}.md")
        except Exception as e:
            log.warning(f"Failed to create stub {target}.md: {e}")

    return stubs

