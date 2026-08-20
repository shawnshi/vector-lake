from vector_lake import governance_metrics


def debt_vector_lake(top: int = 20) -> str:
    snapshot = governance_metrics.read_only_governance_debt_snapshot(limit=top)
    metrics = snapshot["metrics"]
    merge_report = snapshot["merge_candidate_report"]
    merge_candidates = merge_report["suggestions"]
    lines = ["=== Vector Lake Debt Dashboard ==="]
    lines.append(
        "availability: " + ("available" if snapshot["available"] else "unavailable")
    )
    if not snapshot["available"]:
        lines.append(f"unavailable_reason: {snapshot['unavailable_reason']}")
    for key, value in metrics.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("Top merge candidates:")
    if not merge_report["available"]:
        lines.append(
            f"- unavailable: {merge_report['unavailable_reason']}"
        )
    elif not merge_candidates:
        lines.append("- none")
    else:
        for candidate in merge_candidates[:top]:
            lines.append(
                f"- {candidate['left_name']} <> {candidate['right_name']} | score={candidate['score']} | reasons={', '.join(candidate['reasons'])}"
            )
    lines.append(f"top: {top}")
    return "\n".join(lines)

