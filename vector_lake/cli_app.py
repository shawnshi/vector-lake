import argparse
import io
import json
import math
import os
import sys

try:
    import dotenv
except ImportError:
    dotenv = None

from vector_lake import tools


_CLI_HEAVY_TASKS = {
    "audit-graph": ("scan", 1800.0),
    "backup-retention": ("maintenance", 900.0),
    "canonical-backfill": ("maintenance", 900.0),
    "change-set-compaction": ("maintenance", 1800.0),
    "debt": ("scan", 900.0),
    "delete": ("maintenance", 900.0),
    "doctor": ("scan", 900.0),
    "embedding-backfill": ("embedding", 3600.0),
    "evidence-foundation-backfill": ("maintenance", 1800.0),
    "gc": ("maintenance", 1800.0),
    "graph": ("scan", 900.0),
    "history-retention": ("maintenance", 1800.0),
    "lint": ("scan", 1800.0),
    "memory-cleanup": ("maintenance", 900.0),
    "memory-search-index": ("maintenance", 1800.0),
    "merge-suggestions": ("scan", 1800.0),
    "orphan-source-classify": ("scan", 900.0),
    "projection-rebuild-index": ("projection", 1800.0),
    "projection-report": ("scan", 900.0),
    "research": ("ingest_scan", 1800.0),
    "schema-migrate": ("maintenance", 1800.0),
    "sync": ("ingest_scan", 1800.0),
    "timeline-rebuild": ("projection", 900.0),
    "topology-queue-cleanup": ("maintenance", 900.0),
    "wiki-restore": ("maintenance", 900.0),
}


def _cli_heavy_task_policy(args) -> tuple[str, float] | None:
    if args.command == "ingest-tasks":
        if any(
            bool(getattr(args, field, False))
            for field in (
                "repair_debt",
                "cleanup_orphans",
                "expire_stale",
            )
        ):
            return "maintenance", 900.0
        return None
    if args.command == "schema-migrate":
        if not (
            getattr(args, "apply", False)
            or getattr(args, "checkpoint_wal", False)
        ):
            return None
    return _CLI_HEAVY_TASKS.get(args.command)


def _cli_heavy_task_wait_seconds() -> float:
    try:
        value = float(
            os.environ.get("VECTOR_LAKE_CLI_HEAVY_TASK_WAIT_SECONDS", "30")
        )
    except (TypeError, ValueError):
        value = 30.0
    if not math.isfinite(value):
        value = 30.0
    return max(0.0, min(300.0, value))


def _configure_stdout():
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )


def _load_env():
    if dotenv is None:
        return
    env_path = os.path.join(os.path.expanduser("~"), ".gemini", ".env")
    if os.path.exists(env_path):
        dotenv.load_dotenv(env_path)


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected zero or a positive integer")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


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
  python cli.py delete "/path/to/raw/file.pdf" --dry-run
  python cli.py doctor
  python cli.py evidence-packet "claim_id"
""",
    )

    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Available wiki operations"
    )

    subparsers.add_parser(
        "sync",
        help="[INGEST] Generates MCP ingestion instructions for Native Subagents.",
    )
    ingest_tasks_parser = subparsers.add_parser(
        "ingest-tasks", help="[INGEST] List or expire subagent ingest tasks."
    )
    ingest_tasks_parser.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=20,
        help="Maximum number of jobs to list.",
    )
    ingest_tasks_parser.add_argument(
        "--awaiting-only",
        action="store_true",
        help="Hide queued jobs and show only awaiting-subagent jobs.",
    )
    ingest_tasks_parser.add_argument(
        "--expire-stale",
        action="store_true",
        help="Expire stale awaiting-subagent jobs instead of listing.",
    )
    ingest_tasks_parser.add_argument(
        "--claim",
        action="store_true",
        help="Lease awaiting task packets to this host runtime.",
    )
    ingest_tasks_parser.add_argument(
        "--repair-debt",
        action="store_true",
        help="Classify and recover abandoned ingest jobs.",
    )
    ingest_tasks_parser.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help="Preview old unreferenced ingest task packets.",
    )
    ingest_tasks_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the selected repair or cleanup operation.",
    )
    ingest_tasks_parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=86400,
        help="Age threshold for --expire-stale.",
    )
    ingest_tasks_parser.add_argument(
        "--min-age-seconds",
        type=_nonnegative_int,
        default=86400,
        help="Minimum age for --cleanup-orphans.",
    )
    ingest_tasks_parser.add_argument(
        "--lease-seconds", type=int, default=3600, help="Lease duration for --claim."
    )

    lint_parser = subparsers.add_parser(
        "lint", help="[LINT] Run self-healing audit on the Wiki nodes."
    )
    lint_parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="Automatically fix issues such as decaying notes.",
    )

    search_parser = subparsers.add_parser(
        "search", help="[SEARCH] CJK-aware search with graph expansion."
    )
    search_parser.add_argument("query", help="Semantic query string.")
    search_parser.add_argument(
        "--top_k", type=int, default=5, help="Number of results (default: 5)."
    )
    search_parser.add_argument(
        "--domain", type=str, default=None, help="Filter by domain namespace."
    )
    search_parser.add_argument(
        "--cluster", type=str, default=None, help="Filter by topic cluster."
    )
    search_parser.add_argument(
        "--include-history",
        action="store_true",
        help="Bypass temporal invalidation to search deprecated facts.",
    )
    search_parser.add_argument(
        "--mode",
        choices=["page", "memory", "claim"],
        default="page",
        help="Search page index, operational memory, or fact claims.",
    )

    query_parser = subparsers.add_parser(
        "query", help="[QUERY] Deep reasoning with budget-controlled context."
    )
    query_parser.add_argument("query_str", help="The topic or command for reasoning.")
    query_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Output Markdown to stdout only without persisting to disk.",
    )

    subparsers.add_parser(
        "graph",
        help="[GRAPH] Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.",
    )
    timeline_rebuild_parser = subparsers.add_parser(
        "timeline-rebuild",
        help="[TIMELINE] Rebuild timeline_events from timeline-event claims.",
    )
    timeline_rebuild_parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the rebuilt projection. Defaults to dry-run.",
    )
    timeline_rebuild_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of claims to project.",
    )

    projection_report_parser = subparsers.add_parser(
        "projection-report", help="[MAINTENANCE] Report Wiki / canonical / index drift."
    )
    projection_report_parser.add_argument(
        "--limit", type=int, default=20, help="Sample size per drift bucket."
    )

    canonical_backfill_parser = subparsers.add_parser(
        "canonical-backfill",
        help="[MAINTENANCE] Backfill missing canonical rows from Wiki pages.",
    )
    canonical_backfill_parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the backfill. Defaults to dry-run.",
    )
    canonical_backfill_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of missing pages to process.",
    )

    foundation_backfill_parser = subparsers.add_parser(
        "evidence-foundation-backfill",
        help="[MAINTENANCE] Backfill missing evidence-foundation metadata without replacing canonical content.",
    )
    foundation_backfill_parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the backfill. Defaults to dry-run.",
    )
    foundation_backfill_parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum page revisions per run; zero means all.",
    )
    foundation_backfill_parser.add_argument(
        "--batch-size", type=int, default=100, help="Atomic page count per transaction."
    )
    foundation_backfill_parser.add_argument(
        "--backup-reference",
        default="",
        help="Existing verified SQLite backup to reuse.",
    )

    index_rebuild_parser = subparsers.add_parser(
        "projection-rebuild-index",
        help="[MAINTENANCE] Rebuild index projection from canonical SQLite.",
    )
    index_rebuild_parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist rebuilt index projection. Defaults to dry-run.",
    )

    embedding_backfill_parser = subparsers.add_parser(
        "embedding-backfill",
        help="[MAINTENANCE] Backfill missing vector embeddings under rate limits.",
    )
    embedding_backfill_parser.add_argument(
        "--apply", action="store_true", help="Persist embeddings. Defaults to dry-run."
    )
    embedding_backfill_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of nodes to embed.",
    )
    embedding_backfill_parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Re-embed nodes that already have vectors.",
    )

    wiki_restore_parser = subparsers.add_parser(
        "wiki-restore",
        help="[MAINTENANCE] Restore missing Wiki pages from canonical metadata.",
    )
    wiki_restore_parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist restored Markdown pages. Defaults to dry-run.",
    )
    wiki_restore_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of canonical-only pages to restore.",
    )

    memory_index_parser = subparsers.add_parser(
        "memory-search-index",
        help="[MAINTENANCE] Report or explicitly advance the optional operational-memory FTS index.",
    )
    memory_index_parser.add_argument(
        "--apply",
        action="store_true",
        help="Advance one bounded index batch. Defaults to read-only status.",
    )
    memory_index_parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Maximum documents to synchronize when --apply is used (max: 10000).",
    )

    memory_cleanup_parser = subparsers.add_parser(
        "memory-cleanup",
        help="[MAINTENANCE] Preview or archive generated template artifacts in operational memory.",
    )
    memory_cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Archive detected artifacts. Defaults to dry-run.",
    )
    memory_cleanup_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum rows to archive; zero means all candidates.",
    )
    history_retention_parser = subparsers.add_parser(
        "history-retention",
        help="[MAINTENANCE] Preview or explicitly apply bounded history retention.",
    )
    history_retention_parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the selected history batch. Defaults to read-only preview.",
    )
    history_retention_parser.add_argument(
        "--ttl-days",
        type=_positive_int,
        default=30,
        help="Minimum history age in days.",
    )
    history_retention_parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=500,
        help="Maximum rows selected globally (max: 500).",
    )
    history_retention_parser.add_argument(
        "--max-delete-bytes",
        type=_positive_int,
        default=128 * 1024 * 1024,
        help="Maximum logical bytes selected globally.",
    )
    history_retention_parser.add_argument(
        "--keep-change-sets",
        type=_nonnegative_int,
        default=1000,
        help="Always retain at least this many newest change sets.",
    )
    history_retention_parser.add_argument(
        "--keep-terminal-jobs",
        type=_nonnegative_int,
        default=1000,
        help="Always retain at least this many newest terminal jobs.",
    )
    history_retention_parser.add_argument(
        "--keep-terminal-outbox",
        type=_nonnegative_int,
        default=1000,
        help="Always retain at least this many newest terminal outbox rows.",
    )
    history_retention_parser.add_argument(
        "--keep-versions-per-family",
        type=_positive_int,
        default=2,
        help="Always retain the newest versions in each claim/evidence family.",
    )
    history_retention_parser.add_argument(
        "--claim-version-cursor",
        default="",
        help="Receipt-issued claim-version keyset cursor for the next batch.",
    )
    history_retention_parser.add_argument(
        "--evidence-version-cursor",
        default="",
        help="Receipt-issued evidence-version keyset cursor for the next batch.",
    )
    history_retention_parser.add_argument(
        "--version-cursor-receipt",
        default="",
        help="Fingerprint of the successful receipt that issued both cursors.",
    )
    history_retention_parser.add_argument(
        "--plan-as-of",
        default="",
        help="Fixed timezone-aware preview instant required for apply.",
    )
    history_retention_parser.add_argument(
        "--confirm-fingerprint",
        default="",
        help="Exact sha256 fingerprint returned by the matching preview.",
    )
    change_set_compaction_parser = subparsers.add_parser(
        "change-set-compaction",
        help="[MAINTENANCE] Preview or apply bounded legacy change-set compaction.",
    )
    change_set_compaction_parser.add_argument("--apply", action="store_true")
    change_set_compaction_parser.add_argument(
        "--max-rows", type=_positive_int, default=100
    )
    change_set_compaction_parser.add_argument(
        "--max-input-bytes", type=_positive_int, default=64 * 1024 * 1024
    )
    change_set_compaction_parser.add_argument(
        "--cursor",
        default="",
        help="safe_next_cursor returned by the prior successful apply.",
    )
    change_set_compaction_parser.add_argument(
        "--confirm-fingerprint", default=""
    )
    backup_retention_parser = subparsers.add_parser(
        "backup-retention",
        help="[MAINTENANCE] Preview or explicitly apply backup retention.",
    )
    backup_retention_parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete only candidates matching --confirm-fingerprint. Defaults to preview.",
    )
    backup_retention_parser.add_argument(
        "--keep-latest",
        type=_positive_int,
        default=5,
        help="Retain this many newest backups, plus the newest restorable backup.",
    )
    backup_retention_parser.add_argument(
        "--min-age-days",
        type=_positive_int,
        default=30,
        help="Minimum complete-backup age in days.",
    )
    backup_retention_parser.add_argument(
        "--stage-ttl-hours",
        type=_positive_int,
        default=24,
        help="Minimum private staging age in hours.",
    )
    backup_retention_parser.add_argument(
        "--confirm-fingerprint",
        default="",
        help="Exact fingerprint from a current preview; required with --apply.",
    )
    schema_migrate_parser = subparsers.add_parser(
        "schema-migrate",
        help=(
            "[MAINTENANCE] Preview or apply the controlled SQLite "
            "v4/v5/v6 to v7 migration."
        ),
    )
    schema_migrate_action = schema_migrate_parser.add_mutually_exclusive_group()
    schema_migrate_action.add_argument(
        "--apply",
        action="store_true",
        help="Apply the exact previewed plan. Defaults to a physically read-only preview.",
    )
    schema_migrate_action.add_argument(
        "--checkpoint-wal",
        action="store_true",
        help="Checkpoint and truncate the exact previewed WAL without running migration DDL.",
    )
    schema_migrate_parser.add_argument(
        "--confirm-fingerprint",
        default="",
        help="Exact sha256 fingerprint returned by the matching preview.",
    )
    schema_migrate_parser.add_argument(
        "--confirm-no-writers",
        action="store_true",
        help="Assert that MCP, watchdog, and every other database writer are stopped.",
    )
    topology_cleanup_parser = subparsers.add_parser(
        "topology-queue-cleanup",
        help="[MAINTENANCE] Preview or retire legacy indexer-generated community naming items.",
    )
    topology_cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Retire matching legacy items. Defaults to dry-run.",
    )
    orphan_source_parser = subparsers.add_parser(
        "orphan-source-classify",
        help="[MAINTENANCE] Classify unreferenced canonical sources and register non-destructive debt.",
    )
    orphan_source_parser.add_argument(
        "--apply",
        action="store_true",
        help="Register classified debt. Defaults to dry-run.",
    )

    review_parser = subparsers.add_parser(
        "review",
        help="[REVIEW] Inspect and resolve the unified legacy/governance review surface.",
    )
    review_parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "resolve", "ground"],
        help="Action: 'list' (default), 'resolve', or 'ground'.",
    )
    review_parser.add_argument(
        "index",
        nargs="?",
        default="-1",
        help="Index or item_id of review item to resolve (for 'resolve' action).",
    )
    review_parser.add_argument(
        "--resolution",
        type=str,
        default="skip",
        help="Resolution type: 'skip', 'create', 'merge', 'acknowledge' (default: skip).",
    )

    subparsers.add_parser(
        "audit-graph",
        help="[AUDIT-GRAPH] Synthesize graph topology insights into the unified review surface.",
    )
    subparsers.add_parser(
        "doctor", help="[DOCTOR] Validate runtime dependencies and filesystem layout."
    )
    readiness_parser = subparsers.add_parser(
        "readiness",
        help="[READINESS] Report semantic readiness separately from runtime health.",
    )
    readiness_parser.add_argument(
        "--decision-id",
        default=None,
        help="Evaluate only a verified CriticalDecisionRegistry scope.",
    )

    research_parser = subparsers.add_parser(
        "research",
        help="[RESEARCH] Autonomously scan graph gaps and governance queue to formulate web research directives.",
    )
    research_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the research queries without the SYSTEM DIRECTIVE execution hook.",
    )

    debt_parser = subparsers.add_parser(
        "debt", help="[DEBT] Show governance debt metrics."
    )
    debt_parser.add_argument(
        "--top", type=int, default=20, help="Top debt window size."
    )

    trace_parser = subparsers.add_parser(
        "trace", help="[TRACE] Show provenance trace for a query or identifier."
    )
    trace_parser.add_argument("query_or_id", help="Query text or object identifier.")

    evidence_parser = subparsers.add_parser(
        "evidence-packet",
        help="[EVIDENCE] Export a read-only CBSS EvidencePacket for one canonical claim.",
    )
    evidence_parser.add_argument("claim_id", help="Canonical claim identifier.")
    evidence_parser.add_argument(
        "--include-text",
        action="store_true",
        help="Include bounded evidence text. The default exports hashes and locators only.",
    )
    evidence_parser.add_argument(
        "--max-text-chars",
        type=int,
        default=2000,
        help="Per-evidence text limit when --include-text is used (default: 2000, max: 10000).",
    )
    evidence_parser.add_argument(
        "--actor-id",
        default="",
        help="Required operator identifier when --include-text is used.",
    )
    evidence_parser.add_argument(
        "--purpose",
        default="",
        help="Required bounded export purpose when --include-text is used.",
    )

    merge_parser = subparsers.add_parser(
        "merge-suggestions", help="[MERGE] Detect and enqueue candidate entity merges."
    )
    merge_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of merge candidates to surface.",
    )
    merge_parser.add_argument(
        "--preview",
        action="store_true",
        help="Do not enqueue governance items; only preview candidates.",
    )

    delete_parser = subparsers.add_parser(
        "delete",
        help="[DELETE] Cascade-delete a raw source and all related wiki pages.",
    )
    delete_parser.add_argument(
        "raw_path", help="Path to the raw source file to remove."
    )
    delete_mode = delete_parser.add_mutually_exclusive_group()
    delete_mode.add_argument(
        "--apply",
        action="store_true",
        help="Execute the deletion. Defaults to dry-run.",
    )
    delete_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without making changes.",
    )

    gc_parser = subparsers.add_parser(
        "gc", help="[GC] Automatically prune isolated/orphan entities."
    )
    gc_parser.add_argument(
        "--days",
        type=_positive_int,
        default=30,
        help="Prune entities older than this many days (default: 30; minimum: 1).",
    )
    gc_parser.add_argument(
        "--confirm-orphans",
        default=None,
        help="Delete only the orphan candidate set matching this dry-run fingerprint. Requires --apply.",
    )
    gc_mode = gc_parser.add_mutually_exclusive_group()
    gc_mode.add_argument(
        "--apply",
        action="store_true",
        help="Execute garbage collection. Defaults to dry-run.",
    )
    gc_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without making changes.",
    )
    return parser


def main() -> int:
    _configure_stdout()
    _load_env()
    parser = build_parser()
    args = parser.parse_args()

    lease = None
    lease_entered = False
    lease_failure = None
    try:
        policy = _cli_heavy_task_policy(args)
        if policy is not None:
            from vector_lake.heavy_task_gate import heavy_task

            task_class, warn_after_seconds = policy
            lease = heavy_task(
                task_class,
                args.command,
                origin="cli",
                wait_timeout_seconds=_cli_heavy_task_wait_seconds(),
                warn_after_seconds=warn_after_seconds,
            )
            lease.__enter__()
            lease_entered = True
        if args.command == "sync":
            print(tools.sync_vector_lake())
        elif args.command == "ingest-tasks":
            if getattr(args, "repair_debt", False):
                print(
                    tools.reconcile_ingest_job_debt(
                        dry_run=not getattr(args, "apply", False),
                        limit=getattr(args, "limit", 20),
                    )
                )
            elif getattr(args, "cleanup_orphans", False):
                print(
                    tools.reconcile_orphan_ingest_task_packets(
                        dry_run=not getattr(args, "apply", False),
                        min_age_seconds=getattr(args, "min_age_seconds", 86400),
                        limit=getattr(args, "limit", 20),
                    )
                )
            elif getattr(args, "expire_stale", False):
                print(
                    tools.expire_ingest_tasks(getattr(args, "max_age_seconds", 86400))
                )
            elif getattr(args, "claim", False):
                print(
                    tools.claim_ingest_tasks(
                        limit=getattr(args, "limit", 20),
                        lease_seconds=getattr(args, "lease_seconds", 3600),
                    )
                )
            else:
                print(
                    tools.list_ingest_tasks(
                        limit=getattr(args, "limit", 20),
                        include_queued=not getattr(args, "awaiting_only", False),
                    )
                )
        elif args.command == "search":
            print(
                tools.search_vector_lake(
                    args.query,
                    args.top_k,
                    domain=getattr(args, "domain", None),
                    cluster=getattr(args, "cluster", None),
                    include_history=getattr(args, "include_history", False),
                    mode=getattr(args, "mode", "page"),
                )
            )
        elif args.command == "lint":
            print(tools.lint_vector_lake(getattr(args, "auto_fix", False)))
        elif args.command == "query":
            print(
                tools.prepare_query_context(
                    args.query_str, getattr(args, "dry_run", False)
                )
            )
        elif args.command == "graph":
            print(tools.visualize_vector_lake())
        elif args.command == "timeline-rebuild":
            print(
                tools.rebuild_timeline_events_from_claims(
                    dry_run=not getattr(args, "apply", False),
                    limit=getattr(args, "limit", None),
                )
            )
        elif args.command == "projection-report":
            print(tools.projection_diff_report(limit=getattr(args, "limit", 20)))
        elif args.command == "canonical-backfill":
            print(
                tools.canonical_backfill_missing_wiki(
                    dry_run=not getattr(args, "apply", False),
                    limit=getattr(args, "limit", 50),
                )
            )
        elif args.command == "evidence-foundation-backfill":
            print(
                tools.evidence_foundation_backfill(
                    dry_run=not getattr(args, "apply", False),
                    limit=getattr(args, "limit", 500),
                    batch_size=getattr(args, "batch_size", 100),
                    backup_reference=getattr(args, "backup_reference", ""),
                )
            )
        elif args.command == "projection-rebuild-index":
            print(
                tools.rebuild_index_projection(
                    dry_run=not getattr(args, "apply", False)
                )
            )
        elif args.command == "embedding-backfill":
            print(
                tools.embedding_backfill_projection(
                    dry_run=not getattr(args, "apply", False),
                    limit=getattr(args, "limit", None),
                    include_existing=getattr(args, "include_existing", False),
                )
            )
        elif args.command == "wiki-restore":
            print(
                tools.restore_missing_wiki_from_canonical(
                    dry_run=not getattr(args, "apply", False),
                    limit=getattr(args, "limit", 10),
                )
            )
        elif args.command == "memory-search-index":
            print(
                tools.operational_memory_search_index_maintenance(
                    dry_run=not getattr(args, "apply", False),
                    batch_size=getattr(args, "batch_size", 256),
                )
            )
        elif args.command == "memory-cleanup":
            print(
                tools.cleanup_operational_memory(
                    dry_run=not getattr(args, "apply", False),
                    limit=getattr(args, "limit", 0),
                )
            )
        elif args.command == "history-retention":
            print(
                tools.history_retention_maintenance(
                    dry_run=not getattr(args, "apply", False),
                    ttl_days=getattr(args, "ttl_days", 30),
                    batch_size=getattr(args, "batch_size", 500),
                    max_delete_bytes=getattr(
                        args, "max_delete_bytes", 128 * 1024 * 1024
                    ),
                    keep_change_sets=getattr(args, "keep_change_sets", 1000),
                    keep_terminal_jobs=getattr(args, "keep_terminal_jobs", 1000),
                    keep_terminal_outbox=getattr(args, "keep_terminal_outbox", 1000),
                    keep_versions_per_family=getattr(
                        args, "keep_versions_per_family", 2
                    ),
                    claim_version_cursor=getattr(
                        args, "claim_version_cursor", ""
                    ),
                    evidence_version_cursor=getattr(
                        args, "evidence_version_cursor", ""
                    ),
                    version_cursor_receipt=getattr(
                        args, "version_cursor_receipt", ""
                    ),
                    plan_as_of=getattr(args, "plan_as_of", ""),
                    confirmation=getattr(args, "confirm_fingerprint", ""),
                )
            )
        elif args.command == "change-set-compaction":
            print(
                tools.compact_change_set_history(
                    dry_run=not getattr(args, "apply", False),
                    max_rows=getattr(args, "max_rows", 100),
                    max_input_bytes=getattr(
                        args, "max_input_bytes", 64 * 1024 * 1024
                    ),
                    cursor=getattr(args, "cursor", ""),
                    confirmation=getattr(args, "confirm_fingerprint", ""),
                )
            )
        elif args.command == "backup-retention":
            print(
                tools.backup_retention_maintenance(
                    dry_run=not getattr(args, "apply", False),
                    keep_latest=getattr(args, "keep_latest", 5),
                    min_age_days=getattr(args, "min_age_days", 30),
                    stage_ttl_hours=getattr(args, "stage_ttl_hours", 24),
                    confirmation=getattr(args, "confirm_fingerprint", ""),
                )
            )
        elif args.command == "schema-migrate":
            from vector_lake import db_store

            print(
                json.dumps(
                    db_store.schema_migration_maintenance(
                        apply=getattr(args, "apply", False),
                        checkpoint_wal=getattr(args, "checkpoint_wal", False),
                        confirmation=getattr(args, "confirm_fingerprint", ""),
                        confirm_no_writers=getattr(
                            args, "confirm_no_writers", False
                        ),
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "topology-queue-cleanup":
            print(
                tools.retire_legacy_topology_queue(
                    dry_run=not getattr(args, "apply", False)
                )
            )
        elif args.command == "orphan-source-classify":
            print(
                json.dumps(
                    tools.classify_orphan_source_debt(
                        dry_run=not getattr(args, "apply", False)
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "review":
            print(
                tools.review_vector_lake(
                    action=args.action,
                    index=args.index,
                    resolution=getattr(args, "resolution", "skip"),
                )
            )
        elif args.command == "audit-graph":
            print(tools.audit_graph())
        elif args.command == "doctor":
            print(tools.doctor_vector_lake())
        elif args.command == "readiness":
            print(
                tools.semantic_readiness_vector_lake(getattr(args, "decision_id", None))
            )
        elif args.command == "research":
            print(tools.research_vector_lake(getattr(args, "dry_run", False)))
        elif args.command == "debt":
            print(tools.debt_vector_lake(getattr(args, "top", 20)))
        elif args.command == "trace":
            print(tools.trace_vector_lake(args.query_or_id))
        elif args.command == "evidence-packet":
            print(
                tools.export_evidence_packet(
                    args.claim_id,
                    include_evidence_text=getattr(args, "include_text", False),
                    max_evidence_text_chars=getattr(args, "max_text_chars", 2000),
                    actor_id=getattr(args, "actor_id", ""),
                    purpose=getattr(args, "purpose", ""),
                )
            )
        elif args.command == "merge-suggestions":
            print(
                tools.merge_suggestions_vector_lake(
                    limit=getattr(args, "limit", 20),
                    enqueue=not getattr(args, "preview", False),
                )
            )
        elif args.command == "delete":
            print(
                tools.delete_source(
                    args.raw_path, dry_run=not getattr(args, "apply", False)
                )
            )
        elif args.command == "gc":
            gc_kwargs = {
                "days": getattr(args, "days", 30),
                "dry_run": not getattr(args, "apply", False),
            }
            orphan_confirmation = getattr(args, "confirm_orphans", None)
            if orphan_confirmation is not None:
                gc_kwargs["orphan_confirmation"] = orphan_confirmation
            print(tools.gc_vector_lake(**gc_kwargs))
    except Exception as exc:
        lease_failure = exc
        from vector_lake.heavy_task_gate import HeavyTaskBusy

        if isinstance(exc, HeavyTaskBusy):
            print(
                json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
            )
            return 75
        print(f"Error executing command '{args.command}': {exc}", file=sys.stderr)
        return 1
    finally:
        if lease_entered and lease is not None:
            lease.__exit__(
                type(lease_failure) if lease_failure is not None else None,
                lease_failure,
                (
                    lease_failure.__traceback__
                    if lease_failure is not None
                    else None
                ),
            )
    return 0
