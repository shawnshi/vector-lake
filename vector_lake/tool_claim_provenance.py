"""Preview-first reconstruction of missing claim provenance links.

The planner admits only evidence-preserving strategies:

* an existing evidence record whose text exactly equals the claim text and whose
  source/artifact lineage is complete;
* the single canonical source attached to a ``Source_*`` page, resolved to a
  verified artifact; or
* an explicitly frozen raw mapping bound to a source page, unique extraction
  lineage, or unique page-level ``Source_*`` link.

No claim text is created or reinterpreted. Ambiguous and unsupported candidates
remain untouched for governed research.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from collections import defaultdict
from pathlib import Path

from vector_lake import db_store, governance_store
from vector_lake.claim_extractor import _stable_id as _claim_stable_id
from vector_lake.evidence_foundation import (
    PROVENANCE_REPAIR_EXTRACTOR_NAME,
    PROVENANCE_REPAIR_EXTRACTOR_VERSION,
    claim_page_key,
    build_extraction_run,
    evidence_independence,
    resolve_source_artifact,
    source_locator_for,
    version_family_id,
)
from vector_lake.governance_metrics import (
    claim_governance_version,
    infer_claim_validity,
)
from vector_lake.wiki_utils import (
    get_memory_dir,
    get_wiki_dir,
    iter_wiki_link_matches,
    markdown_fenced_code_spans,
    read_markdown_file,
)

_CONTRACT = "claim-provenance-repair-plan-v1"
_SOURCE_MAP_CONTRACT = "claim-provenance-source-map/v1"


_OFFICIAL_MAP_CONTRACT = "claim-official-evidence-map/v1"


def _json_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _validate_claim_scope(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list) or not 1 <= len(value) <= 256
        or any(not isinstance(item, str) or not item or any(char.isspace() for char in item)
               for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("source_claim_ids must contain 1..256 unique nonempty whitespace-free IDs.")
    return sorted(value)


def _scoped_unsupported_claims(conn, scope, *, runtime_only: bool) -> dict[str, dict]:
    claims = _unsupported_claims(conn, runtime_only=runtime_only)
    if scope is None:
        return claims
    if set(scope) - claims.keys():
        raise ValueError("Exact scope contains missing or unsupported-ineligible claims.")
    return {claim_id: claims[claim_id] for claim_id in scope}


def _repair_state_basis(conn) -> dict[str, str]:
    # Conservative global drift fence: also covers equal-text lineage and runtime
    # rows not represented in claim_governance_version. No text is exported.
    return {
        table: _json_sha256(sorted(
            (dict(row) for row in conn.execute(f"SELECT * FROM {table}")),  # noqa: S608
            key=lambda row: json.dumps(row, sort_keys=True),
        ))
        for table in (
            "claims", "operational_memory", "sources", "evidence",
            "source_artifacts", "extraction_runs", "governance_queue", "canonical_identities",
        )
    }


def _validate_scoped_backup(backup: str, state_basis: dict) -> None:
    import sqlite3

    from vector_lake.tool_backup_retention import _verify_restorable_backup_snapshot
    from vector_lake.tool_projection import validate_maintenance_backup_v4

    directory = Path(backup)
    manifest, _inventory = validate_maintenance_backup_v4(directory / "manifest.json")
    if (
        manifest.get("restorable_as_consistent_canonical_projection_snapshot") is not True
        or (manifest.get("canonical_projection_consistency") or {}).get("status") != "verified"
        or "vector_lake.db" not in manifest.get("copied", [])
    ):
        raise ValueError("Scoped repair requires a complete consistent maintenance backup.")
    _verify_restorable_backup_snapshot(directory, manifest)
    uri = (directory / "vector_lake.db").resolve().as_uri() + "?mode=ro&immutable=1"
    backup_conn = sqlite3.connect(uri, uri=True)
    try:
        backup_conn.row_factory = sqlite3.Row
        if _repair_state_basis(backup_conn) != state_basis:
            raise ValueError("Maintenance backup does not contain the confirmed claim/memory basis.")
    finally:
        backup_conn.close()


def _required_text(record: dict, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Official evidence requires nonempty {key}.")
    return value


def _timestamp(value: object):
    from datetime import datetime

    if not isinstance(value, str):
        raise ValueError("Official evidence timestamp must include a timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid official evidence timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Official evidence timestamp must include a timezone.")
    return parsed


def _official_snapshot_record(snapshot: dict, claim: dict, review: dict, receipt: str) -> dict:
    from urllib.parse import urlsplit

    if not isinstance(snapshot, dict):
        raise ValueError("Official snapshot must be an object.")
    url = _required_text(snapshot, "original_url")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or any(char.isspace() for char in url)):
        raise ValueError("Official evidence original_url must be an absolute HTTPS URL.")
    retrieved = _timestamp(snapshot.get("retrieved_at"))
    reviewed = _timestamp(review.get("reviewed_at"))
    if retrieved > reviewed or reviewed > _timestamp(_utc_now()):
        raise ValueError("Official evidence retrieval/review timestamp order is invalid.")
    if snapshot.get("representation") not in {"original-http-body", "verbatim-text-extraction"}:
        raise ValueError("Official evidence representation cannot be summary or generated prose.")
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Official evidence requires preservation metadata.")
    for key in ("validator", "consent", "classification", "retention_policy"):
        _required_text(metadata, key)
    parents = metadata.get("generation_parent_refs")
    if not isinstance(parents, list) or any(not isinstance(ref, str) or not ref.strip() for ref in parents):
        raise ValueError("Official evidence requires explicit generation_parent_refs.")
    if "expires_at" not in metadata or "revoked_at" not in metadata:
        raise ValueError("Official evidence requires explicit expiry/revocation metadata.")
    if metadata["revoked_at"] is not None:
        raise ValueError("Revoked official evidence cannot support a repair.")
    if metadata["expires_at"] is not None and _timestamp(metadata["expires_at"]) <= _timestamp(_utc_now()):
        raise ValueError("Expired official evidence cannot support a repair.")
    attestation = snapshot.get("semantic_review")
    if (not isinstance(attestation, dict)
            or attestation.get("supports_claim") is not True
            or attestation.get("original_source_verified") is not True):
        raise ValueError("Official evidence requires semantic support and original-source attestation.")
    _required_text(attestation, "rationale")
    raw_ref = _required_text(snapshot, "raw_ref")
    memory = get_memory_dir().resolve()
    research = memory / "raw" / "research"
    raw_path = (memory / raw_ref).resolve()
    if (not research.is_relative_to(memory) or not raw_ref.startswith("raw/research/")
            or "\\" in raw_ref or ".." in Path(raw_ref).parts
            or not raw_path.is_relative_to(research) or not raw_path.is_file()):
        raise ValueError("Official snapshot must be inside effective MEMORY/raw/research.")
    data = raw_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if (snapshot.get("raw_sha256") != digest
            or type(snapshot.get("byte_size")) is not int
            or snapshot["byte_size"] != len(data)):
        raise ValueError("Official snapshot hash/byte size changed after freeze.")
    excerpt = _required_text(snapshot, "quoted_excerpt")
    if excerpt.encode("utf-8") not in data:
        raise ValueError("Official quoted excerpt is absent from the frozen snapshot bytes.")
    # This metadata describes a preserved representation, never a claim-text
    # surrogate. Semantic truth remains an operator attestation, not a byte test.
    provenance = {key: copy.deepcopy(value) for key, value in snapshot.items()
                  if key not in {"quoted_excerpt", "semantic_review"}}
    source_id = _claim_stable_id("source", "official:" + _json_sha256(provenance))
    artifact = resolve_source_artifact(raw_ref, source_id=source_id, metadata=metadata)
    if artifact.get("content_hash") != digest or artifact.get("byte_size") != len(data):
        raise ValueError("Official snapshot changed during validation.")
    artifact["official_snapshot"] = provenance
    source = {**copy.deepcopy(artifact), "source_type": "official-snapshot",
              "original_url": url, "retrieved_at": snapshot["retrieved_at"],
              "representation": snapshot["representation"], "official_snapshot": provenance}
    claim_id = claim["claim_id"]
    page_key = claim_page_key(claim)
    run = build_extraction_run(
        page_key=page_key, body=excerpt, artifact_ids=[artifact["artifact_id"]],
        frontmatter={}, extractor_name=PROVENANCE_REPAIR_EXTRACTOR_NAME, extractor_version=PROVENANCE_REPAIR_EXTRACTOR_VERSION,
    )
    run["review_receipt_sha256"] = receipt
    # Include receipt so separate reviews on the same page cannot overwrite runs.
    run["run_id"] = _claim_stable_id("extractrun", run["run_id"] + receipt + claim_id)
    evidence_id = _claim_stable_id("evidence", "official:" + claim_id + receipt + _json_sha256(snapshot))
    locator = copy.deepcopy(claim.get("locator") or {"page_key": page_key})
    evidence = {
        "evidence_id": evidence_id, "evidence_family_id": evidence_id,
        "source_id": source_id, "artifact_id": artifact["artifact_id"],
        "locator": locator, "projection_locator": locator,
        "source_locator": {**source_locator_for({}, raw_ref, artifact=artifact),
                           "original_url": url, "representation": snapshot["representation"],
                           "quoted_excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()},
        "evidence_text": excerpt, "evidence_type": "operator-reviewed-official-excerpt",
        "extraction_run_id": run["run_id"], "supports_claim_ids": [claim_id],
        "contradicts_claim_ids": [],
        "official_review": {"claim_id": claim_id, "claim_version": claim_governance_version(claim),
                            "source_id": source_id, "receipt_sha256": receipt,
                            "review": copy.deepcopy(review), "semantic_review": attestation},
        **evidence_independence(raw_ref, f"{page_key}.md", parents),
    }
    return {"source": source, "artifact": artifact, "evidence": evidence, "run": run}


def _build_official_evidence_plan(scope: list[str], map_path: str, *, runtime_only: bool) -> dict:
    path = Path(map_path).expanduser().resolve()
    if not path.is_relative_to(get_memory_dir().resolve()) or not path.is_file():
        raise ValueError("Official evidence map must be an existing file inside MEMORY.")
    map_bytes = path.read_bytes()
    payload = json.loads(map_bytes.decode("utf-8"))
    if (not isinstance(payload, dict) or set(payload) != {"contract", "entries"}
            or payload.get("contract") != _OFFICIAL_MAP_CONTRACT
            or not isinstance(payload.get("entries"), list)):
        raise ValueError(f"Official evidence map must use {_OFFICIAL_MAP_CONTRACT}.")
    entries = payload["entries"]
    if (len(entries) != len(scope) or any(not isinstance(entry, dict) for entry in entries)
            or sorted(str(entry.get("claim_id")) for entry in entries) != scope):
        raise ValueError("Official evidence map must cover exactly the complete claim scope.")
    db_store.init_db()
    conn = db_store.get_connection()
    claims = _scoped_unsupported_claims(conn, scope, runtime_only=runtime_only)
    state_basis = _repair_state_basis(conn)
    candidates = []
    planned_artifacts: dict[str, dict] = {}
    for entry in sorted(entries, key=lambda entry: entry["claim_id"]):
        claim = claims[entry["claim_id"]]
        if (entry.get("expected_claim_version") != claim_governance_version(claim)
                or entry.get("expected_claim_sha256") != _json_sha256(claim)):
            raise ValueError("Official evidence expected claim version/hash changed.")
        review = entry.get("review")
        if not isinstance(review, dict):
            raise ValueError("Official evidence requires an operator review receipt.")
        for key in ("actor_id", "method_version", "purpose", "reviewed_at"):
            _required_text(review, key)
        receipt = entry.get("review_receipt_sha256")
        if receipt != _json_sha256({key: value for key, value in entry.items()
                                   if key != "review_receipt_sha256"}):
            raise ValueError("Official evidence review receipt hash mismatch.")
        snapshots = entry.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            raise ValueError("Official evidence requires primary snapshots for every claim.")
        records = [_official_snapshot_record(snapshot, claim, review, receipt) for snapshot in snapshots]
        for record in records:
            # Retain the complete hash preimage in the existing extraction ledger
            # so later audits do not depend on a mutable external map file.
            record["run"]["review_receipt"] = copy.deepcopy(entry)
        if len({record["source"]["source_id"] for record in records}) != len(records):
            raise ValueError("Official evidence snapshots must be distinct per claim.")
        for record in records:
            for kind, table, id_field in (("source", "sources", "source_id"),
                                          ("artifact", "source_artifacts", "artifact_id"),
                                          ("evidence", "evidence", "evidence_id"),
                                          ("run", "extraction_runs", "run_id")):
                proposed = record[kind]
                row = conn.execute(f"SELECT data_json FROM {table} WHERE {id_field} = ?",  # noqa: S608
                                   (proposed[id_field],)).fetchone()
                if row is not None:
                    existing = json.loads(row["data_json"])
                    if kind in {"evidence", "run"} or any(existing.get(key) != value for key, value in proposed.items()):
                        raise ValueError("Official evidence collides with existing provenance; review separately.")
                    record[kind] = existing
            artifact = record["artifact"]
            previous = planned_artifacts.setdefault(artifact["artifact_id"], artifact)
            if previous != artifact:
                raise ValueError("Conflicting official snapshot metadata for identical artifact bytes.")
        candidates.append({
            "claim_id": claim["claim_id"], "claim_version": claim_governance_version(claim),
            "page_key": claim_page_key(claim),
            "source_ids": sorted(record["source"]["source_id"] for record in records),
            "evidence_ids": sorted(record["evidence"]["evidence_id"] for record in records),
            "existing_evidence_ids": [], "source_page_record": None,
            "official_evidence_records": records, "strategies": ["frozen-official-evidence-map"],
        })
    map_hash = "sha256:" + hashlib.sha256(map_bytes).hexdigest()
    fingerprint = "sha256:" + _json_sha256({
        "contract": _OFFICIAL_MAP_CONTRACT, "source_claim_ids": scope,
        "runtime_only": runtime_only, "map_sha256": map_hash,
        "state_basis": state_basis, "candidates": candidates,
    })
    return {
        "source_claim_ids": scope, "state_basis": state_basis, "runtime_only": runtime_only,
        "official_evidence_map_sha256": map_hash, "unsupported_claims": len(scope),
        "repairable_claims": len(scope), "repairable_pages": len({c["page_key"] for c in candidates}),
        "unresolved_claims": 0, "unresolved_pages": 0,
        "candidate_fingerprint": fingerprint, "candidate_claim_ids": scope,
        "unresolved_claim_ids": [], "candidates": candidates,
    }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _runtime_unsupported_claim_ids(conn) -> set[str]:
    return {
        str(row["claim_id"] or "")
        for row in conn.execute(
            "SELECT DISTINCT json_extract(data_json, '$.source_claim_id') AS claim_id "
            "FROM operational_memory "
            "WHERE json_extract(data_json, '$.validity_state') = 'unsupported'"
        )
        if str(row["claim_id"] or "")
    }


def _unsupported_claims(conn, *, runtime_only: bool) -> dict[str, dict]:
    runtime_claim_ids = _runtime_unsupported_claim_ids(conn) if runtime_only else set()
    claims: dict[str, dict] = {}
    for row in conn.execute("SELECT claim_id, data_json FROM claims"):
        claim_id = str(row["claim_id"] or "")
        if runtime_only and claim_id not in runtime_claim_ids:
            continue
        claim = json.loads(row["data_json"])
        if infer_claim_validity(claim).get("validity_state") == "unsupported":
            claims[claim_id] = claim
    return claims


def _canonical_records(conn, table: str, id_field: str) -> dict[str, dict]:
    return {
        str(row[id_field]): json.loads(row["data_json"])
        for row in conn.execute(
            f"SELECT {id_field}, data_json FROM {table}"  # noqa: S608 - fixed callers
        )
    }


def _load_source_map(
    source_map_path: str,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict], str]:
    if not source_map_path:
        return {}, {}, {}, ""
    memory_dir = get_memory_dir().resolve()
    raw_dir = (memory_dir / "raw").resolve()
    path = Path(source_map_path).expanduser().resolve()
    if not path.is_relative_to(memory_dir) or not path.is_file():
        raise ValueError("Source map must be an existing file inside MEMORY.")
    payload_bytes = path.read_bytes()
    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("contract") != _SOURCE_MAP_CONTRACT:
        raise ValueError(f"Source map must use contract {_SOURCE_MAP_CONTRACT!r}.")
    raw_page_entries = payload.get("entries", [])
    raw_lineage_entries = payload.get("lineage_entries", [])
    raw_linked_page_entries = payload.get("linked_page_entries", [])
    if (
        not isinstance(raw_page_entries, list)
        or not isinstance(raw_lineage_entries, list)
        or not isinstance(raw_linked_page_entries, list)
    ):
        raise ValueError(
            "Source map entries, lineage_entries, and linked_page_entries "
            "must be lists."
        )
    if not raw_page_entries and not raw_lineage_entries and not raw_linked_page_entries:
        raise ValueError("Source map must include at least one mapping entry.")

    def validated_entry(raw_entry: dict, *, key_field: str) -> tuple[str, dict]:
        if not isinstance(raw_entry, dict):
            raise ValueError("Each source map entry must be an object.")
        source_key = str(raw_entry.get(key_field) or "").strip()
        raw_ref = str(raw_entry.get("raw_ref") or "").strip().replace("\\", "/")
        raw_sha256 = str(raw_entry.get("raw_sha256") or "").strip().lower()
        expected_claims = raw_entry.get("expected_unsupported_claims")
        if (
            not source_key.startswith("Source_")
            or not raw_ref.startswith("raw/")
            or len(raw_sha256) != 64
            or isinstance(expected_claims, bool)
            or not isinstance(expected_claims, int)
            or expected_claims < 1
        ):
            raise ValueError(
                f"Invalid source map {key_field} entry {source_key!r}."
            )
        raw_path = (memory_dir / raw_ref).resolve()
        if not raw_path.is_relative_to(raw_dir) or not raw_path.is_file():
            raise ValueError(f"Mapped raw source {raw_ref!r} is unavailable.")
        raw_bytes = raw_path.read_bytes()
        if (
            hashlib.sha256(raw_bytes).hexdigest() != raw_sha256
            or len(raw_bytes) != int(raw_entry.get("byte_size") or -1)
        ):
            raise ValueError(f"Mapped raw source {raw_ref!r} changed after freeze.")
        return source_key, {
            key_field: source_key,
            "raw_ref": raw_ref,
            "raw_sha256": raw_sha256,
            "byte_size": len(raw_bytes),
            "expected_unsupported_claims": expected_claims,
        }

    page_entries: dict[str, dict] = {}
    for raw_entry in raw_page_entries:
        page_key, entry = validated_entry(raw_entry, key_field="page_key")
        if page_key in page_entries:
            raise ValueError(f"Duplicate source map page {page_key!r}.")
        page_entries[page_key] = entry

    lineage_entries: dict[str, dict] = {}
    for raw_entry in raw_lineage_entries:
        source_ref, entry = validated_entry(raw_entry, key_field="source_ref")
        if source_ref in lineage_entries:
            raise ValueError(f"Duplicate source map lineage {source_ref!r}.")
        lineage_entries[source_ref] = entry

    linked_page_entries: dict[str, dict] = {}
    for raw_entry in raw_linked_page_entries:
        source_ref, entry = validated_entry(raw_entry, key_field="source_ref")
        page_key = str(raw_entry.get("page_key") or "").strip()
        if not page_key or page_key.startswith("Source_"):
            raise ValueError(f"Invalid linked source map page {page_key!r}.")
        if page_key in linked_page_entries:
            raise ValueError(f"Duplicate linked source map page {page_key!r}.")
        linked_page_entries[page_key] = {
            **entry,
            "page_key": page_key,
            "source_ref": source_ref,
        }
    return (
        page_entries,
        lineage_entries,
        linked_page_entries,
        "sha256:" + hashlib.sha256(payload_bytes).hexdigest(),
    )

def _mapped_source_page_candidate(
    claim: dict,
    *,
    page_key: str,
    mapping: dict,
    sources_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
) -> dict | None:
    page_path = get_wiki_dir() / f"{page_key}.md"
    if not page_path.is_file():
        return None
    frontmatter, _body, _raw = read_markdown_file(page_path)
    if str(frontmatter.get("type") or "").strip().lower() != "source":
        return None

    raw_ref = str(mapping["raw_ref"])
    source_id = _claim_stable_id("source", raw_ref)
    artifact = resolve_source_artifact(raw_ref, source_id=source_id)
    if (
        artifact.get("integrity_status") != "verified"
        or str(artifact.get("content_hash") or "") != str(mapping["raw_sha256"])
        or int(artifact.get("byte_size") or -1) != int(mapping["byte_size"])
    ):
        return None
    existing_source = sources_by_id.get(source_id)
    if existing_source is not None and str(existing_source.get("raw_ref") or "") != raw_ref:
        return None
    source = existing_source or {
        "source_id": source_id,
        "raw_ref": raw_ref,
        "canonical_source_page": page_path.name,
        "source_type": Path(raw_ref).suffix.lstrip(".").lower() or "file",
        "title": str(frontmatter.get("title") or page_key),
        "ingested_at": str(
            frontmatter.get("created")
            or frontmatter.get("updated")
            or claim.get("created_at")
            or "1970-01-01T00:00:00+00:00"
        ),
        "artifact_id": artifact["artifact_id"],
        "content_hash": artifact["content_hash"],
        "hash_algorithm": artifact["hash_algorithm"],
        "byte_size": artifact["byte_size"],
        "mime_type": artifact["mime_type"],
        "storage_uri": artifact["storage_uri"],
        "integrity_status": artifact["integrity_status"],
        "classification": artifact["classification"],
        "retention_policy": artifact["retention_policy"],
        "legal_hold": artifact["legal_hold"],
        "lineage_id": artifact["lineage_id"],
        "generation_parent_refs": artifact["generation_parent_refs"],
    }
    evidence_id = _claim_stable_id(
        "evidence", f"{page_key}:{raw_ref}:{claim.get('claim_text') or ''}"
    )
    collision = evidence_by_id.get(evidence_id)
    if collision is not None and (
        str(collision.get("evidence_text") or "")
        != str(claim.get("claim_text") or "")
        or str(collision.get("source_id") or "") != source_id
    ):
        return None
    return {
        "source_id": source_id,
        "artifact_id": str(artifact["artifact_id"]),
        "artifact_hash": str(artifact["content_hash"]),
        "evidence_id": evidence_id,
        "bootstrap_source": existing_source is None,
        "source_record": source,
        "artifact_record": artifact,
        "mapping_kind": "frozen-raw-source-map",
    }


def _claim_extraction_source_refs(
    claim: dict,
    *,
    extraction_runs_by_id: dict[str, dict],
    artifacts_by_id: dict[str, dict],
) -> list[str]:
    run_id = str(claim.get("extraction_run_id") or "")
    run = extraction_runs_by_id.get(run_id) or {}
    artifact_ids = (
        run.get("artifact_ids")
        or run.get("source_artifact_ids")
        or run.get("input_artifact_ids")
        or []
    )
    refs = {
        str((artifacts_by_id.get(str(artifact_id)) or {}).get("raw_ref") or "")
        for artifact_id in artifact_ids
    }
    return sorted(ref for ref in refs if ref)


def _mapped_extraction_lineage_candidate(
    claim: dict,
    *,
    page_key: str,
    source_ref: str,
    mapping: dict,
    sources_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
) -> dict | None:
    source_page_path = get_wiki_dir() / f"{source_ref}.md"
    if not source_page_path.is_file():
        return None
    frontmatter, _body, _raw = read_markdown_file(source_page_path)
    if str(frontmatter.get("type") or "").strip().lower() != "source":
        return None

    raw_ref = str(mapping["raw_ref"])
    source_id = _claim_stable_id("source", raw_ref)
    artifact = resolve_source_artifact(raw_ref, source_id=source_id)
    if (
        artifact.get("integrity_status") != "verified"
        or str(artifact.get("content_hash") or "") != str(mapping["raw_sha256"])
        or int(artifact.get("byte_size") or -1) != int(mapping["byte_size"])
    ):
        return None
    existing_source = sources_by_id.get(source_id)
    if existing_source is not None and str(existing_source.get("raw_ref") or "") != raw_ref:
        return None
    source = existing_source or {
        "source_id": source_id,
        "raw_ref": raw_ref,
        "canonical_source_page": source_page_path.name,
        "source_type": Path(raw_ref).suffix.lstrip(".").lower() or "file",
        "title": str(frontmatter.get("title") or source_ref),
        "ingested_at": str(
            frontmatter.get("created")
            or frontmatter.get("updated")
            or claim.get("created_at")
            or "1970-01-01T00:00:00+00:00"
        ),
        "artifact_id": artifact["artifact_id"],
        "content_hash": artifact["content_hash"],
        "hash_algorithm": artifact["hash_algorithm"],
        "byte_size": artifact["byte_size"],
        "mime_type": artifact["mime_type"],
        "storage_uri": artifact["storage_uri"],
        "integrity_status": artifact["integrity_status"],
        "classification": artifact["classification"],
        "retention_policy": artifact["retention_policy"],
        "legal_hold": artifact["legal_hold"],
        "lineage_id": artifact["lineage_id"],
        "generation_parent_refs": artifact["generation_parent_refs"],
    }
    evidence_id = _claim_stable_id(
        "evidence",
        f"{page_key}:{source_ref}:{raw_ref}:{claim.get('claim_text') or ''}",
    )
    collision = evidence_by_id.get(evidence_id)
    if collision is not None and (
        str(collision.get("evidence_text") or "")
        != str(claim.get("claim_text") or "")
        or str(collision.get("source_id") or "") != source_id
    ):
        return None
    return {
        "source_id": source_id,
        "artifact_id": str(artifact["artifact_id"]),
        "artifact_hash": str(artifact["content_hash"]),
        "evidence_id": evidence_id,
        "bootstrap_source": existing_source is None,
        "source_record": source,
        "artifact_record": artifact,
        "mapping_kind": "frozen-extraction-lineage-source-map",
        "source_ref_hash": hashlib.sha256(source_ref.encode("utf-8")).hexdigest(),
    }


_SOURCE_FOOTNOTE_LABEL_RE = re.compile(r"\[\^(Source_[^\]\r\n]+)\]")
_SOURCE_FOOTNOTE_DEFINITION_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[\^(Source_[^\]\r\n]+)\]:[ \t]*(?P<body>[^\r\n]*)"
)
_SOURCE_PAGE_FILENAME_RE = re.compile(
    r"(?<![\w./\\-])(Source_[^\s\]\[()<>\"'`|]+?)\.md(?![\w/\\-])"
)


def _normalize_source_page_ref(value: str) -> str:
    from vector_lake.source_references import normalize_explicit_source_page_ref

    return normalize_explicit_source_page_ref(value)


def _inside_markdown_spans(spans: list[tuple[int, int]], offset: int) -> bool:
    return any(start <= offset < end for start, end in spans)


def _inside_inline_code_span(content: str, start: int) -> bool:
    line_start = content.rfind("\n", 0, start) + 1
    prefix = content[line_start:start]
    delimiter = None
    for match in re.finditer(r"`+", prefix):
        width = len(match.group(0))
        if delimiter is None:
            delimiter = width
        elif width == delimiter:
            delimiter = None
    return delimiter is not None


def _excluded_markdown_ref(content: str, spans: list[tuple[int, int]], start: int) -> bool:
    return _inside_markdown_spans(spans, start) or _inside_inline_code_span(content, start)


def _source_wiki_link_ref(target: str) -> tuple[bool, str]:
    raw_target = str(target or "").strip()
    if not raw_target.startswith("Source_"):
        return False, ""
    return True, _normalize_source_page_ref(raw_target)


def _has_unsupported_source_footnote_continuation(
    content: str,
    definition_end: int,
    spans: list[tuple[int, int]],
) -> bool:
    offset = definition_end
    if offset < len(content) and content[offset] == "\r":
        offset += 1
    if offset < len(content) and content[offset] == "\n":
        offset += 1
    while offset < len(content):
        if _inside_markdown_spans(spans, offset):
            return False
        line_end = content.find("\n", offset)
        if line_end == -1:
            line_end = len(content)
        line = content[offset:line_end].rstrip("\r")
        if not line.strip():
            offset = line_end + 1
            continue
        return line.startswith(("    ", "\t"))
    return False


def _source_refs_in_footnote_definition_body(body: str) -> set[str] | None:
    refs: set[str] = set()
    spans = markdown_fenced_code_spans(body)
    for match in iter_wiki_link_matches(body):
        is_source_link, source_ref = _source_wiki_link_ref(match.group(1))
        if is_source_link and not source_ref:
            return None
        if source_ref:
            refs.add(source_ref)
    for match in _SOURCE_FOOTNOTE_LABEL_RE.finditer(body):
        if _excluded_markdown_ref(body, spans, match.start()):
            continue
        source_ref = _normalize_source_page_ref(match.group(1))
        if not source_ref:
            return None
        refs.add(source_ref)
    for match in _SOURCE_PAGE_FILENAME_RE.finditer(body):
        if _excluded_markdown_ref(body, spans, match.start()):
            continue
        source_ref = _normalize_source_page_ref(match.group(1))
        if not source_ref:
            return None
        refs.add(source_ref)
    return refs


def _linked_page_source_refs(body: str) -> set[str] | None:
    refs: set[str] = set()
    footnote_refs: set[str] = set()
    footnote_definitions: dict[str, list[str]] = defaultdict(list)
    spans = markdown_fenced_code_spans(body)
    for match in iter_wiki_link_matches(body):
        is_source_link, source_ref = _source_wiki_link_ref(match.group(1))
        if is_source_link and not source_ref:
            return None
        if source_ref:
            refs.add(source_ref)
    for match in _SOURCE_FOOTNOTE_DEFINITION_RE.finditer(body):
        if _excluded_markdown_ref(body, spans, match.start()):
            continue
        if _has_unsupported_source_footnote_continuation(body, match.end(), spans):
            return None
        source_ref = _normalize_source_page_ref(match.group(1))
        if not source_ref:
            return None
        refs.add(source_ref)
        footnote_refs.add(source_ref)
        footnote_definitions[source_ref].append(match.group("body"))
    for match in _SOURCE_FOOTNOTE_LABEL_RE.finditer(body):
        if _excluded_markdown_ref(body, spans, match.start()):
            continue
        source_ref = _normalize_source_page_ref(match.group(1))
        if not source_ref:
            return None
        refs.add(source_ref)
        footnote_refs.add(source_ref)
    if footnote_refs - set(footnote_definitions):
        return None
    for source_ref, definitions in footnote_definitions.items():
        if len(definitions) != 1:
            return None
        definition_refs = _source_refs_in_footnote_definition_body(definitions[0])
        if definition_refs is None:
            return None
        if any(definition_ref != source_ref for definition_ref in definition_refs):
            return None
        refs.update(definition_refs)
    return refs


def _mapped_linked_page_candidate(
    claim: dict,
    *,
    page_key: str,
    source_ref: str,
    mapping: dict,
    sources_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
) -> dict | None:
    page_path = get_wiki_dir() / f"{page_key}.md"
    if not page_path.is_file():
        return None
    _frontmatter, body, _raw = read_markdown_file(page_path)
    linked_source_refs = _linked_page_source_refs(body)
    if linked_source_refs != {source_ref}:
        return None
    candidate = _mapped_extraction_lineage_candidate(
        claim,
        page_key=page_key,
        source_ref=source_ref,
        mapping=mapping,
        sources_by_id=sources_by_id,
        evidence_by_id=evidence_by_id,
    )
    if candidate is not None:
        candidate["mapping_kind"] = "frozen-linked-page-source-map"
    return candidate


def _qualifying_evidence_by_text(
    evidence_by_id: dict[str, dict],
    *,
    target_texts: set[str],
    sources_by_id: dict[str, dict],
    artifacts_by_id: dict[str, dict],
) -> dict[str, list[dict]]:
    matches: dict[str, list[dict]] = defaultdict(list)
    for evidence in evidence_by_id.values():
        evidence_text = str(evidence.get("evidence_text") or "")
        source_id = str(evidence.get("source_id") or "")
        artifact_id = str(evidence.get("artifact_id") or "")
        if (
            evidence_text in target_texts
            and source_id in sources_by_id
            and artifact_id in artifacts_by_id
            and isinstance(evidence.get("source_locator"), dict)
            and evidence.get("lineage_safe") is True
        ):
            matches[evidence_text].append(evidence)
    return matches


def _source_page_candidate(
    claim: dict,
    *,
    page_key: str,
    source_ids_by_page: dict[str, set[str]],
    sources_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
) -> dict | None:
    page_source_ids = source_ids_by_page.get(page_key, set())
    if not page_key.startswith("Source_") or len(page_source_ids) != 1:
        return None
    source_id = next(iter(page_source_ids))
    source = sources_by_id[source_id]
    raw_ref = str(source.get("raw_ref") or "")
    if not raw_ref:
        return None
    artifact = resolve_source_artifact(raw_ref, source_id=source_id, metadata=source)
    if (
        not artifact.get("artifact_id")
        or not artifact.get("content_hash")
        or artifact.get("integrity_status") != "verified"
    ):
        return None
    evidence_id = _claim_stable_id(
        "evidence",
        f"{page_key}:{raw_ref}:{claim.get('claim_text') or ''}",
    )
    existing = evidence_by_id.get(evidence_id)
    if existing is not None and (
        str(existing.get("source_id") or "") != source_id
        or str(existing.get("evidence_text") or "")
        != str(claim.get("claim_text") or "")
    ):
        return None
    return {
        "source_id": source_id,
        "evidence_id": evidence_id,
        "artifact_id": str(artifact["artifact_id"]),
        "artifact_hash": str(artifact["content_hash"]),
        "raw_ref_hash": hashlib.sha256(raw_ref.encode("utf-8")).hexdigest(),
    }


def build_claim_provenance_repair_plan(
    *, runtime_only: bool = True, source_map_path: str = "",
    source_claim_ids: list[str] | None = None,
    official_evidence_map_path: str = "",
) -> dict:
    """Return a content-addressed plan without exposing claim or source text."""
    scope = _validate_claim_scope(source_claim_ids)
    if official_evidence_map_path:
        if scope is None or source_map_path:
            raise ValueError("Official evidence requires an exact scope and no legacy source map.")
        return _build_official_evidence_plan(
            scope, official_evidence_map_path, runtime_only=runtime_only
        )
    (
        source_map,
        lineage_source_map,
        linked_page_source_map,
        source_map_sha256,
    ) = _load_source_map(source_map_path)
    db_store.init_db()
    conn = db_store.get_connection()
    claims_by_id = _scoped_unsupported_claims(conn, scope, runtime_only=runtime_only)
    unsupported_by_page: dict[str, int] = defaultdict(int)
    for claim in claims_by_id.values():
        unsupported_by_page[claim_page_key(claim)] += 1
    for page_key, mapping in {
        **source_map,
        **linked_page_source_map,
    }.items():
        actual = unsupported_by_page.get(page_key, 0)
        expected = int(mapping["expected_unsupported_claims"])
        if actual != expected:
            raise RuntimeError(
                f"Source map count changed for {page_key!r}: "
                f"expected {expected}, found {actual}."
            )
    sources_by_id = _canonical_records(conn, "sources", "source_id")
    evidence_by_id = _canonical_records(conn, "evidence", "evidence_id")
    artifacts_by_id = _canonical_records(conn, "source_artifacts", "artifact_id")
    extraction_runs_by_id = _canonical_records(
        conn, "extraction_runs", "run_id"
    )
    target_texts = {
        str(claim.get("claim_text") or "") for claim in claims_by_id.values()
    }
    evidence_by_text = _qualifying_evidence_by_text(
        evidence_by_id,
        target_texts=target_texts,
        sources_by_id=sources_by_id,
        artifacts_by_id=artifacts_by_id,
    )
    source_ids_by_page: dict[str, set[str]] = defaultdict(set)
    for source_id, source in sources_by_id.items():
        page_key = Path(str(source.get("canonical_source_page") or "")).stem
        if page_key:
            source_ids_by_page[page_key].add(source_id)

    source_page_records: dict[str, dict] = {}
    mapped_candidate_counts: dict[str, int] = defaultdict(int)
    mapped_lineage_counts: dict[str, int] = defaultdict(int)
    mapped_linked_page_counts: dict[str, int] = defaultdict(int)
    planned_source_evidence_by_text: dict[str, list[dict]] = defaultdict(list)
    for claim_id, claim in sorted(claims_by_id.items()):
        page_key = claim_page_key(claim)
        page_mapping = source_map.get(page_key)
        linked_page_mapping = linked_page_source_map.get(page_key)
        lineage_ref = ""
        if page_mapping is not None:
            source_page_record = _mapped_source_page_candidate(
                claim,
                page_key=page_key,
                mapping=page_mapping,
                sources_by_id=sources_by_id,
                evidence_by_id=evidence_by_id,
            )
        elif linked_page_mapping is not None:
            source_page_record = _mapped_linked_page_candidate(
                claim,
                page_key=page_key,
                source_ref=str(linked_page_mapping["source_ref"]),
                mapping=linked_page_mapping,
                sources_by_id=sources_by_id,
                evidence_by_id=evidence_by_id,
            )
        else:
            extraction_refs = _claim_extraction_source_refs(
                claim,
                extraction_runs_by_id=extraction_runs_by_id,
                artifacts_by_id=artifacts_by_id,
            )
            if len(extraction_refs) == 1 and extraction_refs[0] in lineage_source_map:
                lineage_ref = extraction_refs[0]
                source_page_record = _mapped_extraction_lineage_candidate(
                    claim,
                    page_key=page_key,
                    source_ref=lineage_ref,
                    mapping=lineage_source_map[lineage_ref],
                    sources_by_id=sources_by_id,
                    evidence_by_id=evidence_by_id,
                )
            else:
                source_page_record = _source_page_candidate(
                    claim,
                    page_key=page_key,
                    source_ids_by_page=source_ids_by_page,
                    sources_by_id=sources_by_id,
                    evidence_by_id=evidence_by_id,
                )
        if source_page_record is not None:
            source_page_records[claim_id] = source_page_record
            if page_mapping is not None:
                mapped_candidate_counts[page_key] += 1
            if linked_page_mapping is not None:
                mapped_linked_page_counts[page_key] += 1
            if lineage_ref:
                mapped_lineage_counts[lineage_ref] += 1
            planned_source_evidence_by_text[
                str(claim.get("claim_text") or "")
            ].append(source_page_record)
    for page_key, mapping in source_map.items():
        expected = int(mapping["expected_unsupported_claims"])
        actual = mapped_candidate_counts.get(page_key, 0)
        if actual != expected:
            raise RuntimeError(
                f"Source map could not produce every candidate for {page_key!r}: "
                f"expected {expected}, planned {actual}."
            )
    for source_ref, mapping in lineage_source_map.items():
        expected = int(mapping["expected_unsupported_claims"])
        actual = mapped_lineage_counts.get(source_ref, 0)
        if actual != expected:
            raise RuntimeError(
                f"Source map could not produce every lineage candidate for "
                f"{source_ref!r}: expected {expected}, planned {actual}."
            )
    for page_key, mapping in linked_page_source_map.items():
        expected = int(mapping["expected_unsupported_claims"])
        actual = mapped_linked_page_counts.get(page_key, 0)
        if actual != expected:
            raise RuntimeError(
                f"Source map could not produce every linked-page candidate for "
                f"{page_key!r}: expected {expected}, planned {actual}."
            )

    candidates: list[dict] = []
    unresolved_claim_ids: list[str] = []
    for claim_id, claim in sorted(claims_by_id.items()):
        page_key = claim_page_key(claim)
        claim_text = str(claim.get("claim_text") or "")
        exact_evidence = evidence_by_text.get(claim_text, [])
        planned_evidence = planned_source_evidence_by_text.get(claim_text, [])
        evidence_ids = {
            str(record["evidence_id"])
            for record in [*exact_evidence, *planned_evidence]
        }
        source_ids = {
            str(record["source_id"])
            for record in [*exact_evidence, *planned_evidence]
        }
        strategies = ["exact-existing-evidence"] if exact_evidence else []
        source_page_record = source_page_records.get(claim_id)
        if planned_evidence:
            strategies.append("exact-planned-source-page-evidence")
        if source_page_record is not None:
            mapping_kind = source_page_record.get("mapping_kind")
            if mapping_kind == "frozen-raw-source-map":
                strategies.append("frozen-raw-source-map")
            elif mapping_kind == "frozen-extraction-lineage-source-map":
                strategies.append("frozen-extraction-lineage-source-map")
            elif mapping_kind == "frozen-linked-page-source-map":
                strategies.append("frozen-linked-page-source-map")
            else:
                strategies.append("unique-canonical-source-page")
        if not source_ids or not evidence_ids:
            unresolved_claim_ids.append(claim_id)
            continue
        candidates.append(
            {
                "claim_id": claim_id,
                "claim_version": claim_governance_version(claim),
                "page_key": page_key,
                "source_ids": sorted(source_ids),
                "evidence_ids": sorted(evidence_ids),
                "existing_evidence_ids": sorted(
                    evidence_id
                    for evidence_id in evidence_ids
                    if evidence_id in evidence_by_id
                ),
                "source_page_record": source_page_record,
                "strategies": sorted(set(strategies)),
            }
        )

    if scope is not None and unresolved_claim_ids:
        raise ValueError("Exact scope is not completely repairable; partial scopes are forbidden.")
    state_basis = _repair_state_basis(conn) if scope is not None else None
    fingerprint_basis = {
        "source_claim_ids": scope,
        "state_basis": state_basis,
        "contract": _CONTRACT,
        "runtime_only": bool(runtime_only),
        "source_map_sha256": source_map_sha256,
        "candidates": candidates,
        "unresolved_claim_ids": unresolved_claim_ids,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            fingerprint_basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    candidate_pages = {item["page_key"] for item in candidates}
    unresolved_pages = {
        claim_page_key(claims_by_id[claim_id])
        for claim_id in unresolved_claim_ids
    }
    return {
        "source_claim_ids": scope,
        "state_basis": state_basis,
        "runtime_only": bool(runtime_only),
        "source_map_sha256": source_map_sha256,
        "source_map_entries": (
            len(source_map)
            + len(lineage_source_map)
            + len(linked_page_source_map)
        ),
        "source_map_page_entries": len(source_map),
        "source_map_lineage_entries": len(lineage_source_map),
        "source_map_linked_page_entries": len(linked_page_source_map),
        "unsupported_claims": len(claims_by_id),
        "repairable_claims": len(candidates),
        "repairable_pages": len(candidate_pages),
        "unresolved_claims": len(unresolved_claim_ids),
        "unresolved_pages": len(unresolved_pages),
        "candidate_fingerprint": fingerprint,
        "candidate_claim_ids": [item["claim_id"] for item in candidates],
        "unresolved_claim_ids": unresolved_claim_ids,
        "candidates": candidates,
    }


def _source_foundation_update(source: dict, artifact: dict) -> dict:
    proposed = copy.deepcopy(source)
    governance_store.merge_foundation_record_fields(
        "sources",
        proposed,
        {
            "source_id": source["source_id"],
            "artifact_id": artifact["artifact_id"],
            "content_hash": artifact.get("content_hash"),
            "hash_algorithm": artifact.get("hash_algorithm"),
            "byte_size": artifact.get("byte_size"),
            "mime_type": artifact.get("mime_type"),
            "storage_uri": artifact.get("storage_uri"),
            "integrity_status": artifact.get("integrity_status"),
            "classification": artifact.get("classification"),
            "retention_policy": artifact.get("retention_policy"),
            "legal_hold": artifact.get("legal_hold"),
            "lineage_id": artifact.get("lineage_id"),
            "generation_parent_refs": artifact.get("generation_parent_refs"),
        },
    )
    return proposed


def _source_page_evidence(
    claim: dict,
    candidate: dict,
    *,
    source: dict,
    artifact: dict,
    frontmatter: dict,
    body: str,
    page_name: str,
    repaired_at: str,
) -> tuple[dict, dict]:
    page_key = str(candidate["page_key"])
    raw_ref = str(source["raw_ref"])
    run = build_extraction_run(
        page_key=page_key,
        body=body,
        artifact_ids=[str(artifact["artifact_id"])],
        frontmatter=frontmatter,
        extractor_name=PROVENANCE_REPAIR_EXTRACTOR_NAME,
        extractor_version=PROVENANCE_REPAIR_EXTRACTOR_VERSION,
    )
    run["recorded_at"] = repaired_at
    locator = dict(claim.get("locator") or {})
    evidence = {
        "evidence_id": candidate["source_page_record"]["evidence_id"],
        "evidence_family_id": version_family_id(
            "evidencefamily",
            page_key,
            {
                **locator,
                "source_id": source["source_id"],
                "kind": "provenance-reconstruction",
            },
        ),
        "source_id": source["source_id"],
        "artifact_id": artifact["artifact_id"],
        "locator": locator,
        "projection_locator": locator,
        "source_locator": source_locator_for(
            frontmatter,
            raw_ref,
            artifact=artifact,
        ),
        "evidence_text": str(claim.get("claim_text") or ""),
        "evidence_type": "provenance-reconstruction",
        "created_at": repaired_at,
        "extraction_run_id": run["run_id"],
        **evidence_independence(
            raw_ref,
            page_name,
            artifact.get("generation_parent_refs") or [],
        ),
        "supports_claim_ids": [claim["claim_id"]],
        "contradicts_claim_ids": [],
    }
    return evidence, run


def _append_support(evidence: dict, claim_id: str) -> bool:
    supports = list(dict.fromkeys(evidence.get("supports_claim_ids") or []))
    if claim_id in supports:
        return False
    supports.append(claim_id)
    evidence["supports_claim_ids"] = sorted(supports)
    return True


def _resolve_evidence_gap_items(conn, claim_ids: set[str], *, now: str, fingerprint: str) -> int:
    resolved = 0
    for row in conn.execute(
        "SELECT item_id, data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.type') = 'evidence-gap' "
        "AND json_extract(data_json, '$.status') = 'acknowledged'"
    ):
        item = json.loads(row["data_json"])
        if str(item.get("claim_id") or "") not in claim_ids:
            continue
        item.update(
            {
                "status": "resolved",
                "resolution": "provenance-reconstructed",
                "resolved_at": now,
                "repair_fingerprint": fingerprint,
            }
        )
        conn.execute(
            "UPDATE governance_queue SET data_json = ?, updated_at = ? "
            "WHERE item_id = ?",
            (json.dumps(item, ensure_ascii=False), now, str(row["item_id"])),
        )
        resolved += 1
    return resolved


def _apply_current_plan(plan: dict, *, fingerprint: str, repaired_at: str) -> dict:
    conn = db_store.get_connection()
    candidate_ids = set(plan["candidate_claim_ids"])
    claims_by_id = {
        claim_id: claim
        for claim_id, claim in _canonical_records(conn, "claims", "claim_id").items()
        if claim_id in candidate_ids
    }
    evidence_by_id = _canonical_records(conn, "evidence", "evidence_id")
    sources_by_id = _canonical_records(conn, "sources", "source_id")
    old_claims: list[dict] = []
    proposed_claims: list[dict] = []
    old_evidence_by_id: dict[str, dict] = {}
    proposed_evidence_by_id: dict[str, dict] = {}
    proposed_sources_by_id: dict[str, dict] = {}
    source_artifacts_by_id: dict[str, dict] = {}
    extraction_runs_by_id: dict[str, dict] = {}
    page_inputs: dict[str, tuple[dict, str, str]] = {}
    updated_evidence_links = 0
    created_evidence = 0

    for candidate in plan["candidates"]:
        claim_id = str(candidate["claim_id"])
        current_claim = claims_by_id[claim_id]
        old_claims.append(copy.deepcopy(current_claim))
        claim = copy.deepcopy(current_claim)
        for evidence_id in candidate["existing_evidence_ids"]:
            current = evidence_by_id.get(str(evidence_id))
            if current is None:
                raise RuntimeError(f"Evidence {evidence_id!r} disappeared after preview.")
            old_evidence_by_id.setdefault(evidence_id, copy.deepcopy(current))
            proposed = proposed_evidence_by_id.setdefault(
                evidence_id, copy.deepcopy(current)
            )
            updated_evidence_links += int(_append_support(proposed, claim_id))

        for record in candidate.get("official_evidence_records", []):
            source = record["source"]
            artifact = record["artifact"]
            evidence = copy.deepcopy(record["evidence"])
            evidence["created_at"] = repaired_at
            run = copy.deepcopy(record["run"])
            run["recorded_at"] = repaired_at
            evidence_id = evidence["evidence_id"]
            # Official records are immutable, claim-specific and collision checked
            # by the planner under this transaction, before any writes.
            proposed_evidence_by_id[evidence_id] = evidence
            proposed_sources_by_id[source["source_id"]] = source
            source_artifacts_by_id[artifact["artifact_id"]] = artifact
            extraction_runs_by_id[run["run_id"]] = run
            created_evidence += 1

        source_page_record = candidate.get("source_page_record")
        if source_page_record is not None:
            source_id = str(source_page_record["source_id"])
            source = sources_by_id.get(source_id)
            if source is None:
                planned_source = source_page_record.get("source_record")
                if not source_page_record.get("bootstrap_source") or not isinstance(
                    planned_source, dict
                ):
                    raise RuntimeError(
                        f"Source {source_id!r} disappeared after preview."
                    )
                source = copy.deepcopy(planned_source)
            artifact = resolve_source_artifact(
                str(source.get("raw_ref") or ""),
                source_id=source_id,
                metadata=source,
            )
            if (
                str(artifact.get("artifact_id") or "")
                != str(source_page_record["artifact_id"])
                or str(artifact.get("content_hash") or "")
                != str(source_page_record["artifact_hash"])
                or artifact.get("integrity_status") != "verified"
            ):
                raise RuntimeError(
                    f"Source artifact for {source_id!r} changed after preview."
                )
            page_key = str(candidate["page_key"])
            if page_key not in page_inputs:
                page_path = get_wiki_dir() / f"{page_key}.md"
                if not page_path.exists():
                    raise RuntimeError(
                        f"Source page {page_key!r} disappeared after preview."
                    )
                frontmatter, body, _raw = read_markdown_file(page_path)
                page_inputs[page_key] = (frontmatter, body, page_path.name)
            frontmatter, body, page_name = page_inputs[page_key]
            new_evidence, run = _source_page_evidence(
                claim,
                candidate,
                source=source,
                artifact=artifact,
                frontmatter=frontmatter,
                body=body,
                page_name=page_name,
                repaired_at=repaired_at,
            )
            evidence_id = str(new_evidence["evidence_id"])
            current = evidence_by_id.get(evidence_id)
            if current is None:
                proposed_evidence_by_id[evidence_id] = new_evidence
                created_evidence += 1
            else:
                old_evidence_by_id.setdefault(evidence_id, copy.deepcopy(current))
                proposed = proposed_evidence_by_id.setdefault(
                    evidence_id, copy.deepcopy(current)
                )
                updated_evidence_links += int(_append_support(proposed, claim_id))
            proposed_sources_by_id[source_id] = _source_foundation_update(
                source, artifact
            )
            source_artifacts_by_id[str(artifact["artifact_id"])] = artifact
            extraction_runs_by_id[str(run["run_id"])] = run

        claim["source_ids"] = sorted(
            set(claim.get("source_ids") or []) | set(candidate["source_ids"])
        )
        claim["evidence_ids"] = sorted(
            set(claim.get("evidence_ids") or []) | set(candidate["evidence_ids"])
        )
        claim["provenance_repair"] = {
            "contract": _CONTRACT,
            "fingerprint": fingerprint,
            "repaired_at": repaired_at,
            "strategies": candidate["strategies"],
        }
        proposed_claims.append(claim)

    for claim in proposed_claims:
        for evidence_id in claim.get("evidence_ids") or []:
            proposed = proposed_evidence_by_id.get(str(evidence_id))
            if proposed is not None:
                updated_evidence_links += int(
                    _append_support(proposed, str(claim["claim_id"]))
                )

    proposed_evidence = list(proposed_evidence_by_id.values())
    evidence_page_keys = {
        claim_page_key(record) for record in proposed_evidence
    }
    evidence_owners = governance_store._proposed_id_owners(
        proposed_evidence,
        record_kind="evidence",
        id_field="evidence_id",
        affected_page_keys=evidence_page_keys,
    )
    governance_store._validate_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    if plan.get("source_claim_ids") is not None:
        governance_store._validate_operational_memory_delta_scope(
            candidate_ids, proposed_claims, set(plan["source_claim_ids"])
        )
    governance_store._register_locator_id_ownership(
        conn,
        owners=evidence_owners,
        record_kind="evidence",
    )
    governance_store._append_version_records(
        "claim_versions",
        "claim_id",
        "claim_family_id",
        "claimfamily",
        "claim_version",
        old_claims,
    )
    if old_evidence_by_id:
        governance_store._append_version_records(
            "evidence_versions",
            "evidence_id",
            "evidence_family_id",
            "evidencefamily",
            "evidence_version",
            list(old_evidence_by_id.values()),
        )
    governance_store._upsert_canonical_records("claims", "claim_id", proposed_claims)
    governance_store._upsert_canonical_records(
        "evidence", "evidence_id", proposed_evidence
    )
    governance_store._upsert_canonical_records(
        "sources", "source_id", list(proposed_sources_by_id.values())
    )
    governance_store._upsert_foundation_records(
        [],
        list(source_artifacts_by_id.values()),
        list(extraction_runs_by_id.values()),
    )
    governance_store._append_version_records(
        "claim_versions",
        "claim_id",
        "claim_family_id",
        "claimfamily",
        "claim_version",
        proposed_claims,
    )
    governance_store._append_version_records(
        "evidence_versions",
        "evidence_id",
        "evidence_family_id",
        "evidencefamily",
        "evidence_version",
        proposed_evidence,
    )
    governance_store._refresh_operational_memory_delta(candidate_ids, proposed_claims)
    resolved_items = _resolve_evidence_gap_items(
        conn,
        candidate_ids,
        now=repaired_at,
        fingerprint=fingerprint,
    )
    return {
        "created_evidence": created_evidence,
        "updated_evidence_links": updated_evidence_links,
        "resolved_governance_items": resolved_items,
    }


def repair_claim_provenance(
    dry_run: bool = True,
    *,
    runtime_only: bool = True,
    confirmation: str = "",
    source_map_path: str = "",
    source_claim_ids: list[str] | None = None,
    official_evidence_map_path: str = "",
) -> dict:
    """Preview or apply one exact, non-invented provenance repair plan."""
    plan = build_claim_provenance_repair_plan(
        runtime_only=runtime_only,
        source_map_path=source_map_path,
        source_claim_ids=source_claim_ids,
        official_evidence_map_path=official_evidence_map_path,
    )
    if dry_run:
        return {
            "dry_run": True,
            **{
                key: value
                for key, value in plan.items()
                if key
                not in {
                    "candidates",
                    "state_basis",
                    "candidate_claim_ids",
                    "unresolved_claim_ids",
                }
            },
            "candidate_sample": plan["candidate_claim_ids"][:10],
            "unresolved_sample": plan["unresolved_claim_ids"][:10],
            "confirmation_required": bool(plan["repairable_claims"]),
        }
    expected = str(plan["candidate_fingerprint"])
    if not confirmation or not hmac.compare_digest(str(confirmation), expected):
        raise ValueError(
            "Claim provenance repair requires the exact preview fingerprint: "
            f"{expected}"
        )

    from vector_lake.tool_projection import require_maintenance_backup

    backup = require_maintenance_backup("claim_provenance_repair")
    repaired_at = _utc_now()
    with db_store.transaction():
        current_plan = build_claim_provenance_repair_plan(
            runtime_only=runtime_only,
            source_map_path=source_map_path,
            source_claim_ids=source_claim_ids,
            official_evidence_map_path=official_evidence_map_path,
        )
        if not hmac.compare_digest(
            str(current_plan["candidate_fingerprint"]), expected
        ):
            raise RuntimeError(
                "Claim provenance repair candidate set changed after preview; "
                "run a new dry-run and review its fingerprint."
            )
        if source_claim_ids is not None:
            _validate_scoped_backup(backup, current_plan["state_basis"])
            # Backup verification can be lengthy. Recheck external frozen bytes
            # at the final pre-write boundary as well as on transaction entry.
            current_plan = build_claim_provenance_repair_plan(
                runtime_only=runtime_only, source_map_path=source_map_path,
                source_claim_ids=source_claim_ids,
                official_evidence_map_path=official_evidence_map_path,
            )
            if current_plan["candidate_fingerprint"] != expected:
                raise RuntimeError("Claim provenance repair changed during backup validation.")
        applied = _apply_current_plan(
            current_plan,
            fingerprint=expected,
            repaired_at=repaired_at,
        )
    return {
        "dry_run": False,
        "runtime_only": bool(runtime_only),
        "unsupported_claims_before": plan["unsupported_claims"],
        "repaired_claims": plan["repairable_claims"],
        "repaired_pages": plan["repairable_pages"],
        **applied,
        "unresolved_claims": plan["unresolved_claims"],
        "unresolved_pages": plan["unresolved_pages"],
        "candidate_fingerprint": expected,
        "backup": backup,
        "projection_rebuild_required": True,
    }
