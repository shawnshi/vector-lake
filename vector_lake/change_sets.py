from vector_lake import governance_store


def create_change_set(page_paths: list[str], origin: str, summary: str | None = None, auto_approve: bool = False) -> dict:
    return governance_store.create_change_set(page_paths, origin=origin, summary=summary, auto_approve=auto_approve)


def publish_pending(limit: int | None = None) -> dict:
    return governance_store.publish_change_sets(limit=limit)

