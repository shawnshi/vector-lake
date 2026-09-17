from vector_lake import governance_store


def merge_suggestions_vector_lake(limit: int = 20, enqueue: bool = True) -> str:
    result = governance_store.create_merge_suggestions(limit=limit, enqueue=enqueue)
    suggestions = result.get("suggestions", [])
    lines = [
        "=== Merge Suggestions ===",
        f"created: {result.get('created', 0)}",
        f"total_candidates: {len(suggestions)}",
        f"skipped_hazardous: {result.get('skipped_hazardous', 0)}",
    ]
    for suggestion in suggestions[:limit]:
        hazards = suggestion.get("hazards") or []
        hazard_note = f" | HAZARDS: {'; '.join(hazards)}" if hazards else ""
        lines.append(
            f"- {suggestion['left_name']} <> {suggestion['right_name']} | score={suggestion['score']} | reasons={', '.join(suggestion['reasons'])}{hazard_note}"
        )
    return "\n".join(lines)

