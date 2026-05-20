from mcp.server.fastmcp import FastMCP
from vector_lake import tools
from vector_lake.tool_timeline import search_timeline_events

mcp = FastMCP("vector-lake")

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
def search_vector_lake(query: str, top_k: int = 5, mode: str = "page") -> str:
    """Search the Vector Lake index.
    
    Args:
        query: The semantic query string.
        top_k: Number of results to return.
        mode: Search mode, can be 'page', 'memory', or 'claim'.
    """
    return tools.search_vector_lake(query, top_k, mode=mode)

@mcp.tool()
def sync_vector_lake() -> str:
    """Trigger a 2-step CoT sync of all raw sources into the Wiki nodes."""
    return tools.sync_vector_lake()

@mcp.tool()
def lint_vector_lake(auto_fix: bool = False) -> str:
    """Run self-healing audit on the Wiki nodes.
    
    Args:
        auto_fix: Automatically fix issues such as decaying notes.
    """
    return tools.lint_vector_lake(auto_fix=auto_fix)

@mcp.tool()
def query_logic_lake(query_str: str) -> str:
    """Deep reasoning with budget-controlled context.
    
    Args:
        query_str: The topic or command for reasoning.
    """
    return tools.query_logic_lake(query_str)

@mcp.tool()
def review_governance_list() -> str:
    """List pending items in the governance review queue (contradictions, gaps, merges)."""
    return tools.review_vector_lake(action="list")

@mcp.tool()
def resolve_governance_item(item_id: str, resolution: str, change_manifest_json: str = None) -> str:
    """Resolve a governance item.

    Args:
        item_id: The ID or index of the item.
        resolution: Resolution action: 'skip', 'create', 'merge', 'acknowledge'.
        change_manifest_json: Optional JSON string of the expected outcome manifest to ensure safety (e.g. '{"allow_cycles": false}').
    """
    import json
    manifest = None
    if change_manifest_json:
        try:
            manifest = json.loads(change_manifest_json)
        except Exception:
            pass
    return tools.review_vector_lake(action="resolve", index=item_id, resolution=resolution, change_manifest=manifest)
@mcp.tool()
def trigger_autonomous_research(dry_run: bool = False) -> str:
    """Autonomously scan graph gaps and governance queue to formulate web research directives.
    
    Args:
        dry_run: If true, just lists the topics without emitting a SYSTEM DIRECTIVE.
    """
    return tools.research_vector_lake(dry_run=dry_run)

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
def delete_source(raw_path: str, dry_run: bool = False) -> str:
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
def gc_vector_lake(days: int = 30, dry_run: bool = False) -> str:
    """Automatically prune isolated or orphaned entities.
    
    Args:
        days: Prune entities older than this many days (default: 30).
        dry_run: Preview what would be deleted without making changes.
    """
    return tools.gc_vector_lake(days=days, dry_run=dry_run)

@mcp.tool()
def visualize_vector_lake() -> str:
    """Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard."""
    return tools.visualize_vector_lake()

if __name__ == "__main__":
    mcp.run()

