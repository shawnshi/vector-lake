from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json

from vector_lake import governance_store
from vector_lake.merge_analysis import analyze_entities, normalize_name, preflight_suggestion
from vector_lake.wiki_utils import get_wiki_dir


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _normalized_name(value: str) -> str:
    return normalize_name(value)


def claim_governance_version(claim: dict) -> str:
    stable = dict(claim)
    stable.pop("validity_state", None)
    stable.pop("validity_reasons", None)
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def infer_claim_validity(claim: dict, now=None) -> dict:
    now = now or _utc_now()
    valid_to = _parse_dt(claim.get("valid_to"))
    review_after = _parse_dt(claim.get("review_after"))
    freshness_tier = str(claim.get("freshness_tier", "unknown")).lower()
    confidence = float(claim.get("confidence", 0) or 0)
    status = str(claim.get("status", "Active")).lower()
    evidence_count = len(claim.get("evidence_ids", []))
    source_count = len(claim.get("source_ids", []))
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
        if source_count:
            reasons.append("source_only_without_block_evidence")
            return {"validity_state": "provisional", "reasons": reasons}
        reasons.append("missing_evidence_and_source")
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


def find_merge_candidate_report(
    limit: int | None = 20,
    run_preflight: bool = True,
    decision: str | None = None,
) -> dict:
    entities = list(
        governance_store.query_entities(
            {"status!=": "Merged", "type!=": "system"}
        )["items"].values()
    )
    page_keys = {
        str(entity.get("page_key"))
        for entity in entities
        if entity.get("page_key")
    }
    snapshot_versions = governance_store.canonical_page_versions(page_keys)
    candidate_pool = analyze_entities(
        entities,
        limit=None,
        versions=snapshot_versions,
    )
    decision_order = ("merge", "alias", "review", "keep_separate")
    if decision is not None and decision not in decision_order:
        raise ValueError(f"Unsupported merge decision filter: {decision}")
    decision_counts = Counter(
        candidate["decision"]
        for candidate in candidate_pool
    )
    filtered_pool = (
        [
            candidate
            for candidate in candidate_pool
            if candidate["decision"] == decision
        ]
        if decision
        else candidate_pool
    )
    if limit is None:
        suggestions = filtered_pool
    elif decision:
        suggestions = filtered_pool[: max(0, limit)]
    else:
        suggestions = []
        buckets = {
            candidate_decision: [
                candidate
                for candidate in candidate_pool
                if candidate["decision"] == candidate_decision
            ]
            for candidate_decision in decision_order
        }
        max_bucket_size = max(
            (len(bucket) for bucket in buckets.values()),
            default=0,
        )
        for offset in range(max_bucket_size):
            for candidate_decision in decision_order:
                if len(suggestions) >= max(0, limit):
                    break
                bucket = buckets[candidate_decision]
                if offset < len(bucket):
                    suggestions.append(bucket[offset])
            if len(suggestions) >= max(0, limit):
                break

    if run_preflight:
        wiki_dir = get_wiki_dir()
        suggestions = [
            preflight_suggestion(suggestion, wiki_dir)
            for suggestion in suggestions
        ]
        checked_page_keys = {
            page_key
            for suggestion in suggestions
            for page_key in (
                suggestion.get("left_page_key"),
                suggestion.get("right_page_key"),
            )
            if page_key
        }
        current_versions = governance_store.canonical_page_versions(checked_page_keys)
        for suggestion in suggestions:
            if suggestion.get("decision") != "merge":
                suggestion["snapshot_state"] = "not_applicable"
                continue
            expected = {
                suggestion.get("left_page_key"): suggestion.get("left_version", ""),
                suggestion.get("right_page_key"): suggestion.get("right_version", ""),
            }
            changed = [
                page_key
                for page_key, version in expected.items()
                if page_key and current_versions.get(page_key, "") != version
            ]
            if changed:
                suggestion["preflight_state"] = "blocked"
                suggestion.setdefault("preflight_errors", []).append(
                    "Canonical version changed during preflight: " + ", ".join(sorted(changed))
                )
                suggestion["snapshot_state"] = "changed"
            else:
                suggestion["snapshot_state"] = "stable"

    return {
        "candidate_pool_size": len(candidate_pool),
        "actionable_pool_size": sum(
            decision_counts[candidate_decision]
            for candidate_decision in ("merge", "alias", "review")
        ),
        "decision_counts": {
            candidate_decision: decision_counts[candidate_decision]
            for candidate_decision in decision_order
        },
        "selected_decision_counts": dict(
            Counter(candidate["decision"] for candidate in suggestions)
        ),
        "returned_count": len(suggestions),
        "suggestions": suggestions,
    }


def find_merge_candidates(
    limit: int | None = 20,
    run_preflight: bool = True,
    decision: str | None = None,
) -> list[dict]:
    return find_merge_candidate_report(
        limit=limit,
        run_preflight=run_preflight,
        decision=decision,
    )["suggestions"]


def compute_debt_metrics(skip_heavy: bool = False) -> dict:
    """Compute governance counts without materializing the whole canonical graph."""
    governance_store.initialize_meta_store()
    conn = governance_store.get_connection()
    now = _utc_now()
    validity_state_counts = defaultdict(int)
    unsupported_claim_count = 0
    managed_unsupported_claim_count = 0
    conflicted_claim_count = 0
    stale_claim_count = 0
    expired_claim_count = 0
    review_due_claim_count = 0
    provisional_claim_count = 0
    high_centrality_low_confidence = 0
    source_ids_with_claims = set()
    claim_count = 0

    managed_claim_items = {}
    for row in conn.execute(
            "SELECT data_json FROM governance_queue "
            "WHERE json_extract(data_json, '$.type') = 'evidence-gap' "
            "AND json_extract(data_json, '$.status') = 'acknowledged' "
            "AND json_extract(data_json, '$.claim_id') IS NOT NULL"
    ):
        item = json.loads(row["data_json"])
        managed_claim_items[str(item.get("claim_id") or "")] = item

    for row in conn.execute("SELECT data_json FROM claims"):
        raw_claim = json.loads(row["data_json"])
        claim_version = claim_governance_version(raw_claim)
        claim_count += 1
        source_ids_with_claims.update(str(item) for item in raw_claim.get("source_ids", []))
        claim = annotate_claim_validity(raw_claim, now=now)
        state = claim.get("validity_state", "active")
        validity_state_counts[state] += 1
        if state == "unsupported":
            unsupported_claim_count += 1
            managed_item = managed_claim_items.get(str(claim.get("claim_id") or ""))
            due_at = _parse_dt((managed_item or {}).get("due_at"))
            if (
                managed_item
                and str(managed_item.get("owner") or "").strip()
                and due_at is not None
                and due_at >= now
                and str(managed_item.get("claim_version") or "") == claim_version
            ):
                managed_unsupported_claim_count += 1
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

    orphan_source_count = sum(
        1
        for row in conn.execute("SELECT source_id FROM sources")
        if str(row["source_id"]) not in source_ids_with_claims
    )
    pending_governance_item_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM governance_queue "
            "WHERE json_extract(data_json, '$.status') = 'pending'"
        ).fetchone()[0]
    )
    pending_change_set_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM change_sets "
            "WHERE json_extract(data_json, '$.status') = 'pending'"
        ).fetchone()[0]
    )
    missing_link_target_count = 0
    managed_missing_link_target_count = 0
    for row in conn.execute(
        "SELECT data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.type') = 'missing-link-target' "
        "AND json_extract(data_json, '$.status') = 'acknowledged'"
    ):
        item = json.loads(row["data_json"])
        missing_link_target_count += 1
        due_at = _parse_dt(item.get("due_at"))
        if (
            str(item.get("owner") or "").strip()
            and due_at is not None
            and due_at >= now
        ):
            managed_missing_link_target_count += 1
    operational_memory_count = int(conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0])
    if operational_memory_count == 0 and claim_count and not skip_heavy:
        governance_store.rebuild_operational_memory()
        operational_memory_count = int(conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0])
    memory_type_counts = {
        str(row["memory_type"] or "unknown"): int(row["count"])
        for row in conn.execute(
            "SELECT memory_type, COUNT(*) AS count FROM operational_memory GROUP BY memory_type"
        )
    }
    memory_validity_counts = {
        str(row["validity_state"] or "active"): int(row["count"])
        for row in conn.execute(
            "SELECT json_extract(data_json, '$.validity_state') AS validity_state, "
            "COUNT(*) AS count FROM operational_memory GROUP BY validity_state"
        )
    }
    merge_candidates = [] if skip_heavy else find_merge_candidates(limit=20, run_preflight=False)

    return {
        "stale_claim_count": stale_claim_count,
        "expired_claim_count": expired_claim_count,
        "review_due_claim_count": review_due_claim_count,
        "unsupported_claim_count": unsupported_claim_count,
        "managed_unsupported_claim_count": managed_unsupported_claim_count,
        "unmanaged_unsupported_claim_count": max(
            0,
            unsupported_claim_count - managed_unsupported_claim_count,
        ),
        "conflicted_claim_count": conflicted_claim_count,
        "provisional_claim_count": provisional_claim_count,
        "pending_change_set_count": pending_change_set_count,
        "merge_candidate_count": len(merge_candidates),
        "orphan_source_count": orphan_source_count,
        "high_centrality_low_confidence_count": high_centrality_low_confidence,
        "pending_governance_item_count": pending_governance_item_count,
        "acknowledged_missing_link_target_count": missing_link_target_count,
        "managed_missing_link_target_count": managed_missing_link_target_count,
        "unmanaged_missing_link_target_count": max(
            0,
            missing_link_target_count - managed_missing_link_target_count,
        ),
        "operational_memory_count": operational_memory_count,
        "superseded_memory_count": memory_validity_counts.get("superseded", 0),
        "conflicted_memory_count": memory_validity_counts.get("conflicted", 0),
        "memory_type_counts": memory_type_counts,
        "validity_state_counts": dict(validity_state_counts),
    }

