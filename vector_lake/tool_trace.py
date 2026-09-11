from vector_lake import provenance
from vector_lake.db_store import require_current_schema_for_read


def trace_vector_lake(query_or_id: str) -> str:
    # Reading provenance must not initialize or bootstrap canonical state.
    require_current_schema_for_read("claims", "entities", "sources")
    trace = provenance.build_trace_for_query(query_or_id)
    return provenance.format_trace(trace)

