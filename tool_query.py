import datetime
import logging
import os
import re
import subprocess

import governance_store
import indexer
import provenance
from tool_search import assemble_context
from wiki_utils import get_wiki_dir, sanitize_wiki_node, write_markdown_file


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-query")


def _run_gemini(prompt: str):
    return subprocess.run(
        ["gemini.cmd", "-p", "", "--approval-mode", "yolo"],
        input=prompt,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=300,
    )


def query_logic_lake(query_str: str, dry_run: bool = False):
    wiki_dir = str(get_wiki_dir())
    before_mtimes = {}
    if os.path.exists(wiki_dir):
        for name in os.listdir(wiki_dir):
            if name.endswith(".md"):
                path = os.path.join(wiki_dir, name)
                try:
                    before_mtimes[name] = os.path.getmtime(path)
                except OSError:
                    continue

    context = assemble_context(query_str)
    context_block = ""
    if context["wiki_context"]:
        context_block = (
            f"\n\n--- RELEVANT WIKI PAGES ({context['wiki_page_count']} pages, "
            f"{context['budget_used']}/{context['budget_max']} chars) ---\n{context['wiki_context']}"
        )
    if context["purpose"]:
        context_block += f"\n\n--- PURPOSE ---\n{context['purpose']}"

    if dry_run:
        prompt = (
            "You are drafting a Vector Lake synthesis preview.\n"
            "Return exactly one Markdown document with valid YAML frontmatter for a synthesis page.\n"
            "Do not write files. Do not mention that this is a preview.\n"
            f"Query: {query_str}{context_block}"
        )
        try:
            result = _run_gemini(prompt)
        except Exception as e:
            return f"Error: {e}"
        output = (result.stdout or "").strip()
        if result.returncode != 0 and not output:
            return (result.stderr or "").strip() or "Dry-run query failed."
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return f"{output or 'Dry-run query returned no output.'}\n\n{trace}"

    prompt = f"@vector-lake-synthesizer\nQuery: {query_str}{context_block}"
    try:
        result = _run_gemini(prompt)
    except Exception as e:
        return f"Error: {e}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return stderr or "Query failed."

    changed_node_files = set()
    excluded_files = {"index.md", "log.md", "overview.md"}
    if os.path.exists(wiki_dir):
        for name in os.listdir(wiki_dir):
            if not name.endswith(".md") or name in excluded_files:
                continue
            path = os.path.join(wiki_dir, name)
            try:
                after_mtime = os.path.getmtime(path)
            except OSError:
                continue
            if name not in before_mtimes or after_mtime > before_mtimes.get(name, 0):
                changed_node_files.add(name)

    if changed_node_files:
        new_files = {name for name in changed_node_files if name not in before_mtimes}
        updated_files = changed_node_files - new_files
        log.info(f"[Auto-Reingest] Detected {len(new_files)} new and {len(updated_files)} updated wiki node(s): {changed_node_files}")
        for filename in changed_node_files:
            sanitize_wiki_node(os.path.join(wiki_dir, filename))

        indexer.generate_index()
        stubs_created = _generate_stubs_for_broken_links(wiki_dir, changed_node_files)
        if stubs_created:
            indexer.generate_index()
        change_set = governance_store.sync_pages_to_canonical(
            [os.path.join(wiki_dir, filename) for filename in changed_node_files],
            origin="query",
            auto_approve=True,
            summary=f"Query synthesis for: {query_str[:80]}",
        )
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return (
            f"Query completed. {len(new_files)} new page(s) created. "
            f"{len(updated_files)} existing page(s) updated. {stubs_created} stub(s) generated.\n"
            f"Canonical change set: {change_set['change_set_id'] if change_set else 'none'}\n\n{trace}"
        )

    return "Query completed."


def _generate_stubs_for_broken_links(wiki_dir: str, files_to_scan: set) -> int:
    existing_files = {name.replace(".md", "") for name in os.listdir(wiki_dir) if name.endswith(".md")}
    broken_targets = set()

    for filename in files_to_scan:
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                content = handle.read()
        except Exception:
            continue

        for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
            target = match.group(1).strip().replace(".md", "")
            if target and target not in existing_files:
                broken_targets.add(target)
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            target = match.group(1).strip().split("|")[0].strip().replace(".md", "")
            if target and target not in existing_files:
                broken_targets.add(target)

    if not broken_targets:
        return 0

    stubs = 0
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    for target in broken_targets:
        node_type = target.split("_")[0].lower() if target.startswith(("Concept_", "Entity_", "Source_", "Synthesis_")) else "concept"
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
