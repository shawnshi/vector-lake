from vector_lake import governance_store


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
    for item in governance_store.reviewable_governance_items():
        enriched = governance_store.normalize_governance_item(item)
        enriched["queue_kind"] = "governance"
        combined.append(enriched)
    combined.sort(key=governance_store.governance_priority_sort_key)
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
        "community_naming": "[N]",
    }
    lines = [
        f"[REVIEW] {len(items)} Pending Governance Items",
        "",
    ]
    for index, item in enumerate(items):
        icon = type_icons.get(item.get("type"), "[*]")
        lines.append(f"  [{index}] {icon} **{item.get('title', 'Untitled')}** ({item.get('type', 'unknown')})")
        lines.append(f"      ID: {item.get('item_id', 'unknown')}")
        lines.append(f"      Status: {item.get('status', 'pending')}")
        lines.append(f"      Priority: {item.get('priority', 'P2')}")
        if item.get("critical_decision_refs"):
            lines.append(
                "      Critical decisions: "
                + _summarize_values(item["critical_decision_refs"], limit=5)
            )
        lines.append(f"      Source: {item.get('source', 'unknown')}")
        if item.get("description"):
            lines.append(f"      {_truncate_text(item['description'])}")
        if item.get("search_queries"):
            lines.append(f"      Research queries: {_summarize_values(item['search_queries'], limit=3)}")
        if item.get("affected_pages"):
            lines.append(f"      Pages: {_summarize_values(item['affected_pages'], limit=5)}")
        lines.append("")
    lines.append("Actions: `python cli.py review resolve <index|item_id> [--resolution skip|create|merge|acknowledge]`")
    return "\n".join(lines)


def _resolve_combined_item(identifier, resolution: str = "skip", change_manifest: dict = None):
    from vector_lake import governance_service

    if isinstance(identifier, str) and not identifier.isdigit():
        return governance_service.resolve_governance_item(
            identifier,
            resolution,
            change_manifest=change_manifest,
        )

    pending = _combined_pending_items()
    index = int(identifier)
    if not 0 <= index < len(pending):
        return None
    return governance_service.resolve_governance_item(
        pending[index]["item_id"],
        resolution,
        change_manifest=change_manifest,
    )


def review_vector_lake(action: str = "list", index="-1", resolution: str = "skip", change_manifest: dict = None):
    if action == "list":
        return _format_combined_report(_combined_pending_items())
    if action == "ground":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review ground <index|item_id>"
        pending = _combined_pending_items()
        target = None
        if isinstance(index, str) and not index.isdigit():
            target = next((item for item in pending if item.get("item_id") == index), None)
        else:
            idx = int(index)
            if 0 <= idx < len(pending):
                target = pending[idx]
        if not target:
            return f"Failed to resolve item '{index}'."
        if target.get("type") != "missing-page":
            return f"Item {target.get('item_id')} is not a missing-page item. Cannot ground."

        queries = " | ".join(target.get("search_queries", []))
        result = f"[SYSTEM DIRECTIVE]: Autonomous Web Grounding Triggered for Item {target.get('item_id')}.\n"
        result += "Agent: You must now execute the following steps:\n"
        result += f"1. Use `google_web_search` with the queries: {queries}\n"
        result += "2. Pick the most authoritative result and fetch it using the `url-to-markdown` skill (or web_fetch).\n"
        result += "3. Use `write_file` to save the clean Markdown content to a new file in `MEMORY/raw/news/` (or appropriate subfolder).\n"
        result += f"4. Resolve this governance item by running `python cli.py review resolve {target.get('item_id')} --resolution create`."
        return result
    if action == "name_community":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review name_community <index|item_id>"
        pending = _combined_pending_items()
        target = next((item for item in pending if item.get("item_id") == index), None) if not str(index).isdigit() else pending[int(index)] if 0 <= int(index) < len(pending) else None
        if not target or target.get("type") != "community_naming":
            return f"Item '{index}' is not a valid community_naming item."
            
        hubs = target.get("hubs", [])
        page = target.get("affected_pages", [""])[0]
        result = f"[SYSTEM DIRECTIVE]: Autonomous Semantic Naming Triggered for {target.get('item_id')}.\n"
        result += "Agent: You must now execute the following steps:\n"
        result += f"1. Analyze the following Hub nodes: {', '.join(hubs)}\n"
        result += "2. Synthesize a 3-5 word high-level business/domain abstraction for this community.\n"
        result += f"3. Use `multi_replace_file_content` to edit `MEMORY/wiki/{page}`, replacing `*(To be generated by LLM during Review/Synthesis)*` with your generated summary under the `## 语义总结 (Semantic Summary)` section.\n"
        result += f"4. Resolve this governance item by running `python cli.py review resolve {target.get('item_id')} --resolution named`."
        return result
    if action == "resolve":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review resolve <index|item_id>"
        item = _resolve_combined_item(index, resolution, change_manifest=change_manifest)
        if not item:
            return f"Failed to resolve item '{index}'."

        status = str(item.get("status") or "")
        if status == "resolved":
            result = f"Resolved item {item.get('item_id')} ('{item.get('title')}') → {resolution}."
        elif status == "projection_pending":
            result = (
                f"Merge committed for item {item.get('item_id')} "
                f"('{item.get('title')}'); projection recovery pending."
            )
            if item.get("last_projection_error"):
                result += f"\nLast projection error: {item['last_projection_error']}"
            if item.get("merge_outbox_statuses"):
                result += f"\nOutbox statuses: {item['merge_outbox_statuses']}"
        else:
            return (
                f"Item {item.get('item_id')} returned nonterminal status "
                f"'{status or 'unknown'}'; no resolution was reported."
            )
        if resolution == "create" and item.get("search_queries"):
            queries = " | ".join(item["search_queries"])
            result += "\n\n[SYSTEM DIRECTIVE]: Autonomous Deep Research Triggered.\n"
            result += "Agent: You must now execute `google_web_search` for the following queries:\n"
            result += f"{queries}\n"
            result += "Save the extracted content as Markdown to MEMORY/raw/, then run `python cli.py sync`."
        return result
    return f"Unknown review action: {action}. Use 'list', 'resolve', or 'ground'."

