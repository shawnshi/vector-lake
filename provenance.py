import re

import governance_metrics
import governance_store


def _tokenize(query: str) -> list[str]:
    return [token.lower() for token in re.split(r"\W+", query or "") if token.strip()]


def build_trace_for_query(query: str, top_k: int = 5) -> dict:
    tokens = _tokenize(query)
    claims = governance_store.load_claims()["items"].values()
    entities = governance_store.load_entities()["items"]
    sources = governance_store.load_sources()["items"]

    matches = []
    for claim in claims:
        haystack = f"{claim.get('claim_text', '')} {claim.get('source_page', '')}".lower()
        score = sum(1 for token in tokens if token in haystack)
        if score > 0:
            matches.append((score, claim))
    matches.sort(key=lambda item: item[0], reverse=True)

    trace_items = []
    for _, claim in matches[:top_k]:
        annotated = governance_metrics.annotate_claim_validity(claim)
        trace_items.append({
            "claim_id": annotated["claim_id"],
            "claim_text": annotated.get("claim_text", ""),
            "subject_entities": [entities[entity_id]["canonical_name"] for entity_id in annotated.get("subject_entity_ids", []) if entity_id in entities],
            "source_pages": [sources[source_id]["canonical_source_page"] for source_id in annotated.get("source_ids", []) if source_id in sources],
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
    lines = [f"=== Provenance Trace ===", f"Query: {trace.get('query', '')}", ""]
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
