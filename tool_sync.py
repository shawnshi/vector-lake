import ingest
import indexer


def sync_vector_lake():
    ingest.sync_all()
    indexer.generate_index()
    return "Ingestion Sync (2-Step CoT) and Index generation completed."
