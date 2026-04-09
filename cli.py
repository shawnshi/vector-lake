import os
import sys
import argparse
import io

# Force UTF-8 stdout on Windows to prevent cp1252 encoding errors
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    import dotenv
    env_path = os.path.join(os.path.expanduser("~"), ".gemini", ".env")
    if os.path.exists(env_path):
        dotenv.load_dotenv(env_path)
except ImportError:
    pass

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import tools

def main():
    parser = argparse.ArgumentParser(
        description="Vector Lake LLM-Wiki CLI Gateway (V7.1 — 2-Step CoT)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage Examples:
  python cli.py sync
  python cli.py search "MSL" --top_k 5
  python cli.py review
  python cli.py review resolve 0
  python cli.py delete "/path/to/raw/file.pdf" --dry-run
"""
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

    query_parser = subparsers.add_parser("query", help="[QUERY] Deep reasoning with budget-controlled context.")
    query_parser.add_argument("query_str", help="The topic or command for reasoning.")
    query_parser.add_argument("--dry-run", action="store_true", help="Output Markdown to stdout only without persisting to disk.")

    seren_parser = subparsers.add_parser("serendipity", help="[SERENDIPITY] Trigger background random synthesis collision.")

    graph_parser = subparsers.add_parser("graph", help="[GRAPH] Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.")

    review_parser = subparsers.add_parser("review", help="[REVIEW] Inspect and resolve the async review queue.")
    review_parser.add_argument("action", nargs="?", default="list", choices=["list", "resolve"],
                               help="Action: 'list' (default) or 'resolve'.")
    review_parser.add_argument("index", nargs="?", type=int, default=-1,
                               help="Index of review item to resolve (for 'resolve' action).")
    review_parser.add_argument("--resolution", type=str, default="skip",
                               help="Resolution type: 'skip', 'create', 'acknowledge' (default: skip).")

    delete_parser = subparsers.add_parser("delete", help="[DELETE] Cascade-delete a raw source and all related wiki pages.")
    delete_parser.add_argument("raw_path", help="Path to the raw source file to remove.")
    delete_parser.add_argument("--dry-run", action="store_true",
                               help="Preview what would be deleted without making changes.")

    args = parser.parse_args()

    try:
        if args.command == "sync":
            print(tools.sync_vector_lake())
        elif args.command == "search":
            print(tools.search_vector_lake(
                args.query, 
                args.top_k, 
                domain=getattr(args, 'domain', None), 
                cluster=getattr(args, 'cluster', None), 
                include_history=getattr(args, 'include_history', False)
            ))
        elif args.command == "lint":
            print(tools.lint_vector_lake(getattr(args, 'auto_fix', False)))
        elif args.command == "query":
            print(tools.query_logic_lake(args.query_str, getattr(args, 'dry_run', False)))
        elif args.command == "serendipity":
            print(tools.trigger_serendipity_collision())
        elif args.command == "graph":
            print(tools.visualize_vector_lake())
        elif args.command == "review":
            print(tools.review_vector_lake(
                action=args.action,
                index=args.index,
                resolution=getattr(args, 'resolution', 'skip')
            ))
        elif args.command == "delete":
            print(tools.delete_source(
                args.raw_path,
                dry_run=getattr(args, 'dry_run', False)
            ))
    except Exception as e:
        print(f"Error executing command '{args.command}': {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
