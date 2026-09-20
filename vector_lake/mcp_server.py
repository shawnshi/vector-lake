import inspect
import os
import sys
import logging

# A stdio MCP server must not do startup network I/O: fastmcp's default version
# check calls pypi.org on every host start.  This has to be set before fastmcp is
# imported (its settings are read at import time) and stays overridable from the
# environment.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from fastmcp import FastMCP

SERVER_API = "FastMCP"

# Global lock against stdout pollution
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', force=True)
from vector_lake import tools, tool_memory
from vector_lake.tool_timeline import search_timeline_events

mcp = FastMCP("vector-lake")


def registered_tool_names(server=None) -> list[str]:
    """Names of the tools registered on ``server`` (defaults to this module's).

    fastmcp exposes ``list_tools()`` only as a coroutine, while the doctor and the
    test suite need the surface from synchronous code.  Prefer the public API,
    discarding the coroutine when an event loop is already running; fall back to
    the local provider's component registry so a health check cannot crash here.
    """
    server = server if server is not None else mcp
    lister = getattr(server, "list_tools", None)
    if callable(lister):
        try:
            result = lister()
        except Exception:
            result = None
        if inspect.isawaitable(result):
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                try:
                    result = asyncio.run(result)
                except Exception:
                    result = None
            else:
                # A running loop owns this coroutine; close it instead of blocking.
                closer = getattr(result, "close", None)
                if callable(closer):
                    closer()
                result = None
        try:
            listed = list(result) if result is not None else []
        except TypeError:
            listed = []
        if listed:
            return [str(getattr(tool, "name", tool)) for tool in listed]
    provider = getattr(server, "local_provider", None)
    components = getattr(provider, "_components", None) if provider is not None else None
    if isinstance(components, dict):
        registered = [
            str(key).split(":", 1)[1].rsplit("@", 1)[0]
            for key in components
            if str(key).startswith("tool:")
        ]
        if registered:
            return registered
    return []


@mcp.tool()
def search_timeline(entity_name: str = "", sentiment: str = "", action: str = "", limit: int = 10) -> str:
    """Search the strategic timeline events database.
    
    Args:
        entity_name: Filter by entity title (e.g., '卫宁健康'). Leave empty to search all.
        sentiment: Filter by sentiment ('positive', 'neutral', 'negative'). Leave empty for all.
        action: Filter by action type (e.g., 'Release', 'Earnings'). Leave empty for all.
        limit: Number of events to return (default 10).
    """
    return search_timeline_events(
        entity_name=entity_name if entity_name else None,
        sentiment=sentiment if sentiment else None,
        action=action if action else None,
        limit=limit
    )

@mcp.tool()
def rebuild_timeline_events(dry_run: bool = True, limit: int = 0) -> str:
    """Rebuild the timeline_events projection from timeline-event claims."""
    return tools.rebuild_timeline_events_from_claims(
        dry_run=dry_run,
        limit=limit if limit and limit > 0 else None,
    )

@mcp.tool()
def memory_gram_index_status() -> str:
    """Report the exact n-gram index used by operational-memory search.

    The index answers ``search_operational_memory`` in ~0.1-0.5 s instead of the
    O(rows x terms) scan.  When it reports ``usable=False`` the search silently
    falls back to the slower exact scan; run the rebuild to restore the fast path.
    """
    return tools.memory_gram_index_report()

@mcp.tool()
def rebuild_memory_gram_index(dry_run: bool = True) -> str:
    """Bulk-rebuild the operational-memory n-gram index (minutes on a large corpus)."""
    return tools.rebuild_memory_gram_index(dry_run=dry_run)

@mcp.tool()
def backup_retention_report(keep: int = 0, max_bytes: int = 0, dry_run: bool = True) -> str:
    """Report the .meta/backups footprint against its retention bound.

    Backup files are written by the repair, projection and ingest paths and nothing
    used to remove them, so the tree grows without bound (a full copy per backup of
    a multi-GB database).  This reports what lies outside the bound; ``dry_run=False``
    removes exactly those entries.

    Args:
        keep: Newest copies to retain. 0 uses the configured default (3).
        max_bytes: Byte budget for the older copies. 0 uses the configured default (12 GiB).
        dry_run: When True (the default) nothing is removed. The newest copy is
            always retained, and anything unrecognised is never pruned.
    """
    return tools.backup_retention_report(keep=keep, max_bytes=max_bytes, dry_run=dry_run)

@mcp.tool()
def idempotency_index_status() -> str:
    """Report the uniqueness guarantee each idempotency table actually has.

    ``full`` means an idempotency key can never repeat.  ``active`` means the table
    already held duplicate history written by an earlier release, so uniqueness is
    enforced over non-terminal rows only -- the population a concurrent enqueue can
    collide in.  ``absent`` means only the write lock protects enqueue.
    """
    return tools.idempotency_index_report()

@mcp.tool()
def repair_idempotency_keys(table: str = "mutation_outbox", dry_run: bool = True) -> str:
    """Clear the redundant idempotency keys so the full unique index can be created.

    Use this after ``idempotency_index_status`` reports ``active`` or ``absent``.
    Only the duplicate *key* is cleared: the row, its status, timestamps, error text
    and superseded_by link are all kept, so no audit history is lost.  Each key stays
    on its canonical (lowest id) row -- the one enqueue already returns for it.

    Args:
        table: 'mutation_outbox' or 'jobs'.
        dry_run: When True (the default) no row is changed.
    """
    return tools.repair_idempotency_keys(table=table, dry_run=dry_run)

@mcp.tool()
def projection_report(limit: int = 20) -> str:
    """Report drift between Wiki pages, SQLite canonical entities, and index.json."""
    return tools.projection_diff_report(limit=limit)

@mcp.tool()
def canonical_backfill(dry_run: bool = True, limit: int = 50) -> str:
    """Backfill missing SQLite canonical rows from existing Wiki pages."""
    return tools.canonical_backfill_missing_wiki(dry_run=dry_run, limit=limit)

@mcp.tool()
def projection_rebuild_index(dry_run: bool = True) -> str:
    """Rebuild index.json, FTS, embeddings, and claim_topology from SQLite canonical state."""
    return tools.rebuild_index_projection(dry_run=dry_run)

@mcp.tool()
def embedding_backfill(dry_run: bool = True, limit: int = 0, include_existing: bool = False) -> str:
    """Backfill missing vector embeddings under RPM/TPM rate limits."""
    return tools.embedding_backfill_projection(
        dry_run=dry_run,
        limit=limit if limit and limit > 0 else None,
        include_existing=include_existing,
    )

@mcp.tool()
def wiki_restore(dry_run: bool = True, limit: int = 10) -> str:
    """Restore missing Wiki Markdown pages from canonical metadata."""
    return tools.restore_missing_wiki_from_canonical(dry_run=dry_run, limit=limit)

@mcp.tool()
def search_vector_lake(query: str, top_k: int = 5, mode: str = "page") -> str:
    """Search the Vector Lake index.
    
    Args:
        query: The semantic query string.
        top_k: Number of results to return.
        mode: Search mode, can be 'page', 'memory', or 'claim'.
    """
    return tools.search_vector_lake(query, top_k, mode=mode)

def _payload_path_allowed(abs_path) -> bool:
    """Sandbox predicate for agent payload files.

    Roots are additive: ``VECTOR_LAKE_PAYLOAD_ROOT`` *extends* the built-in
    sandboxes instead of replacing them, so an operator override can no longer
    silently revoke the repository-local ``brain/`` sandbox.

    The built-in roots still require the ``<root>/<project>/scratch/...`` shape
    because they are shared with every other host process.  An explicitly
    configured root is trusted as-is: the operator named that exact directory,
    and ``host_env.payload_root_from_env()`` already rejects a filesystem anchor.
    """
    from vector_lake import host_env

    configured_root = host_env.payload_root_from_env()
    if configured_root is not None and abs_path.is_relative_to(configured_root):
        return True
    for root in host_env.payload_sandbox_roots():
        if not abs_path.is_relative_to(root):
            continue
        relative_parts = abs_path.relative_to(root).parts
        if len(relative_parts) >= 3 and relative_parts[1].lower() == "scratch":
            return True
    return False


def _read_payload(payload_file: str) -> str:
    if not payload_file:
        return ""
    import os
    from pathlib import Path

    abs_path = Path(payload_file).resolve()
    if not _payload_path_allowed(abs_path):
        raise ValueError(f"[Security Error] Payload file must be within an approved agent sandbox: {payload_file}")
    if not abs_path.exists() or not abs_path.is_file():
        raise ValueError(f"[Sandbox Error] Payload file not found: {payload_file}. Create it inside an approved sandbox first.")
    max_bytes = max(1, int(os.environ.get("VECTOR_LAKE_PAYLOAD_MAX_BYTES", str(5 * 1024 * 1024))))
    if abs_path.stat().st_size > max_bytes:
        raise ValueError(f"[Sandbox Error] Payload file exceeds {max_bytes} bytes: {payload_file}")
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

@mcp.tool()
def update_operational_memory(memory_type: str, payload_file: str) -> str:
    """Safely persist an operational memory (preference, decision, fact, task_state) without corrupting the graph.
    
    Args:
        memory_type: Type of memory ('preference', 'decision', 'fact', 'task_state').
        payload_file: Absolute path to a temporary file containing the text content of the memory.
    """
    try:
        content = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    return tool_memory.update_operational_memory(memory_type, content)

@mcp.tool()
def sync_vector_lake() -> str:
    """(Legacy Alias) Trigger an ingestion batch scan. Replaced by the asynchronous Subagent pipeline, now wraps prepare_ingest_batch."""
    try:
        return tools.sync_vector_lake()
    except Exception as e:
        import traceback
        logging.error(f"MCP Tool Exception (sync_vector_lake): {e}\n{traceback.format_exc()}")
        return f"MCP Exception: {str(e)}\n{traceback.format_exc()}"

@mcp.tool()
def lint_vector_lake(auto_fix: bool = False) -> str:
    """Run self-healing audit on the Wiki nodes.
    
    Args:
        auto_fix: Automatically fix issues such as decaying notes.
    """
    try:
        return tools.lint_vector_lake(auto_fix=auto_fix)
    except Exception as e:
        import traceback
        logging.error(f"MCP Tool Exception (lint_vector_lake): {e}\n{traceback.format_exc()}")
        return f"MCP Exception: {str(e)}\n{traceback.format_exc()}"

@mcp.tool()
def query_logic_lake(query_str: str) -> str:
    """Deep reasoning with budget-controlled context.
    
    Args:
        query_str: The topic or command for reasoning.
    """
    return tools.prepare_query_context(query_str)

@mcp.tool()
def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    """Finalize the logic lake query by indexing the new pages and syncing to the governance store.
    
    Args:
        files_written_str: Comma-separated list of filenames (e.g. 'Synthesis_Topic.md') that were written by the subagent.
        query_str: The original query string for the trace.
    """
    return tools.finalize_query_synthesis(files_written_str, query_str)

@mcp.tool()
def review_governance_list() -> str:
    """List pending items in the governance review queue (contradictions, gaps, merges)."""
    return tools.review_vector_lake(action="list")

@mcp.tool()
def resolve_governance_item(item_id: str, resolution: str, payload_file: str = None) -> str:
    """Resolve a governance item.

    Args:
        item_id: The ID or index of the item.
        resolution: Resolution action: 'skip', 'create', 'merge', 'acknowledge'.
        payload_file: Optional absolute path to a temporary JSON file containing the expected outcome manifest (e.g. {"allow_cycles": false}).
    """
    import json
    manifest = None
    if payload_file:
        try:
            manifest_str = _read_payload(payload_file)
            if manifest_str.strip():
                manifest = json.loads(manifest_str)
        except json.JSONDecodeError as e:
            return f"[Sandbox JSON Error] Failed to parse payload file {payload_file}: {e}. Please fix the JSON and retry."
        except Exception as e:
            return str(e)
    return tools.review_vector_lake(action="resolve", index=item_id, resolution=resolution, change_manifest=manifest)
@mcp.tool()
def trigger_autonomous_research(dry_run: bool = False) -> str:
    """Autonomously scan graph gaps and governance queue to formulate web research directives.
    
    Args:
        dry_run: If true, just lists the topics without emitting a SYSTEM DIRECTIVE.
    """
    return tools.research_vector_lake(dry_run=dry_run)

@mcp.tool()
def review_strategic_purpose(as_of: str = "") -> str:
    """Review due Standing Intelligence Requirements without changing the Wiki.

    Args:
        as_of: Optional YYYY-MM-DD date. Defaults to the current day.
    """
    return tools.review_strategic_purpose(as_of=as_of)

@mcp.tool()
def get_governance_debt(top: int = 20) -> str:
    """Show governance debt metrics.
    
    Args:
        top: Number of top items to show.
    """
    return tools.debt_vector_lake(top=top)

@mcp.tool()
def trigger_audit_graph() -> str:
    """Synthesize graph topology insights into the unified review surface."""
    return tools.audit_graph()

@mcp.tool()
def delete_source(raw_path: str, dry_run: bool = True) -> str:
    """Cascade-delete a raw source and all related wiki pages.
    
    Args:
        raw_path: Path to the raw source file to remove.
        dry_run: Preview what would be deleted without making changes.
    """
    return tools.delete_source(raw_path, dry_run=dry_run)

@mcp.tool()
def doctor_vector_lake() -> str:
    """Validate runtime dependencies and filesystem layout health."""
    return tools.doctor_vector_lake()

@mcp.tool()
def rename_entity(old_name: str, new_name: str, dry_run: bool = True) -> str:
    """Rename a Wiki entity (filename/frontmatter) and automatically update all referring markdown links.
    
    Args:
        old_name: Current name of the entity (e.g. 'Concept_Old-Name.md').
        new_name: New name for the entity (e.g. 'Concept_New-Name.md').
        dry_run: Preview changes without writing to disk.
    """
    from vector_lake.tool_rename import rename_vector_lake_entity
    return rename_vector_lake_entity(old_name, new_name, dry_run=dry_run)

@mcp.tool()
def trace_vector_lake(query_or_id: str) -> str:
    """Show provenance trace for a query or identifier.
    
    Args:
        query_or_id: Query text or object identifier.
    """
    return tools.trace_vector_lake(query_or_id)

@mcp.tool()
def merge_suggestions_vector_lake(limit: int = 20, enqueue: bool = False) -> str:
    """Detect and surface candidate entity merges.
    
    Args:
        limit: Maximum number of merge candidates to surface.
        enqueue: If True, enqueue the candidates into the governance review queue.
    """
    return tools.merge_suggestions_vector_lake(limit=limit, enqueue=enqueue)

@mcp.tool()
def gc_vector_lake(days: int = 30, dry_run: bool = True, force: bool = False) -> str:
    """Automatically prune isolated or orphaned entities.

    Args:
        days: Prune entities older than this many days (default: 30).
        dry_run: Preview what would be deleted without making changes (default: True).
        force: Bypass the 50% mass-deletion safety threshold. Only set after
            inspecting the page-edge projection with projection-report.
    """
    return tools.gc_vector_lake(days=days, dry_run=dry_run, force=force)

@mcp.tool()
def prepare_ingest_batch(batch_size: int = 5) -> str:
    """Scan for unprocessed raw sources and prepare subagent ingestion instructions.
    
    Args:
        batch_size: Number of files to process in this batch (default: 5).
    """
    return tools.prepare_ingest_batch(batch_size=batch_size)

@mcp.tool()
def list_ingest_tasks(limit: int = 20, include_queued: bool = True) -> str:
    """List queued or awaiting-subagent ingest jobs."""
    return tools.list_ingest_tasks(limit=limit, include_queued=include_queued)

@mcp.tool()
def claim_ingest_tasks(limit: int = 5, lease_seconds: int = 3600) -> str:
    """Lease awaiting ingest task packets to the current-environment subagent host."""
    return tools.claim_ingest_tasks(limit=limit, lease_seconds=lease_seconds)

@mcp.tool()
def list_terminal_failed_ingest_jobs() -> str:
    """List ingest jobs that spent their attempt budget, with the source each names."""
    return tools.list_terminal_failed_ingest_jobs()


@mcp.tool()
def close_terminal_failed_ingest_jobs(source_ingested_only: bool = True) -> str:
    """Mark terminal-failed jobs superseded once their source has been ingested."""
    return tools.close_terminal_failed_ingest_jobs(source_ingested_only=source_ingested_only)


@mcp.tool()
def list_abandoned_ingest_sources() -> str:
    """List raw sources withheld from dispatch after repeated deterministic failures."""
    return tools.list_abandoned_ingest_sources()


@mcp.tool()
def clear_abandoned_ingest_sources(filepath: str = "") -> str:
    """Allow abandoned ingest source(s) to be dispatched again (empty filepath = all)."""
    return tools.clear_abandoned_ingest_sources(filepath or None)


@mcp.tool()
def expire_ingest_tasks(max_age_seconds: int = 86400) -> str:
    """Expire stale awaiting-subagent ingest jobs so they can be retried deliberately."""
    return tools.expire_ingest_tasks(max_age_seconds=max_age_seconds)

@mcp.tool()
def finalize_ingest(
    files_written: list = None,
    processed_data: dict = None,
    files_written_payload_file: str = "",
    raw_files_payload_file: str = "",
) -> str:
    """Finalize ingestion after a subagent has produced validated wiki pages.
    
    Args:
        files_written: Direct list of dicts with 'filename' and 'content'.
        processed_data: Claimed job dict with filepath/hash/source_hash/lease fields plus an integration disposition manifest.
        files_written_payload_file: Sandbox JSON file containing files_written.
        raw_files_payload_file: Sandbox JSON file containing processed_data.
    """
    import json
    try:
        import json
        if files_written_payload_file or raw_files_payload_file:
            if not files_written_payload_file or not raw_files_payload_file:
                return "Error: Both payload files are required when using the file-based ingest contract."
            files_written = json.loads(_read_payload(files_written_payload_file))
            processed_data = json.loads(_read_payload(raw_files_payload_file))
        if not isinstance(files_written, list) or not isinstance(processed_data, dict):
            return "Error: finalize_ingest requires a files list and processed-data object."
        return tools.finalize_ingest(files_written, processed_data)
    except Exception as e:
        return str(e)

@mcp.tool()
def check_duplicate_entity(candidate_title: str, candidate_type: str, candidate_summary: str = "") -> str:
    """Check if an entity or concept already exists in the graph to prevent duplicates.
    
    Args:
        candidate_title: The title of the entity to create.
        candidate_type: The specific type of the entity (e.g. 'vendor', 'product', 'person', 'event', 'concept').
        candidate_summary: A brief summary of the entity to use for similarity matching.
    """
    return tools.check_duplicate_entity(candidate_title, candidate_type, candidate_summary)

@mcp.tool()
def visualize_vector_lake(output_dir: str = None) -> str:
    """Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.

    ``output_dir`` is an *output* directory, not an input sandbox: this call
    writes exactly one file (``vector_lake_graph.html``) into it, so the gate
    only requires a directory under a known host home.  Compare
    ``_read_payload``, which additionally requires the ``<root>/<project>/scratch``
    shape because it reads arbitrary caller-named content.  When ``output_dir``
    is omitted the file goes to the lake's own ``<MEMORY>/scratch`` tree.
    """
    if output_dir:
        from vector_lake import host_env

        if not host_env.is_within_host_home(output_dir):
            return "Error: Output directory must be inside an approved agent sandbox."
    return tools.visualize_vector_lake(output_dir)

@mcp.tool()
def write_wiki_page(filename: str, payload_file: str) -> str:
    """Write or update a Vector Lake wiki page safely.
    
    Args:
        filename: The filename (e.g. 'Concept_Example.md').
        payload_file: Absolute path to a temporary file containing the full markdown content including YAML frontmatter.
    """
    try:
        content = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    from vector_lake.wiki_utils import SafeWriteError
    try:
        from vector_lake.mutation_coordinator import execute_mutation_plan
        execute_mutation_plan(filename, content=content, is_delete=False)
        return f"Successfully wrote {filename} and queued index update."
    except SafeWriteError as e:
        return f"[Write Rejected] {str(e)}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

import uuid
from vector_lake.governance_store import load_governance_queue, save_governance_queue, _utc_now

@mcp.tool()
def propose_schema_mutation(new_category: str, payload_file: str, parent_category: str = "Uncategorized") -> str:
    """Propose a new taxonomy category to the ontology team.
    
    Args:
        new_category: The name of the new category.
        payload_file: Absolute path to a temporary file containing a brief definition or justification for the category.
        parent_category: The parent category (default: 'Uncategorized').
    """
    try:
        description = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    from vector_lake.governance_store import governance_queue_session

    with governance_queue_session():
        queue = load_governance_queue()
        item_id = f"gov_{uuid.uuid4().hex[:12]}"
        queue.setdefault("items", []).append({
            "item_id": item_id,
            "type": "schema-mutation",
            "title": f"New Schema Category: {new_category}",
            "description": f"Definition: {description}\nParent: {parent_category}",
            "created_at": _utc_now(),
            "status": "pending",
            "source": "mcp-agent",
            "affected_ids": [],
            "search_queries": [],
            "affected_pages": ["SCHEMA_CATEGORIES.md"],
        })
        save_governance_queue(queue)
    return f"Schema mutation proposed and logged as {item_id} for review."



@mcp.tool()
def batch_replace_links(old_text: str, new_text: str, dry_run: bool = True) -> str:
    """Batch replace occurrences of a string (usually a link) across all wiki pages.
    Use this when an entity's name changes but `rename_entity` failed to cover all cases.
    
    Args:
        old_text: The exact string to search for (e.g. '[[Old Name]]').
        new_text: The exact replacement string (e.g. '[[New Name]]').
        dry_run: If True, only count how many files would be modified without actually changing them.
    """
    
    if old_text.strip() in ["", "---", "[[", "]]", "```", "#"]:
        return f"Error: '{old_text}' is a structural syntax marker. Global replacement aborted to protect graph topology."
        
    import os
    from vector_lake.wiki_utils import get_wiki_dir
    from vector_lake.mutation_coordinator import execute_mutation_batch
    wiki_dir = get_wiki_dir()
    modified_count = 0
    matched_files = []
    mutations = []
    
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            if old_text in content:
                mutations.append({"filename": filename, "content": content.replace(old_text, new_text)})
                modified_count += 1
                matched_files.append(filename)
        except Exception as e:
            logging.error(f"Error processing {filename} for link replacement: {e}")
            
    if dry_run:
        return f"[DRY RUN] Would replace '{old_text}' with '{new_text}' in {modified_count} files: {', '.join(matched_files[:10])}..."

    if mutations:
        execute_mutation_batch(mutations)
    return f"Successfully replaced '{old_text}' with '{new_text}' in {modified_count} files and queued projections."

@mcp.tool()
def bulk_reconciliation(payload_file: str, dry_run: bool = True) -> str:
    """Execute a batch of graph reconciliation operations (merge, replace_only, alias).
    
    Args:
        payload_file: Absolute path to a temporary JSON file containing the operations array.
        dry_run: Whether to perform a dry run (default: True).
    """
    import json
    try:
        content = _read_payload(payload_file)
        operations = json.loads(content)
    except Exception as e:
        return str(e)
    from vector_lake.tool_bulk_reconciliation import bulk_reconcile
    return bulk_reconcile(operations, dry_run)

if __name__ == "__main__":
    # stdout carries the JSON-RPC stream; no banner, no version check.
    #
    # The embedding SDK costs ~4.8 s of first-use latency (``import google.genai`` 3.5 s plus
    # ``genai.Client()`` 1.3 s) and this process answers many searches, so it pays that off
    # the request path while the host completes the handshake.  Best effort: if it does not
    # finish in time, the first embedding call simply pays the cost as before.
    from vector_lake.embedding_scheduler import start_prewarm_thread

    start_prewarm_thread()
    mcp.run(show_banner=False)
