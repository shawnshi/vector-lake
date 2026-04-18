from vector_lake import governance_store
from vector_lake import indexer


def migrate_v8(dry_run: bool = False) -> str:
    result = governance_store.migrate_existing_wiki(dry_run=dry_run)
    if not dry_run:
        indexer.generate_index()
    return (
        f"V8 migration {'previewed' if dry_run else 'completed'}.\n"
        f"Pages scanned: {result['pages_scanned']}\n"
        f"Entities: {result['entities']}\n"
        f"Claims: {result['claims']}\n"
        f"Evidence: {result['evidence']}\n"
        f"Sources: {result['sources']}"
    )

