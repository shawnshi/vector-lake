from vector_lake import tool_ingest
from vector_lake import indexer


def sync_vector_lake():
    """Alias for prepare_ingest_batch for backwards compatibility."""
    res = tool_ingest.prepare_ingest_batch(batch_size=50)
    indexer.generate_index()
    return f"Legacy Sync triggered. Index regenerated.\n\n{res}"

