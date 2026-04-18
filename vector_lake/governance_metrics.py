import re
from datetime import datetime, timedelta, timezone

from vector_lake import governance_store


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def infer_claim_validity(claim: dict, now=None) -> dict:
    now = now or _utc_now()
    valid_to = _parse_dt(claim.get("valid_to"))
    review_after = _parse_dt(claim.get("review_after"))
    freshness_tier = str(claim.get("freshness_tier", "unknown")).lower()
    confidence = float(claim.get("confidence", 0) or 0)
    status = str(claim.get("status", "Active")).lower()
    evidence_count = len(claim.get("evidence_ids", []))
    contradictions = len(claim.get("contradicts", []))
    reasons = []

    if status in {"deprecated", "archived", "inactive"}:
        reasons.append("status")
        return {"validity_state": "expired", "reasons": reasons}
    if valid_to and valid_to < now:
        reasons.append("valid_to")
        return {"validity_state": "expired", "reasons": reasons}
    if contradictions:
        reasons.append("conflicts")
        return {"validity_state": "conflicted", "reasons": reasons}
    if evidence_count == 0:
        reasons.append("missing_evidence")
        return {"validity_state": "unsupported", "reasons": reasons}
    if review_after and review_after < now:
        reasons.append("review_after")
        return {"validity_state": "review-due", "reasons": reasons}
    if valid_to and valid_to < now + timedelta(days=14):
        reasons.append("valid_to")
        return {"validity_state": "expiring-soon", "reasons": reasons}
    if freshness_tier in {"volatile", "breaking", "fast-changing"} and not review_after:
        reasons.append("freshness_tier")
        return {"validity_state": "needs-review", "reasons": reasons}
    if confidence < 0.55:
        reasons.append("confidence")
        return {"validity_state": "provisional", "reasons": reasons}
    return {"validity_state": "active", "reasons": reasons}


def annotate_claim_validity(claim: dict, now=None) -> dict:
    validity = infer_claim_validity(claim, now=now)
    annotated = dict(claim)
    annotated["validity_state"] = validity["validity_state"]
    annotated["validity_reasons"] = validity["reasons"]
    return annotated


def find_merge_candidates(limit: int = 20) -> list[dict]:
    entities = list(governance_store.load_entities()["items"].values())
    candidates = []

    for index, left in enumerate(entities):
        left_names = {left.get("canonical_name", ""), *left.get("aliases", [])}
        left_norms = {_normalized_name(name) for name in left_names if name}
        left_tokens = {token for name in left_names for token in re.split(r"\W+", str(name).lower()) if token}
        for right in entities[index + 1 :]:
            if left["entity_id"] == right["entity_id"]:
                continue

            right_names = {right.get("canonical_name", ""), *right.get("aliases", [])}
            right_norms = {_normalized_name(name) for name in right_names if name}
            right_tokens = {token for name in right_names for token in re.split(r"\W+", str(name).lower()) if token}

            reasons = []
            score = 0
            alias_overlap = (left_names & right_names) - {""}
            if alias_overlap:
                reasons.append(f"alias-overlap:{', '.join(sorted(alias_overlap)[:3])}")
                score += 3
            if left_norms & right_norms:
                reasons.append("normalized-name-match")
                score += 3
            token_overlap = left_tokens & right_tokens
            if len(token_overlap) >= 2:
                reasons.append(f"token-overlap:{', '.join(sorted(token_overlap)[:4])}")
                score += 1
            if left.get("domain") == right.get("domain") and left.get("topic_cluster") == right.get("topic_cluster"):
                score += 1

            if score < 3:
                continue

            pair_key = "::".join(sorted([left["entity_id"], right["entity_id"]]))
            candidates.append({
                "pair_key": pair_key,
                "score": score,
                "left_entity_id": left["entity_id"],
                "left_name": left.get("canonical_name", left["entity_id"]),
                "right_entity_id": right["entity_id"],
                "right_name": right.get("canonical_name", right["entity_id"]),
                "reasons": reasons,
                "domain": left.get("domain") or right.get("domain") or "General",
            })

    candidates.sort(key=lambda item: (-item["score"], item["pair_key"]))
    return candidates[:limit]


def compute_debt_metrics() -> dict:
    claims = [annotate_claim_validity(claim) for claim in governance_store.load_claims()["items"].values()]
    entities = governance_store.load_entities()["items"].values()
    sources = governance_store.load_sources()["items"].values()
    queue = governance_store.load_governance_queue()["items"]

    validity_state_counts = {}
    unsupported_claim_count = 0
    conflicted_claim_count = 0
    stale_claim_count = 0
    expired_claim_count = 0
    review_due_claim_count = 0
    provisional_claim_count = 0
    high_centrality_low_confidence = 0

    for claim in claims:
        state = claim.get("validity_state", "active")
        validity_state_counts[state] = validity_state_counts.get(state, 0) + 1
        if state == "unsupported":
            unsupported_claim_count += 1
        if state == "conflicted":
            conflicted_claim_count += 1
        if state in {"review-due", "needs-review", "expiring-soon"}:
            stale_claim_count += 1
        if state == "expired":
            expired_claim_count += 1
        if state == "review-due":
            review_due_claim_count += 1
        if state == "provisional":
            provisional_claim_count += 1
        if float(claim.get("confidence", 0)) < 0.5 and len(claim.get("subject_entity_ids", [])) > 0:
            high_centrality_low_confidence += 1

    source_ids_with_claims = {source_id for claim in claims for source_id in claim.get("source_ids", [])}
    orphan_source_count = len([source for source in sources if source["source_id"] not in source_ids_with_claims])
    pending_items = [item for item in queue if item.get("status") == "pending"]
    merge_candidates = find_merge_candidates(limit=20)

    return {
        "stale_claim_count": stale_claim_count,
        "expired_claim_count": expired_claim_count,
        "review_due_claim_count": review_due_claim_count,
        "unsupported_claim_count": unsupported_claim_count,
        "conflicted_claim_count": conflicted_claim_count,
        "provisional_claim_count": provisional_claim_count,
        "pending_change_set_count": len(governance_store.pending_change_sets()),
        "merge_candidate_count": len(merge_candidates),
        "orphan_source_count": orphan_source_count,
        "high_centrality_low_confidence_count": high_centrality_low_confidence,
        "pending_governance_item_count": len(pending_items),
        "validity_state_counts": validity_state_counts,
    }

