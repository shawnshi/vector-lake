"""Exact, preview-first removal of generated placeholder claims."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import db_store, governance_store
from vector_lake.claim_extractor import classify_non_claim_text

_CONTRACT = "claim-placeholder-cleanup/v1"
_ALLOWED_REASONS = {"generated_reshaped_stub", "generated_entity_stub"}
_ACTIVE_JOB_STATES = {
    "pending", "queued", "dispatched", "running", "leased", "retryable",
    "awaiting_subagent", "subagent_processing", "claimed", "prepared",
}
_TERMINAL_STATES = {
    "completed", "finalized", "cancelled", "canceled", "superseded", "resolved",
    "retired", "applied", "committed", "published", "rejected", "done", "expired",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _scope(value: list[str]) -> list[str]:
    if (
        not isinstance(value, list) or not 1 <= len(value) <= 256
        or any(not isinstance(item, str) or not item or any(c.isspace() for c in item) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("source_claim_ids must contain 1..256 unique nonempty whitespace-free IDs.")
    return sorted(value)


def _decode(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed_json:{label}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"malformed_json:{label}")
    return value


def _contains(value: object, identifiers: set[str]) -> bool:
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, list):
        return any(_contains(item, identifiers) for item in value)
    if isinstance(value, dict):
        return any(_contains(item, identifiers) for item in value.values())
    return False


def _runtime_row_contains(row, identifiers: set[str]) -> bool:
    for value in dict(row).values():
        if not isinstance(value, str) or not value:
            continue
        if value in identifiers:
            return True
        if value[:1] in {"{", "["}:
            try:
                if _contains(json.loads(value), identifiers):
                    return True
            except json.JSONDecodeError as exc:
                raise ValueError("malformed_runtime_json") from exc
    return False


def _late_apply_hook() -> None:
    """Fault-injection seam immediately before transactional commit."""


def _rows(conn, table: str, key: str, ids: list[str]):
    marks = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT * FROM {table} WHERE {key} IN ({marks}) ORDER BY {key}",  # noqa: S608
        tuple(ids),
    ).fetchall()


def _plan(source_claim_ids: list[str]) -> dict:
    ids = _scope(source_claim_ids)
    conn = db_store.require_current_schema_for_read(
        "claims", "operational_memory", "claim_versions", "evidence", "claim_graph_edges",
        "claim_assessments", "timeline_events", "jobs", "governance_queue", "runtime_generations",
        "mutation_outbox", "change_sets", "ingest_task_cleanup",
        "canonical_identities",
    )
    selected = set(ids)
    blockers: list[str] = []
    claim_rows = _rows(conn, "claims", "claim_id", ids)
    if [str(row["claim_id"]) for row in claim_rows] != ids:
        blockers.append("scope_missing_or_stale")
    claims: dict[str, dict] = {}
    for row in claim_rows:
        claim_id = str(row["claim_id"])
        claim = _decode(row["data_json"], f"claim:{claim_id}")
        claims[claim_id] = claim
        if (claim.get("claim_id") != claim_id
                or claim.get("claim_text") != row["claim_text"]
                or str(claim.get("status") or "").lower() != str(row["status"] or "").lower()):
            blockers.append(f"claim_physical_json_mismatch:{claim_id}")
        page = str((claim.get("locator") or {}).get("page_key") or "")
        reason = classify_non_claim_text(str(claim.get("claim_text") or ""), page_key=page)
        if str(row["status"] or claim.get("status") or "").lower() != "active":
            blockers.append(f"claim_not_active:{claim_id}")
        if reason not in _ALLOWED_REASONS:
            blockers.append(f"claim_not_matching_placeholder:{claim_id}")
        if claim.get("source_ids") != [] or claim.get("evidence_ids") != []:
            blockers.append(f"claim_has_provenance:{claim_id}")
        family, _page = governance_store._record_family(claim, "claim_family_id", "claimfamily")
        record_hash = hashlib.sha256(governance_store._canonical_record_json(claim).encode("utf-8")).hexdigest()
        found = conn.execute(
            "SELECT 1 FROM claim_versions WHERE claim_family_id = ? AND record_hash = ?",
            (family, record_hash),
        ).fetchone()
        if found is None:
            blockers.append(f"claim_preimage_not_versioned:{claim_id}")
        identity = conn.execute(
            "SELECT page_key,identity_origin,data_json FROM canonical_identities "
            "WHERE record_kind='claim' AND record_id=?", (claim_id,),
        ).fetchone()
        try:
            owner_page = governance_store._identity_registry_owner_page(
                identity, record_kind="claim", record_id=claim_id,
            ) if identity is not None else ""
        except (ValueError, TypeError):
            owner_page = ""
        if not owner_page or owner_page != page:
            blockers.append(f"claim_identity_invalid:{claim_id}")

    memory_rows = conn.execute(
        "SELECT * FROM operational_memory WHERE json_extract(data_json, '$.source_claim_id') "
        f"IN ({','.join('?' for _ in ids)}) ORDER BY memory_id", tuple(ids),
    ).fetchall()
    memories: dict[str, dict] = {}
    per_claim = {claim_id: 0 for claim_id in ids}
    for row in memory_rows:
        memory_id = str(row["memory_id"])
        memory = _decode(row["data_json"], f"memory:{memory_id}")
        memories[memory_id] = memory
        claim_id = str(memory.get("source_claim_id") or "")
        if claim_id in per_claim:
            per_claim[claim_id] += 1
        reasons = memory.get("validity_reasons") or []
        owner = claims.get(claim_id, {})
        if memory.get("text") != owner.get("claim_text"):
            blockers.append(f"memory_claim_text_mismatch:{memory_id}")
        owner_reason = classify_non_claim_text(
            str(owner.get("claim_text") or ""),
            page_key=str((owner.get("locator") or {}).get("page_key") or ""),
        )
        if (not isinstance(reasons, list)
                or any(not isinstance(reason, str) for reason in reasons)
                or any(reason.startswith("infrastructure_artifact:")
                       and reason != f"infrastructure_artifact:{owner_reason}" for reason in reasons)):
            blockers.append(f"memory_preserved_infrastructure_artifact:{memory_id}")
        if (memory.get("memory_id") != memory_id
                or memory.get("memory_type") != row["memory_type"]
                or str(memory.get("validity_state") or "").lower() != str(row["status"] or "").lower()):
            blockers.append(f"memory_physical_json_mismatch:{memory_id}")
        if (row["memory_type"] != "fact"
                or str(row["status"] or "").lower() != "archived"
                or memory.get("memory_type") != "fact"
                or str(memory.get("validity_state") or "").lower() != "archived"):
            blockers.append(f"memory_not_archived_fact:{memory_id}")
    for claim_id, count in per_claim.items():
        if count != 1:
            blockers.append(f"memory_cardinality:{claim_id}:{count}")
    memory_ids = set(memories)

    marks = ",".join("?" for _ in ids)
    evidence_refs = conn.execute(
        "SELECT value FROM evidence, json_each(evidence.data_json,'$.supports_claim_ids') "
        "UNION ALL SELECT value FROM evidence, json_each(evidence.data_json,'$.contradicts_claim_ids')"
    )
    if any(str(row[0]) in selected for row in evidence_refs):
        blockers.append("evidence_dependency")
    relation_paths = (
        "$.supersedes", "$.contradicts", "$.supersedes_claim_ids", "$.contradicts_claim_ids",
    )
    relation_sql = " UNION ALL ".join(
        f"SELECT c.claim_id,j.value FROM claims c,json_each(c.data_json, '{path}') j "
        f"WHERE c.claim_id NOT IN ({marks}) AND json_valid(c.data_json)" for path in relation_paths
    )
    relation_args = tuple(ids) * len(relation_paths)
    if conn.execute(
        f"SELECT 1 FROM ({relation_sql}) WHERE value IN ({marks}) LIMIT 1",
        relation_args + tuple(ids),
    ).fetchone():
        blockers.append("other_claim_dependency")
    other_memories = conn.execute(
        f"SELECT memory_id,data_json FROM operational_memory WHERE memory_id NOT IN "
        f"({','.join('?' for _ in memory_ids)})", tuple(sorted(memory_ids)),
    ) if memory_ids else conn.execute("SELECT memory_id,data_json FROM operational_memory")
    if any(_contains(_decode(row["data_json"], f"memory:{row['memory_id']}"), selected | memory_ids)
           for row in other_memories):
        blockers.append("operational_memory_dependency")
    if conn.execute(
        f"SELECT 1 FROM claim_graph_edges WHERE source_id IN ({marks}) OR target_id IN ({marks}) LIMIT 1",
        tuple(ids) + tuple(ids),
    ).fetchone(): blockers.append("claim_graph_dependency")
    if conn.execute(f"SELECT 1 FROM claim_assessments WHERE claim_id IN ({marks}) LIMIT 1", tuple(ids)).fetchone():
        blockers.append("claim_assessment_dependency")
    # Timeline is a flat schema, not a data_json envelope. Its stable event ID
    # embeds claim identity through hashing, so inspect derived IDs as well as
    # explicit references in its typed columns.
    from vector_lake.tool_timeline import _event_from_claim_row
    event_ids = [_event_from_claim_row(row)["id"] for row in claim_rows]
    if (any(claim.get("claim_type") == "timeline-event" for claim in claims.values())
            or (event_ids and _rows(conn, "timeline_events", "id", event_ids))
            or any(_contains(dict(row), selected | memory_ids)
                   for row in conn.execute("SELECT * FROM timeline_events"))):
        blockers.append("timeline_dependency")

    for row in conn.execute("SELECT job_id,status,retries,payload,result_json FROM jobs"):
        status = str(row["status"] or "").lower()
        active = status in _ACTIVE_JOB_STATES or (status == "failed" and int(row["retries"] or 0) < 3)
        if not active and status not in _TERMINAL_STATES and status != "failed":
            blockers.append("unknown_job_state")
            active = True
        if active:
            for field in ("payload", "result_json"):
                raw = row[field]
                if raw and _contains(_decode(raw, f"job:{row['job_id']}:{field}"), selected | memory_ids):
                    blockers.append("job_dependency")
    for table in ("mutation_outbox", "change_sets", "ingest_task_cleanup"):
        for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 - fixed vocabulary
            values = dict(row)
            state = str(values.get("status") or "").lower()
            if table == "change_sets":
                raw = values.get("data_json")
                state = str(_decode(raw, f"change_set:{values.get('change_set_id')}").get("status") or "").lower()
            terminal_states = (governance_store._CHANGE_SET_TERMINAL_STATUSES
                               if table == "change_sets" else _TERMINAL_STATES)
            active = state in _ACTIVE_JOB_STATES or (state == "failed" and table != "change_sets")
            if not active and state not in terminal_states:
                blockers.append(f"unknown_runtime_state:{table}")
                active = True
            if active and _runtime_row_contains(row, selected | memory_ids):
                blockers.append(f"runtime_dependency:{table}")

    queue_rows = []
    for row in conn.execute("SELECT item_id,data_json,updated_at FROM governance_queue ORDER BY item_id"):
        item = _decode(row["data_json"], f"queue:{row['item_id']}")
        if not _contains(item, selected | memory_ids):
            continue
        exact = (item.get("item_id") == row["item_id"]
                 and item.get("type") == "evidence-gap" and item.get("status") == "acknowledged"
                 and item.get("resolution") == "research-required" and item.get("claim_id") in selected)
        if exact:
            queue_rows.append(row)
        elif str(item.get("status") or "").lower() not in {"resolved", "closed", "retired"}:
            blockers.append(f"governance_dependency:{row['item_id']}")

    generations = {str(r["surface"]): int(r["generation"]) for r in conn.execute(
        "SELECT surface,generation FROM runtime_generations ORDER BY surface")}
    preserved = {
        "claim_versions": int(conn.execute(
            f"SELECT COUNT(*) FROM claim_versions WHERE claim_id IN ({marks})", tuple(ids)
        ).fetchone()[0]),
        "canonical_identities": int(conn.execute(
            f"SELECT COUNT(*) FROM canonical_identities WHERE record_kind='claim' "
            f"AND record_id IN ({marks})", tuple(ids)
        ).fetchone()[0]),
        "bystander_claims": int(conn.execute(
            f"SELECT COUNT(*) FROM claims WHERE claim_id NOT IN ({marks})", tuple(ids)
        ).fetchone()[0]),
    }
    basis = {
        "contract": _CONTRACT, "claim_ids": ids,
        "claim_rows": [_hash(dict(row)) for row in claim_rows],
        "memory_rows": [_hash(dict(row)) for row in memory_rows],
        "queue_rows": [_hash(dict(row)) for row in queue_rows],
        "runtime_generations": generations, "dependency_blockers": sorted(set(blockers)),
    }
    return {
        "contract": _CONTRACT, "claim_ids": ids, "claim_count": len(claim_rows),
        "memory_ids": sorted(memory_ids), "memory_count": len(memory_rows),
        "related_queue_ids": [str(row["item_id"]) for row in queue_rows],
        "related_queue_count": len(queue_rows), "eligible": not blockers,
        "blockers": sorted(set(blockers)), "runtime_generations": generations,
        "preserved_counts": preserved,
        "candidate_fingerprint": "sha256:" + _hash(basis),
    }


def _validate_backup(path: str, expected_generations: dict[str, int]) -> dict:
    from vector_lake.tool_backup_retention import _verify_restorable_backup_snapshot
    from vector_lake.tool_projection import validate_maintenance_backup_v4

    directory = Path(path)
    manifest, _inventory = validate_maintenance_backup_v4(directory / "manifest.json")
    if "vector_lake.db" not in manifest.get("copied", []):
        raise ValueError("placeholder_cleanup_backup_database_missing")
    if manifest.get("database_runtime_generations") != expected_generations:
        raise ValueError("placeholder_cleanup_backup_generation_mismatch")
    _verify_restorable_backup_snapshot(directory, manifest)
    return {"path": path, "manifest_sha256": manifest["_manifest_sha256"]}


def _resolve_queue(conn, item_ids: list[str], now: str, fingerprint: str) -> int:
    resolved = 0
    for row in _rows(conn, "governance_queue", "item_id", item_ids) if item_ids else []:
        item = _decode(row["data_json"], f"queue:{row['item_id']}")
        previous = {key: item.get(key) for key in ("status", "resolution", "resolved_at")}
        item.update({"status": "resolved", "resolution": "removed-generated-placeholder",
                     "resolved_at": now, "cleanup_fingerprint": fingerprint,
                     "previous_resolution": previous})
        conn.execute("UPDATE governance_queue SET data_json=?,updated_at=? WHERE item_id=?",
                     (json.dumps(item, ensure_ascii=False), now, row["item_id"]))
        resolved += 1
    return resolved


def cleanup_placeholder_claims(
    source_claim_ids: list[str], dry_run: bool = True, confirmation: str = ""
) -> dict:
    """Preview or atomically remove one exact set of generated placeholder claims."""
    plan = _plan(source_claim_ids)
    if dry_run:
        return {"dry_run": True, **plan, "confirmation_required": plan["eligible"]}
    if not plan["eligible"]:
        raise ValueError("Placeholder cleanup scope is ineligible: " + ",".join(plan["blockers"]))
    expected = plan["candidate_fingerprint"]
    if not confirmation or not hmac.compare_digest(str(confirmation), expected):
        raise ValueError(f"Placeholder cleanup requires the exact preview fingerprint: {expected}")
    from vector_lake.tool_projection import create_maintenance_backup
    backup_path = create_maintenance_backup("claim_placeholder_cleanup")
    backup = _validate_backup(backup_path, plan["runtime_generations"])
    now = datetime.now(timezone.utc).isoformat()
    with db_store.transaction() as conn:
        current = _plan(source_claim_ids)
        if not current["eligible"] or not hmac.compare_digest(current["candidate_fingerprint"], expected):
            raise RuntimeError("Placeholder cleanup scope changed after preview or backup validation.")
        old_ids = set(current["claim_ids"])
        governance_store._validate_operational_memory_delta_scope(old_ids, [], old_ids)
        old_rows = _rows(conn, "claims", "claim_id", current["claim_ids"])
        from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta
        sync_timeline_events_for_claim_delta(old_rows, [])
        # Generic delta refresh deliberately re-upserts forensic artifacts. This
        # explicit reviewed cleanup removes only the frozen, one-to-one archived
        # stub projections; ordinary forensic retention remains unchanged.
        conn.executemany(
            "DELETE FROM operational_memory WHERE memory_id=?",
            [(memory_id,) for memory_id in current["memory_ids"]],
        )
        remaining_memories = conn.execute(
            f"SELECT COUNT(*) FROM operational_memory WHERE memory_id IN ({','.join('?' for _ in current['memory_ids'])})",
            tuple(current["memory_ids"]),
        ).fetchone()[0]
        if remaining_memories:
            raise RuntimeError("placeholder_cleanup_memory_delete_incomplete")
        deleted_claims = sum(conn.execute("DELETE FROM claims WHERE claim_id=?", (item,)).rowcount
                             for item in current["claim_ids"])
        if deleted_claims != current["claim_count"]:
            raise RuntimeError("placeholder_cleanup_claim_delete_incomplete")
        resolved = _resolve_queue(conn, current["related_queue_ids"], now, expected)
        if conn.execute(f"SELECT COUNT(*) FROM claims WHERE claim_id IN ({','.join('?' for _ in old_ids)})", tuple(sorted(old_ids))).fetchone()[0]:
            raise RuntimeError("placeholder_cleanup_claim_delete_incomplete")
        _late_apply_hook()
    return {
        "dry_run": False, "committed": True, "candidate_fingerprint": expected,
        "deleted_claims": plan["claim_count"], "deleted_memories": plan["memory_count"],
        "resolved_governance_items": resolved, "preserved_claim_versions": True,
        "preserved_counts": plan["preserved_counts"],
        "backup": backup, "projection_rebuild_required": True,
        "operational_memory_search_index_pending": True,
    }
