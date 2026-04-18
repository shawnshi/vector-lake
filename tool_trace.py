import provenance


def trace_vector_lake(query_or_id: str) -> str:
    trace = provenance.build_trace_for_query(query_or_id)
    return provenance.format_trace(trace)
