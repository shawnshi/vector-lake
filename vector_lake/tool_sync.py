from vector_lake import ingest
from vector_lake import indexer


def sync_vector_lake():
    ingest.sync_all()
    indexer.generate_index()
    return "Ingestion Sync (2-Step CoT) and Index generation completed."

