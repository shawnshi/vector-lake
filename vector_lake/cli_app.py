import argparse
import io
import sys

try:
    import dotenv
except ImportError:
    dotenv = None

from vector_lake import host_env, tools


def _configure_stdout():
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _load_env():
    if dotenv is None:
        return
    env_path = host_env.legacy_env_file()
    if env_path.exists():
        dotenv.load_dotenv(str(env_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Vector Lake CLI Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage Examples:
  python cli.py sync
  python cli.py search "MSL" --top_k 5
  python cli.py search "deployment target" --mode memory
  python cli.py review
  python cli.py review resolve 0
  python cli.py review resolve review_ab12cd34ef56
  python cli.py delete "/path/to/raw/file.pdf"            # dry-run by default
  python cli.py delete "/path/to/raw/file.pdf" --apply     # perform the delete
  python cli.py doctor
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Available wiki operations")

    subparsers.add_parser("sync", help="[INGEST] Generates MCP ingestion instructions for Native Subagents.")
    ingest_tasks_parser = subparsers.add_parser("ingest-tasks", help="[INGEST] List or expire subagent ingest tasks.")
    ingest_tasks_parser.add_argument("--limit", type=int, default=20, help="Maximum number of jobs to list.")
    ingest_tasks_parser.add_argument("--awaiting-only", action="store_true", help="Hide queued jobs and show only awaiting-subagent jobs.")
    ingest_tasks_parser.add_argument("--expire-stale", action="store_true", help="Expire stale awaiting-subagent jobs instead of listing.")
    ingest_tasks_parser.add_argument("--claim", action="store_true", help="Lease awaiting task packets to this host runtime.")
    ingest_tasks_parser.add_argument("--max-age-seconds", type=int, default=86400, help="Age threshold for --expire-stale.")
    ingest_tasks_parser.add_argument("--lease-seconds", type=int, default=3600, help="Lease duration for --claim.")
    ingest_tasks_parser.add_argument("--abandoned", action="store_true", help="List sources withheld after repeated deterministic failures.")
    ingest_tasks_parser.add_argument("--terminal-failed", action="store_true", help="List jobs that spent their attempt budget.")
    ingest_tasks_parser.add_argument("--close-terminal-failed", action="store_true", help="Mark terminal-failed jobs superseded once their source is ingested.")
    ingest_tasks_parser.add_argument("--clear-abandoned", nargs="?", const="", default=None, metavar="FILE", help="Allow abandoned source(s) to be dispatched again (all, or one FILE).")

    lint_parser = subparsers.add_parser("lint", help="[LINT] Run self-healing audit on the Wiki nodes.")
    lint_parser.add_argument("--auto-fix", action="store_true", help="Automatically fix issues such as decaying notes.")

    search_parser = subparsers.add_parser("search", help="[SEARCH] CJK-aware search with graph expansion.")
    search_parser.add_argument("query", help="Semantic query string.")
    search_parser.add_argument("--top_k", type=int, default=5, help="Number of results (default: 5).")
    search_parser.add_argument("--domain", type=str, default=None, help="Filter by domain namespace.")
    search_parser.add_argument("--cluster", type=str, default=None, help="Filter by topic cluster.")
    search_parser.add_argument("--include-history", action="store_true", help="Bypass temporal invalidation to search deprecated facts.")
    search_parser.add_argument("--mode", choices=["page", "memory", "claim"], default="page", help="Search page index, operational memory, or fact claims.")

    query_parser = subparsers.add_parser("query", help="[QUERY] Deep reasoning with budget-controlled context.")
    query_parser.add_argument("query_str", help="The topic or command for reasoning.")
    query_parser.add_argument("--dry-run", action="store_true", help="Output Markdown to stdout only without persisting to disk.")

    subparsers.add_parser("graph", help="[GRAPH] Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.")
    timeline_rebuild_parser = subparsers.add_parser("timeline-rebuild", help="[TIMELINE] Rebuild timeline_events from timeline-event claims.")
    timeline_rebuild_parser.add_argument("--apply", action="store_true", help="Persist the rebuilt projection. Defaults to dry-run.")
    timeline_rebuild_parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of claims to project.")
    timeline_repair_parser = subparsers.add_parser(
        "timeline-repair",
        help="[TIMELINE] Close timeline_events parity drift in place instead of rebuilding the whole table.",
    )
    timeline_repair_parser.add_argument("--apply", action="store_true", help="Persist the repair. Defaults to dry-run.")

    gram_index_parser = subparsers.add_parser(
        "gram-index",
        help="[SEARCH] Report or rebuild the exact n-gram index used by operational-memory search.",
    )
    gram_index_parser.add_argument("--apply", action="store_true", help="Run the bulk rebuild. Defaults to dry-run.")
    gram_index_parser.add_argument("--if-due", action="store_true", dest="if_due", help="Rebuild only if REBUILD_AFTER_WRITES documents have been written since the last rebuild.")

    projection_report_parser = subparsers.add_parser("projection-report", help="[MAINTENANCE] Report Wiki / canonical / index drift.")
    projection_report_parser.add_argument("--limit", type=int, default=20, help="Sample size per drift bucket.")

    canonical_backfill_parser = subparsers.add_parser("canonical-backfill", help="[MAINTENANCE] Backfill missing canonical rows from Wiki pages.")
    canonical_backfill_parser.add_argument("--apply", action="store_true", help="Persist the backfill. Defaults to dry-run.")
    canonical_backfill_parser.add_argument("--limit", type=int, default=50, help="Maximum number of missing pages to process.")

    index_rebuild_parser = subparsers.add_parser("projection-rebuild-index", help="[MAINTENANCE] Rebuild index projection from canonical SQLite.")
    index_rebuild_parser.add_argument("--apply", action="store_true", help="Persist rebuilt index projection. Defaults to dry-run.")

    embedding_backfill_parser = subparsers.add_parser("embedding-backfill", help="[MAINTENANCE] Backfill missing vector embeddings under rate limits.")
    embedding_backfill_parser.add_argument("--apply", action="store_true", help="Persist embeddings. Defaults to dry-run.")
    embedding_backfill_parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of nodes to embed.")
    embedding_backfill_parser.add_argument("--include-existing", action="store_true", help="Re-embed nodes that already have vectors.")

    wiki_restore_parser = subparsers.add_parser("wiki-restore", help="[MAINTENANCE] Restore missing Wiki pages from canonical metadata.")
    wiki_restore_parser.add_argument("--apply", action="store_true", help="Persist restored Markdown pages. Defaults to dry-run.")
    wiki_restore_parser.add_argument("--limit", type=int, default=10, help="Maximum number of canonical-only pages to restore.")

    review_parser = subparsers.add_parser("review", help="[REVIEW] Inspect and resolve the unified legacy/governance review surface.")
    review_parser.add_argument("action", nargs="?", default="list", choices=["list", "resolve", "ground"], help="Action: 'list' (default), 'resolve', or 'ground'.")
    review_parser.add_argument("index", nargs="?", default="-1", help="Index or item_id of review item to resolve (for 'resolve' action).")
    review_parser.add_argument("--resolution", type=str, default="skip", help="Resolution type: 'skip', 'create', 'merge', 'acknowledge' (default: skip).")

    subparsers.add_parser("audit-graph", help="[AUDIT-GRAPH] Synthesize graph topology insights into the unified review surface.")
    subparsers.add_parser("doctor", help="[DOCTOR] Validate runtime dependencies and filesystem layout.")

    research_parser = subparsers.add_parser("research", help="[RESEARCH] Autonomously scan graph gaps and governance queue to formulate web research directives.")
    research_parser.add_argument("--dry-run", action="store_true", help="Preview the research queries without the SYSTEM DIRECTIVE execution hook.")


    debt_parser = subparsers.add_parser("debt", help="[DEBT] Show governance debt metrics.")
    debt_parser.add_argument("--top", type=int, default=20, help="Top debt window size.")

    trace_parser = subparsers.add_parser("trace", help="[TRACE] Show provenance trace for a query or identifier.")
    trace_parser.add_argument("query_or_id", help="Query text or object identifier.")

    merge_parser = subparsers.add_parser("merge-suggestions", help="[MERGE] Detect and enqueue candidate entity merges.")
    merge_parser.add_argument("--limit", type=int, default=20, help="Maximum number of merge candidates to surface.")
    merge_parser.add_argument("--preview", action="store_true", help="Do not enqueue governance items; only preview candidates.")

    delete_parser = subparsers.add_parser("delete", help="[DELETE] Cascade-delete a raw source and all related wiki pages.")
    delete_parser.add_argument("raw_path", help="Path to the raw source file to remove.")
    delete_parser.add_argument("--apply", action="store_true", help="Persist the cascade delete. Defaults to dry-run.")

    gc_parser = subparsers.add_parser("gc", help="[GC] Automatically prune isolated/orphan entities.")
    gc_parser.add_argument("--days", type=int, default=30, help="Prune entities older than this many days (default: 30).")
    gc_parser.add_argument("--apply", action="store_true", help="Persist the prune. Defaults to dry-run.")
    gc_parser.add_argument("--force", action="store_true", help="Bypass the 50%% mass-deletion safety threshold.")

    backup_parser = subparsers.add_parser(
        "backup-retention",
        help="[MAINTENANCE] Report or enforce the bound on .meta/backups.",
    )
    backup_parser.add_argument("--keep", type=int, default=0, help="Newest copies to retain (0 = configured default).")
    backup_parser.add_argument("--max-bytes", type=int, default=0, help="Byte budget for the older copies (0 = configured default).")
    backup_parser.add_argument("--apply", action="store_true", help="Remove the entries past the bound. Defaults to dry-run; the newest copy is always kept.")

    subparsers.add_parser(
        "idempotency-status",
        help="[MAINTENANCE] Report the uniqueness guarantee each idempotency table has.",
    )

    repair_parser = subparsers.add_parser(
        "repair-idempotency",
        help="[MAINTENANCE] Clear redundant idempotency keys so the full unique index can be created.",
    )
    repair_parser.add_argument("--table", choices=list(tools.IDEMPOTENCY_TABLES), default="mutation_outbox", help="Table to repair (default: mutation_outbox).")
    repair_parser.add_argument("--apply", action="store_true", help="Clear the redundant keys. Defaults to dry-run; no row is deleted either way.")
    return parser


def main() -> int:
    _configure_stdout()
    _load_env()
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "sync":
            print(tools.sync_vector_lake())
        elif args.command == "ingest-tasks":
            if getattr(args, "close_terminal_failed", False):
                print(tools.close_terminal_failed_ingest_jobs())
            elif getattr(args, "terminal_failed", False):
                print(tools.list_terminal_failed_ingest_jobs())
            elif getattr(args, "clear_abandoned", None) is not None:
                target = getattr(args, "clear_abandoned") or None
                print(tools.clear_abandoned_ingest_sources(target))
            elif getattr(args, "abandoned", False):
                print(tools.list_abandoned_ingest_sources())
            elif getattr(args, "expire_stale", False):
                print(tools.expire_ingest_tasks(getattr(args, "max_age_seconds", 86400)))
            elif getattr(args, "claim", False):
                print(tools.claim_ingest_tasks(
                    limit=getattr(args, "limit", 20),
                    lease_seconds=getattr(args, "lease_seconds", 3600),
                ))
            else:
                print(tools.list_ingest_tasks(
                    limit=getattr(args, "limit", 20),
                    include_queued=not getattr(args, "awaiting_only", False),
                ))
        elif args.command == "search":
            print(tools.search_vector_lake(
                args.query,
                args.top_k,
                domain=getattr(args, "domain", None),
                cluster=getattr(args, "cluster", None),
                include_history=getattr(args, "include_history", False),
                mode=getattr(args, "mode", "page"),
            ))
        elif args.command == "lint":
            print(tools.lint_vector_lake(getattr(args, "auto_fix", False)))
        elif args.command == "query":
            print(tools.prepare_query_context(args.query_str, getattr(args, "dry_run", False)))
        elif args.command == "graph":
            print(tools.visualize_vector_lake())
        elif args.command == "timeline-rebuild":
            print(tools.rebuild_timeline_events_from_claims(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", None),
            ))
        elif args.command == "timeline-repair":
            print(tools.repair_timeline_projection(
                dry_run=not getattr(args, "apply", False),
            ))
        elif args.command == "gram-index":
            print(tools.memory_gram_index_report())
            if getattr(args, "if_due", False):
                # The cadence's operator entry point: rebuild only when the write count
                # says so, so this can be run on a schedule without rebuilding a corpus
                # that has not moved.
                print(tools.maybe_rebuild_memory_gram_index(dry_run=not getattr(args, "apply", False)))
            elif getattr(args, "apply", False):
                print(tools.rebuild_memory_gram_index(dry_run=False))
            else:
                print(tools.rebuild_memory_gram_index(dry_run=True))
        elif args.command == "projection-report":
            print(tools.projection_diff_report(limit=getattr(args, "limit", 20)))
        elif args.command == "canonical-backfill":
            print(tools.canonical_backfill_missing_wiki(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", 50),
            ))
        elif args.command == "projection-rebuild-index":
            print(tools.rebuild_index_projection(dry_run=not getattr(args, "apply", False)))
        elif args.command == "embedding-backfill":
            print(tools.embedding_backfill_projection(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", None),
                include_existing=getattr(args, "include_existing", False),
            ))
        elif args.command == "wiki-restore":
            print(tools.restore_missing_wiki_from_canonical(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", 10),
            ))
        elif args.command == "review":
            print(tools.review_vector_lake(action=args.action, index=args.index, resolution=getattr(args, "resolution", "skip")))
        elif args.command == "audit-graph":
            print(tools.audit_graph())
        elif args.command == "doctor":
            print(tools.doctor_vector_lake())
        elif args.command == "research":
            print(tools.research_vector_lake(getattr(args, "dry_run", False)))
        elif args.command == "debt":
            print(tools.debt_vector_lake(getattr(args, "top", 20)))
        elif args.command == "trace":
            print(tools.trace_vector_lake(args.query_or_id))
        elif args.command == "merge-suggestions":
            print(tools.merge_suggestions_vector_lake(limit=getattr(args, "limit", 20), enqueue=not getattr(args, "preview", False)))
        elif args.command == "delete":
            print(tools.delete_source(args.raw_path, dry_run=not getattr(args, "apply", False)))
        elif args.command == "gc":
            print(tools.gc_vector_lake(
                days=getattr(args, "days", 30),
                dry_run=not getattr(args, "apply", False),
                force=getattr(args, "force", False),
            ))
        elif args.command == "backup-retention":
            print(tools.backup_retention_report(
                keep=getattr(args, "keep", 0),
                max_bytes=getattr(args, "max_bytes", 0),
                dry_run=not getattr(args, "apply", False),
            ))
        elif args.command == "idempotency-status":
            print(tools.idempotency_index_report())
        elif args.command == "repair-idempotency":
            print(tools.repair_idempotency_keys(
                table=getattr(args, "table", "mutation_outbox"),
                dry_run=not getattr(args, "apply", False),
            ))
    except Exception as exc:
        print(f"Error executing command '{args.command}': {exc}", file=sys.stderr)
        return 1
    return 0
