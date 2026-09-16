from vector_lake import governance_store


def merge_suggestions_vector_lake(limit: int = 20, enqueue: bool = True) -> str:
    result = governance_store.create_merge_suggestions(limit=limit, enqueue=enqueue)
    suggestions = result.get("suggestions", [])
    lines = ["=== Merge Suggestions ===", f"created: {result.get('created', 0)}", f"total_candidates: {len(suggestions)}"]
    for suggestion in suggestions[:limit]:
        lines.append(
            f"- {suggestion['left_name']} <> {suggestion['right_name']} | score={suggestion['score']} | reasons={', '.join(suggestion['reasons'])}"
        )
    return "\n".join(lines)

