from vector_lake import governance_metrics


def debt_vector_lake(top: int = 20) -> str:
    # ensure_canonical_store_populated() removed in SQLite refactor
    #
    # The candidate list is computed once and handed to the metric: this function printed
    # the list while ``compute_debt_metrics`` derived its length, so the full entity load
    # and the pairwise scoring ran twice per dashboard.
    merge_candidates = governance_metrics.find_merge_candidates(limit=top)
    metrics = governance_metrics.compute_debt_metrics(merge_candidates=merge_candidates)
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

