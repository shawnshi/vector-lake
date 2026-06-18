from vector_lake import tool_ingest
from vector_lake import indexer


def sync_vector_lake():
    """Alias for prepare_ingest_batch for backwards compatibility."""
    res = tool_ingest.prepare_ingest_batch(batch_size=50)
    
    from vector_lake import get_extension_root
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with open(tmp_dir / "flag_reindex.lock", "w") as f:
        f.write("1")
        
    return f"Legacy Sync triggered. Async index rebuild scheduled.\n\n{res}"

