import argparse
import io
import os
import sys

try:
    import dotenv
except ImportError:
    dotenv = None

from vector_lake import tools


def _configure_stdout():
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _load_env():
    if dotenv is None:
        return
    env_path = os.path.join(os.path.expanduser("~"), ".gemini", ".env")
    if os.path.exists(env_path):
        dotenv.load_dotenv(env_path)


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
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Available wiki operations")

    subparsers.add_parser("sync", help="[INGEST] Trigger a 2-step CoT sync of all raw sources into the Wiki.")

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

    review_parser = subparsers.add_parser("review", help="[REVIEW] Inspect and resolve the unified legacy/governance review surface.")
    review_parser.add_argument("action", nargs="?", default="list", choices=["list", "resolve", "ground"], help="Action: 'list' (default), 'resolve', or 'ground'.")
    review_parser.add_argument("index", nargs="?", default="-1", help="Index or item_id of review item to resolve (for 'resolve' action).")
    review_parser.add_argument("--resolution", type=str, default="skip", help="Resolution type: 'skip', 'create', 'merge', 'acknowledge' (default: skip).")

    subparsers.add_parser("audit-graph", help="[AUDIT-GRAPH] Synthesize graph topology insights into the unified review surface.")
    subparsers.add_parser("doctor", help="[DOCTOR] Validate runtime dependencies and filesystem layout.")


    debt_parser = subparsers.add_parser("debt", help="[DEBT] Show governance debt metrics.")
    debt_parser.add_argument("--top", type=int, default=20, help="Top debt window size.")

    trace_parser = subparsers.add_parser("trace", help="[TRACE] Show provenance trace for a query or identifier.")
    trace_parser.add_argument("query_or_id", help="Query text or object identifier.")

    merge_parser = subparsers.add_parser("merge-suggestions", help="[MERGE] Detect and enqueue candidate entity merges.")
    merge_parser.add_argument("--limit", type=int, default=20, help="Maximum number of merge candidates to surface.")
    merge_parser.add_argument("--preview", action="store_true", help="Do not enqueue governance items; only preview candidates.")

    delete_parser = subparsers.add_parser("delete", help="[DELETE] Cascade-delete a raw source and all related wiki pages.")
    delete_parser.add_argument("raw_path", help="Path to the raw source file to remove.")
    delete_parser.add_argument("--dry-run", action="store_true", help="Preview what would be deleted without making changes.")

    gc_parser = subparsers.add_parser("gc", help="[GC] Automatically prune isolated/orphan entities.")
    gc_parser.add_argument("--days", type=int, default=30, help="Prune entities older than this many days (default: 30).")
    gc_parser.add_argument("--dry-run", action="store_true", help="Preview what would be deleted without making changes.")
    return parser


def main() -> int:
    _configure_stdout()
    _load_env()
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "sync":
            print(tools.sync_vector_lake())
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
            print(tools.query_logic_lake(args.query_str, getattr(args, "dry_run", False)))
        elif args.command == "graph":
            print(tools.visualize_vector_lake())
        elif args.command == "review":
            print(tools.review_vector_lake(action=args.action, index=args.index, resolution=getattr(args, "resolution", "skip")))
        elif args.command == "audit-graph":
            print(tools.audit_graph())
        elif args.command == "doctor":
            print(tools.doctor_vector_lake())
        elif args.command == "debt":
            print(tools.debt_vector_lake(getattr(args, "top", 20)))
        elif args.command == "trace":
            print(tools.trace_vector_lake(args.query_or_id))
        elif args.command == "merge-suggestions":
            print(tools.merge_suggestions_vector_lake(limit=getattr(args, "limit", 20), enqueue=not getattr(args, "preview", False)))
        elif args.command == "delete":
            print(tools.delete_source(args.raw_path, dry_run=getattr(args, "dry_run", False)))
        elif args.command == "gc":
            print(tools.gc_vector_lake(days=getattr(args, "days", 30), dry_run=getattr(args, "dry_run", False)))
    except Exception as exc:
        print(f"Error executing command '{args.command}': {exc}", file=sys.stderr)
        return 1
    return 0
