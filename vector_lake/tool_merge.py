from vector_lake import governance_store


def merge_suggestions_vector_lake(limit: int = 20, enqueue: bool = True) -> str:
    result = governance_store.create_merge_suggestions(limit=limit, enqueue=enqueue)
    suggestions = result.get("suggestions", [])
    lines = [
        "=== Merge Suggestions ===",
        f"created: {result.get('created', 0)}",
        f"candidate_pool_size: {result.get('candidate_pool_size', len(suggestions))}",
        f"actionable_pool_size: {result.get('actionable_pool_size', len(suggestions))}",
        f"decision_counts: {result.get('decision_counts', {})}",
        f"selected_decision_counts: {result.get('selected_decision_counts', {})}",
        f"returned_count: {result.get('returned_count', len(suggestions))}",
        f"eligible_count: {result.get('eligible_count', 0)}",
        f"skipped_count: {result.get('skipped_count', 0)}",
    ]
    for suggestion in suggestions[:limit]:
        lines.append(
            f"- {suggestion['left_name']} <> {suggestion['right_name']}"
            f" | decision={suggestion.get('decision', 'review')}"
            f" | evidence_score={suggestion.get('evidence_score', suggestion.get('score', 0))}"
            f" | component={suggestion.get('component_id') or '-'}"
            f" | preflight={suggestion.get('preflight_state', 'not_run')}"
            f" | reasons={', '.join(suggestion['reasons'])}"
        )
    return "\n".join(lines)

