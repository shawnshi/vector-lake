from vector_lake import review


def review_vector_lake(action: str = "list", index: int = -1, resolution: str = "skip"):
    if action == "list":
        return review.format_pending_report()
    if action == "resolve":
        if index < 0:
            return "Error: specify review item index. Usage: cli.py review resolve <index>"
        item = review.resolve_item(index, resolution)
        if not item:
            return f"Failed to resolve item #{index}."

        result = f"Resolved item #{index} ('{item.get('title')}') → {resolution}."
        if resolution == "create" and item.get("search_queries"):
            queries = " | ".join(item["search_queries"])
            result += "\n\n[SYSTEM DIRECTIVE]: Autonomous Deep Research Triggered.\n"
            result += "Agent: You must now execute `google_web_search` for the following queries:\n"
            result += f"{queries}\n"
            result += "Save the extracted content as Markdown to MEMORY/raw/, then run `python cli.py sync`."
        return result
    return f"Unknown review action: {action}. Use 'list' or 'resolve'."

