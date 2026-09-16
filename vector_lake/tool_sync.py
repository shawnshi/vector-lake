from vector_lake import tool_ingest
def sync_vector_lake():
    """Alias for prepare_ingest_batch for backwards compatibility."""
    res = tool_ingest.prepare_ingest_batch(batch_size=50)
    return f"Legacy Sync prepared ingest work. Index updates are committed through the durable outbox.\n\n{res}"

