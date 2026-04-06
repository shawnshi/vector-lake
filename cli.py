import os
import sys
import argparse

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
        description="Vector Lake LLM-Wiki CLI Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage Examples:
  python cli.py sync
  python cli.py search "MSL 架构" --top_k 5
"""
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available wiki operations")

    subparsers.add_parser("sync", help="[🟢 Ingest] Trigger a sync of all raw sources into the Wiki via Native Agent.")
    
    subparsers.add_parser("lint", help="[🟡 Lint] Run self-healing audit on the Wiki nodes.")

    search_parser = subparsers.add_parser("search", help="[🔍 Search] Semantic search over the Wiki pages via ChromaDB.")
    search_parser.add_argument("query", help="Semantic query string.")
    search_parser.add_argument("--top_k", type=int, default=5, help="Number of results (default: 5).")

    query_parser = subparsers.add_parser("query", help="[🧠 Query] Trigger deep reasoning and create a new Wiki node.")
    query_parser.add_argument("query_str", help="The topic or command for reasoning.")

    seren_parser = subparsers.add_parser("serendipity", help="[✨ Serendipity] Trigger background random synthesis collision.")

    graph_parser = subparsers.add_parser("graph", help="[🌐 Graph] Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.")

    args = parser.parse_args()

    try:
        if args.command == "sync":
            print(tools.sync_vector_lake())
        elif args.command == "search":
            print(tools.search_vector_lake(args.query, args.top_k))
        elif args.command == "lint":
            print(tools.lint_vector_lake())
        elif args.command == "query":
            print(tools.query_logic_lake(args.query_str))
        elif args.command == "serendipity":
            print(tools.trigger_serendipity_collision())
        elif args.command == "graph":
            print(tools.visualize_vector_lake())
    except Exception as e:
        print(f"Error executing command '{args.command}': {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
