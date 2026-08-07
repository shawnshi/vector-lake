import re

from vector_lake import governance_metrics
from vector_lake import governance_store


def _tokenize(query: str) -> list[str]:
    return [token.lower() for token in re.split(r"\W+", query or "") if token.strip()]


def build_trace_for_query(query: str, top_k: int = 5) -> dict:
    if top_k < 0:
        raise ValueError("top_k must be non-negative")
    if top_k == 0:
        return {"query": query, "items": []}
    tokens = _tokenize(query)

    from vector_lake.db_store import search_wiki
    search_results = search_wiki(query, limit=10)
    relevant_pages = {res["node_key"] for res in search_results}
    claims = governance_store.select_trace_claims(tokens, relevant_pages, top_k)
    entity_ids = {
        str(entity_id)
        for claim in claims
        for entity_id in claim.get("subject_entity_ids", [])
    }
    source_ids = {
        str(source_id)
        for claim in claims
        for source_id in claim.get("source_ids", [])
    }
    entity_names, source_pages = governance_store.load_trace_labels(
        entity_ids,
        source_ids,
    )

    trace_items = []
    for claim in claims:
        annotated = governance_metrics.annotate_claim_validity(claim)
        trace_items.append({
            "claim_id": annotated["claim_id"],
            "claim_text": annotated.get("claim_text", ""),
            "subject_entities": [
                entity_names[entity_id]
                for entity_id in annotated.get("subject_entity_ids", [])
                if entity_id in entity_names
            ],
            "source_pages": [
                source_pages[source_id]
                for source_id in annotated.get("source_ids", [])
                if source_id in source_pages
            ],
            "confidence": annotated.get("confidence"),
            "valid_to": annotated.get("valid_to"),
            "review_after": annotated.get("review_after"),
            "validity_state": annotated.get("validity_state"),
            "evidence_count": len(annotated.get("evidence_ids", [])),
            "locator": annotated.get("locator", {}),
        })

    return {"query": query, "items": trace_items}


def format_trace(trace: dict) -> str:
    if not trace.get("items"):
        return "No provenance trace found."
    lines = ["=== Provenance Trace ===", f"Query: {trace.get('query', '')}", ""]
    for index, item in enumerate(trace["items"], start=1):
        lines.append(f"[{index}] {item['claim_id']}")
        lines.append(f"  Claim: {item['claim_text']}")
        if item["subject_entities"]:
            lines.append(f"  Entities: {', '.join(item['subject_entities'])}")
        if item["source_pages"]:
            lines.append(f"  Source Pages: {', '.join(item['source_pages'])}")
        lines.append(f"  Confidence: {item.get('confidence')}")
        lines.append(f"  Validity: {item.get('validity_state')}")
        lines.append(f"  Evidence Count: {item.get('evidence_count')}")
        locator = item.get("locator") or {}
        if locator:
            lines.append(f"  Locator: {locator.get('page_key', '')}#{locator.get('heading', '')}:{locator.get('block_index', '')}")
        if item.get("review_after"):
            lines.append(f"  Review After: {item['review_after']}")
        lines.append("")
    return "\n".join(lines).strip()

