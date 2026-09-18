import hashlib
import logging
import os
import re
import time

from vector_lake import get_extension_root, provenance
from vector_lake.tool_search import assemble_context
from vector_lake import stub_creator
from vector_lake.wiki_utils import get_wiki_dir, normalize_entity_name, sanitize_wiki_node


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-query")

# This file used to keep its own copy of the prefix list, and a second, inline copy of
# the same list further down.  Both omitted ``System_`` while the comment claimed to
# mirror ``wiki_utils.VALID_PREFIXES``.  The vocabulary now has one owner, and so does what a
# stub is: ``node_vocabulary`` for the
# prefixes and types, ``stub_creator`` for the page a broken link needs.  This file used to
# keep its own copy of the prefix list, a second inline copy of it, a stub writer, and a
# covering-page check that only it had -- which is why ``tool_lint`` forked entities that this
# file refused to fork.


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
            
        stubs_created, stubs_refused = _generate_stubs_for_broken_links(wiki_dir, valid_files)
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        refusal_note = (
            f" {stubs_refused} stub write(s) were refused -- a gate rejected them (see the log)."
            if stubs_refused
            else ""
        )
        return (
            f"Query finalization completed. {len(valid_files)} page(s) synced. {stubs_created} stub(s) generated.{refusal_note}\n"
            f"Canonical change set: mutation_coordinator_handled\n\n{trace}"
        )
    return "Query finalization completed with no valid wiki files synced."


def _generate_stubs_for_broken_links(wiki_dir: str, files_to_scan: set) -> tuple[int, int]:
    """``(created, refused)`` for the broken links in ``files_to_scan``.

    ``refused`` counts writes a gate rejected -- as opposed to names that needed no page, which
    are the normal case and are counted nowhere.  Before this split, a run in which every write
    was refused returned 0 and read exactly like a wiki with nothing to fix.
    """
    existing_files, normalized_existing, existing_cores = stub_creator.existence_index(wiki_dir)
    existing = (existing_files, normalized_existing, existing_cores)
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
            covering = stub_creator.covering_page(target, existing)
            if covering:
                if covering != target:
                    log.warning(
                        "Not creating %s.md: %s already covers that name; fix the link instead.",
                        target,
                        covering,
                    )
                continue
            broken_targets.add(target)
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            raw_target = match.group(1).strip().split("|")[0].strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            covering = stub_creator.covering_page(target, existing)
            if covering:
                if covering != target:
                    log.warning(
                        "Not creating %s.md: %s already covers that name; fix the link instead.",
                        target,
                        covering,
                    )
                continue
            broken_targets.add(target)

    if not broken_targets:
        return 0, 0

    stubs = 0
    refused = 0
    # A name two pages declare is contested, not missing: the owner refuses it, and this caller
    # has to say so too -- it writes stubs as well, and with neither claimant's core matching the
    # name, ``covering_page`` cannot refuse for it.  Measured on the live wiki: 31 such names.
    contested = stub_creator.contested_names(stub_creator.declared_names(wiki_dir))
    # What a stub is -- its name, type, fields and the write path -- belongs to one owner.
    # This used to build the page here and hand ``<target>.md`` to ``execute_mutation_plan``,
    # which no node may be named: the write was refused and the exception swallowed, so this
    # function created no stub at all for an untyped target (and, as it turned out, none for
    # a typed one either).  See ``vector_lake.stub_creator``.
    #
    # ``index`` is the one built before the scan, not a fresh one: ``create_stub`` updates it
    # in place, so a stub written for one target is seen as covering its own name by the next
    # iteration.  A second index here would leave that update on a set nothing reads again.
    for target in sorted(broken_targets):
        outcome = stub_creator.create_stub(wiki_dir, target, existing, contested=contested)
        if outcome.stem:
            stubs += 1
        elif outcome.refused:
            refused += 1
    if refused:
        # The count is about closed gates, not about missing links; folding it into ``stubs``
        # would hide a run in which nothing could be written.
        log.error(
            "%s stub write(s) refused: every stub in this pass failed the same gate "
            "(the log above has the traceback).",
            refused,
        )
    return stubs, refused

