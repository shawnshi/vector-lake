from vector_lake import governance_metrics
from vector_lake import governance_store


def debt_vector_lake(top: int = 20) -> str:
    governance_store.ensure_canonical_store_populated()
    metrics = governance_metrics.compute_debt_metrics()
    merge_candidates = governance_metrics.find_merge_candidates(limit=top)
    lines = ["=== Vector Lake Debt Dashboard ==="]
    for key, value in metrics.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("Top merge candidates:")
    if not merge_candidates:
        lines.append("- none")
    else:
        for candidate in merge_candidates[:top]:
            lines.append(
                f"- {candidate['left_name']} <> {candidate['right_name']} | score={candidate['score']} | reasons={', '.join(candidate['reasons'])}"
            )
    lines.append(f"top: {top}")
    return "\n".join(lines)

