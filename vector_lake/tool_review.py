from vector_lake import governance_store
from vector_lake import review


def _truncate_text(text: str, limit: int = 120) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _summarize_values(values, limit: int = 5) -> str:
    cleaned = []
    seen = set()
    for value in (values or []):
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    preview = ", ".join(cleaned[:limit])
    return f"{preview} (+{len(cleaned) - limit} more)"


def _combined_pending_items() -> list[dict]:
    combined = []
    for item in review.get_pending():
        enriched = dict(item)
        enriched["queue_kind"] = "review"
        combined.append(enriched)
    for item in governance_store.pending_governance_items():
        enriched = dict(item)
        enriched["queue_kind"] = "governance"
        combined.append(enriched)
    combined.sort(key=lambda item: item.get("created_at") or item.get("created") or "")
    return combined


def _format_combined_report(items: list[dict]) -> str:
    if not items:
        return "[OK] No pending review items."

    type_icons = {
        "contradiction": "[!]",
        "duplicate": "[D]",
        "missing-page": "[?]",
        "suggestion": "[*]",
        "merge": "[M]",
        "publish-candidate": "[P]",
    }
    review_count = len([item for item in items if item.get("queue_kind") == "review"])
    governance_count = len(items) - review_count
    lines = [
        f"[REVIEW] {len(items)} Pending Items",
        f"legacy_review: {review_count} | governance: {governance_count}",
        "",
    ]
    for index, item in enumerate(items):
        icon = type_icons.get(item.get("type"), "[*]")
        lines.append(f"  [{index}] {icon} **{item.get('title', 'Untitled')}** ({item.get('type', 'unknown')})")
        lines.append(f"      ID: {item.get('item_id', 'unknown')} | Queue: {item.get('queue_kind', 'unknown')}")
        lines.append(f"      Source: {item.get('source', 'unknown')}")
        if item.get("description"):
            lines.append(f"      {_truncate_text(item['description'])}")
        if item.get("search_queries"):
            lines.append(f"      Research queries: {_summarize_values(item['search_queries'], limit=3)}")
        if item.get("affected_pages"):
            lines.append(f"      Pages: {_summarize_values(item['affected_pages'], limit=5)}")
        lines.append("")
    lines.append("Actions: `python cli.py review resolve <index|item_id> [--resolution skip|create]`")
    return "\n".join(lines)


def _resolve_combined_item(identifier, resolution: str = "skip"):
    pending = _combined_pending_items()
    target = None
    if isinstance(identifier, str) and not identifier.isdigit():
        target = next((item for item in pending if item.get("item_id") == identifier), None)
    else:
        index = int(identifier)
        if 0 <= index < len(pending):
            target = pending[index]
    if not target:
        return None

    if target.get("queue_kind") == "governance":
        return governance_store.resolve_governance_item(target["item_id"], resolution)
    return review.resolve_item(target["item_id"], resolution)


def review_vector_lake(action: str = "list", index="-1", resolution: str = "skip"):
    if action == "list":
        return _format_combined_report(_combined_pending_items())
    if action == "resolve":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review resolve <index|item_id>"
        item = _resolve_combined_item(index, resolution)
        if not item:
            return f"Failed to resolve item '{index}'."

        result = f"Resolved item {item.get('item_id')} ('{item.get('title')}') → {resolution}."
        if resolution == "create" and item.get("search_queries"):
            queries = " | ".join(item["search_queries"])
            result += "\n\n[SYSTEM DIRECTIVE]: Autonomous Deep Research Triggered.\n"
            result += "Agent: You must now execute `google_web_search` for the following queries:\n"
            result += f"{queries}\n"
            result += "Save the extracted content as Markdown to MEMORY/raw/, then run `python cli.py sync`."
        return result
    return f"Unknown review action: {action}. Use 'list' or 'resolve'."

