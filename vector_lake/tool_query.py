import datetime
import logging
import os
import re
import subprocess

from vector_lake import governance_store
from vector_lake import indexer
from vector_lake import provenance
from vector_lake.tool_search import assemble_context
from vector_lake.wiki_utils import get_wiki_dir, sanitize_wiki_node, write_markdown_file


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-query")


import time
import json
from vector_lake import get_extension_root

def _run_gemini(prompt: str):
    config_path = get_extension_root() / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            llm_config = json.load(f).get("llm", {})
            timeout = llm_config.get("timeout_query", 120)
            model_cascade = llm_config.get("model_cascade", ["default", "gemini-3.1-flash", "gemini-3.1-8b"])
    except Exception:
        timeout = 120
        model_cascade = ["default", "gemini-3.1-flash", "gemini-3.1-8b"]
    
    retries = len(model_cascade)
    last_err = None
    for attempt in range(retries):
        try:
            current_model = model_cascade[attempt]
            model_flag = [] if current_model in ("", "default") else ["-m", current_model]
            model_disp = current_model if current_model not in ("", "default") else "default"
            log.info(f"Invoking gemini.cmd (Attempt {attempt + 1}/{retries})... (Model: {model_disp})")
            cmd = ["gemini.cmd"] + model_flag + ["-p", "You are a Vector Lake Synthesizer.", "--approval-mode", "yolo"]
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return result
            else:
                log.warning(f"gemini.cmd returned {result.returncode}: {result.stderr.strip()}")
                last_err = result.stderr.strip()
        except subprocess.TimeoutExpired as e:
            log.warning(f"gemini.cmd timed out after {timeout} seconds on attempt {attempt + 1}.")
            last_err = str(e)
        except Exception as e:
            log.error(f"gemini.cmd failed unexpectedly: {e}")
            last_err = str(e)
        
        if attempt < retries - 1:
            time.sleep(3)
            
    # Return a fake CompletedProcess representing failure
    return subprocess.CompletedProcess(
        args=["gemini.cmd"],
        returncode=1,
        stdout="",
        stderr=f"Exhausted {retries} retries. Last error: {last_err}"
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

    if dry_run:
        prompt = (
            "You are drafting a Vector Lake synthesis preview.\n"
            "Return exactly one Markdown document with valid YAML frontmatter for a synthesis page.\n"
            "Do not write files. Do not mention that this is a preview.\n"
            "Treat OPERATIONAL MEMORY PACKET as the authoritative machine-facing state when it conflicts with page prose.\n"
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

    prompt = f"""@vector-lake-synthesizer
[SYSTEM DIRECTIVE: PYTHON-LED I/O]
You are running in a restricted sandbox. DO NOT use the `write_file`, `replace`, or `run_shell_command` tools.
You MUST output the generated synthesis pages in the following plain text format exactly:

---FILE: Synthesis_Topic_Name.md---
(yaml frontmatter)
(body content)
---END FILE---

Treat OPERATIONAL MEMORY PACKET as the authoritative machine-facing state when it conflicts with page prose.
Query: {query_str}{context_block}"""
    try:
        result = _run_gemini(prompt)
    except Exception as e:
        return f"Error: {e}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return stderr or "Query failed."

    stdout_str = result.stdout or ""
    changed_node_files = set()
    
    from vector_lake.wiki_utils import atomic_write_text
    from pathlib import Path
    file_blocks = re.finditer(r"---FILE:\s*([^\n]+)---\n(.*?)\n---END FILE---", stdout_str, re.DOTALL)
    for match in file_blocks:
        filename = match.group(1).strip()
        content = match.group(2).strip()
        if not filename.startswith(("Source_", "Entity_", "Concept_", "Synthesis_")):
            log.warning(f"Intercepted illegal write attempt to {filename}")
            continue
        file_path = os.path.join(wiki_dir, filename)
        try:
            atomic_write_text(Path(file_path), content)
            changed_node_files.add(filename)
        except Exception as e:
            log.error(f"Failed to write {filename}: {e}")

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

