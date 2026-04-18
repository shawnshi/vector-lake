import change_sets


def publish_vector_lake(limit: int | None = None) -> str:
    result = change_sets.publish_pending(limit=limit)
    if not result["published"]:
        return "No pending change sets."
    return f"Published {result['published']} change set(s). Rebuilt {result.get('views_rebuilt', 0)} canonical view(s)."
