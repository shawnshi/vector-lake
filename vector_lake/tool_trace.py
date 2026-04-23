from vector_lake import provenance
from vector_lake import governance_store


def trace_vector_lake(query_or_id: str) -> str:
    governance_store.ensure_canonical_store_populated()
    trace = provenance.build_trace_for_query(query_or_id)
    return provenance.format_trace(trace)

