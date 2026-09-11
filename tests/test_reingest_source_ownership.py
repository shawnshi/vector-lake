import copy
import json
import sqlite3

import pytest

from vector_lake import db_store, governance_store


def _records():
    source = {
        "source_id": "source_test", "raw_ref": "raw/test.md",
        "artifact_id": "artifact_test", "content_hash": "a" * 64,
        "integrity_status": "verified", "canonical_source_page": "Source_Owner.md",
        "classification": "public", "retention_policy": "retain",
        "legal_hold": True, "generation_parent_refs": ["parent_old"],
        "ingested_at": "2026-01-01T00:00:00+00:00",
    }
    proposed = {**source, "canonical_source_page": "", "classification": "unspecified",
                "retention_policy": "unspecified", "legal_hold": False,
                "generation_parent_refs": ["parent_new"]}
    owner = {"entity_id": "entity_owner", "page_key": "Source_Owner", "type": "source",
             "status": "Active", "sources": ["raw/test.md"]}
    artifact = {"artifact_id": "artifact_test", "source_id": "source_test",
                "content_hash": "a" * 64, "integrity_status": "verified"}
    return source, proposed, owner, artifact


def _merge(source, proposed, owner, artifact, *, physical_source_id="source_test", replacements=None, extra_proposals=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE sources (source_id TEXT PRIMARY KEY, data_json TEXT);"
        "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, type TEXT, status TEXT, data_json TEXT);"
        "CREATE INDEX idx_entities_page_key ON entities(json_extract(data_json, '$.page_key'));"
        "CREATE TABLE source_artifacts (artifact_id TEXT PRIMARY KEY, source_id TEXT, data_json TEXT);"
    )
    conn.execute("INSERT INTO sources VALUES (?,?)", ("source_test", json.dumps(source)))
    if owner is not None:
        conn.execute("INSERT INTO entities VALUES (?,?,?,?)", ("entity_owner", owner.get("type"), owner.get("status"), json.dumps(owner)))
    if artifact is not None:
        conn.execute("INSERT INTO source_artifacts VALUES (?,?,?)", ("artifact_test", physical_source_id, json.dumps(artifact)))
    before = copy.deepcopy(proposed)
    changes = conn.total_changes
    try:
        result = governance_store._merge_reingested_source_records(
            conn, "source_id", [proposed, *(extra_proposals or [])], proposed_source_artifacts=replacements,
        )[0]
        assert conn.total_changes == changes
        assert proposed == before
        return result
    finally:
        conn.close()


@pytest.mark.parametrize("case", ["explicit_hint", "changed_raw", "revoked"])
def test_same_source_id_does_not_share_retention_permission(case):
    source, proposed, owner, artifact = _records()
    valid_sibling = copy.deepcopy(proposed)
    if case == "explicit_hint":
        proposed["canonical_source_page"] = "Source_New.md"
    elif case == "changed_raw":
        proposed["raw_ref"] = "raw/other.md"
    else:
        proposed["revoked_at"] = "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="Conflicting same-source retention proposals"):
        _merge(source, proposed, owner, artifact, extra_proposals=[valid_sibling])


def test_valid_hint_and_existing_preservation_contract():
    result = _merge(*_records())
    assert result["canonical_source_page"] == "Source_Owner.md"
    assert result["classification"] == "public"
    assert result["retention_policy"] == "retain"
    assert result["legal_hold"] is True
    assert result["generation_parent_refs"] == ["parent_old", "parent_new"]


@pytest.mark.parametrize("case", [
    "source_revoked_at", "proposal_archived", "artifact_unverified", "artifact_hash_mismatch",
    "physical_source_mismatch", "owner_deleted_lifecycle", "owner_missing", "owner_wrong_type",
    "owner_undeclared", "raw_changed", "artifact_missing", "malformed_expiry", "expired",
    "source_unverified", "proposal_unverified", "missing_source_hash", "missing_proposal_hash",
    "missing_artifact_hash", "owner_archived", "source_superseded", "artifact_revoked",
    "artifact_raw_ref_conflict", "nested_snapshot_expired", "nested_metadata_revoked",
    "hash_algorithm_conflict", "explicit_ref_conflict",
])
def test_invalid_ownership_or_provenance_does_not_retain_hint(case):
    source, proposed, owner, artifact = _records()
    physical = "source_test"
    if case == "source_revoked_at":
        source["revoked_at"] = "2026-01-01T00:00:00+00:00"
    elif case == "proposal_archived":
        proposed["status"] = "archived"
    elif case == "artifact_unverified":
        artifact["integrity_status"] = "unverified"
    elif case == "artifact_hash_mismatch":
        artifact["content_hash"] = "b" * 64
    elif case == "physical_source_mismatch":
        physical = "source_other"
    elif case == "owner_deleted_lifecycle":
        owner["lifecycle_state"] = "deleted"
    elif case == "owner_missing":
        owner = None
    elif case == "owner_wrong_type":
        owner["type"] = "concept"
    elif case == "owner_undeclared":
        owner["sources"] = []
    elif case == "raw_changed":
        proposed["raw_ref"] = "raw/other.md"
    elif case == "artifact_missing":
        artifact = None
    elif case == "malformed_expiry":
        source["expires_at"] = "not-a-date"
    elif case == "expired":
        source["expires_at"] = "2000-01-01T00:00:00+00:00"
    elif case == "source_unverified":
        source["integrity_status"] = "unverified"
    elif case == "proposal_unverified":
        proposed["integrity_status"] = "unverified"
    elif case == "missing_source_hash":
        source.pop("content_hash")
    elif case == "missing_proposal_hash":
        proposed.pop("content_hash")
    elif case == "missing_artifact_hash":
        artifact.pop("content_hash")
    elif case == "owner_archived":
        owner["status"] = "archived"
    elif case == "source_superseded":
        source["lifecycle_state"] = "superseded"
    elif case == "artifact_revoked":
        artifact["revoked_at"] = "2026-01-01T00:00:00+00:00"
    elif case == "artifact_raw_ref_conflict":
        artifact["raw_ref"] = "raw/other.md"
    elif case == "nested_snapshot_expired":
        artifact["official_snapshot"] = {
            "expires_at": "2000-01-01T00:00:00+00:00"
        }
    elif case == "nested_metadata_revoked":
        artifact["official_snapshot"] = {
            "metadata": {"revoked_at": "2026-01-01T00:00:00+00:00"}
        }
    elif case == "hash_algorithm_conflict":
        source["hash_algorithm"] = "sha256"
        proposed["hash_algorithm"] = "sha512"
    elif case == "explicit_ref_conflict":
        proposed["raw_ref"] = "Source_Other.md"
    result = _merge(source, proposed, owner, artifact, physical_source_id=physical)
    assert result["canonical_source_page"] == ""


@pytest.mark.parametrize("case", ["owner_decayed", "owner_unknown", "owner_missing_status", "nontext_hash", "malformed_hash", "unknown_algorithm"])
def test_retention_requires_positive_active_owner_and_sha256(case):
    source, proposed, owner, artifact = _records()
    if case == "owner_decayed":
        owner["status"] = "Decayed"
    elif case == "owner_unknown":
        owner["status"] = "Unknown"
    elif case == "owner_missing_status":
        owner.pop("status")
    elif case in {"nontext_hash", "malformed_hash"}:
        value = 123 if case == "nontext_hash" else "not-a-hash"
        for record in (source, proposed, artifact):
            record["content_hash"] = value
    else:
        for record in (source, proposed, artifact):
            record["hash_algorithm"] = "unknown"
    assert _merge(source, proposed, owner, artifact)["canonical_source_page"] == ""


def test_explicit_nonblank_proposal_is_not_overridden():
    source, proposed, owner, artifact = _records()
    proposed["canonical_source_page"] = "Source_New.md"
    assert _merge(source, proposed, owner, artifact)["canonical_source_page"] == "Source_New.md"


def test_verified_same_batch_replacement_retains_hint():
    source, proposed, owner, artifact = _records()
    replacement = copy.deepcopy(artifact)
    replacement["raw_ref"] = source["raw_ref"]
    result = _merge(
        source, proposed, owner, artifact, replacements=[replacement]
    )
    assert result["canonical_source_page"] == "Source_Owner.md"


def test_all_positive_validity_branches_retain_hint():
    source, proposed, owner, artifact = _records()
    future = "2999-01-01T00:00:00+00:00"
    for record in (source, proposed, artifact):
        record["status"] = "active"
        record["lifecycle_state"] = "active"
        record["integrity_status"] = "verified"
        record["hash_algorithm"] = "sha256"
        record["expires_at"] = future
    artifact["raw_ref"] = source["raw_ref"]
    artifact["official_snapshot"] = {
        "metadata": {"expires_at": future}
    }
    owner["lifecycle_state"] = "active"
    replacement = copy.deepcopy(artifact)
    assert _merge(
        source, proposed, owner, artifact, replacements=[replacement]
    )["canonical_source_page"] == "Source_Owner.md"


@pytest.mark.parametrize("case", [
    "revoked", "unverified", "missing_hash", "hash_changed", "raw_ref_conflict",
    "source_conflict", "artifact_conflict", "duplicate_conflict",
])
def test_unsafe_same_batch_replacement_does_not_retain_hint(case):
    source, proposed, owner, artifact = _records()
    replacement = copy.deepcopy(artifact)
    replacement["raw_ref"] = source["raw_ref"]
    replacements = [replacement]
    if case == "revoked":
        replacement["revoked_at"] = "2026-01-01T00:00:00+00:00"
    elif case == "unverified":
        replacement["integrity_status"] = "unverified"
    elif case == "missing_hash":
        replacement.pop("content_hash")
    elif case == "hash_changed":
        replacement["content_hash"] = "b" * 64
    elif case == "raw_ref_conflict":
        replacement["raw_ref"] = "raw/other.md"
    elif case == "source_conflict":
        replacement["source_id"] = "source_other"
    elif case == "artifact_conflict":
        replacement["artifact_id"] = "artifact_other"
    elif case == "duplicate_conflict":
        conflicting = copy.deepcopy(replacement)
        conflicting["integrity_status"] = "unverified"
        replacements.append(conflicting)
    result = _merge(
        source, proposed, owner, artifact, replacements=replacements
    )
    assert result["canonical_source_page"] == ""


def test_physical_artifact_identity_cannot_be_filled_from_json_defaults():
    source, proposed, owner, artifact = _records()
    artifact.pop("source_id")
    assert _merge(source, proposed, owner, artifact)["canonical_source_page"] == ""
    artifact["source_id"] = "source_test"
    artifact.pop("artifact_id")
    assert _merge(source, proposed, owner, artifact)["canonical_source_page"] == ""


def test_actual_source_then_event_extraction_preserves_current_hint(isolated_memory):
    from vector_lake.claim_extractor import extract_page_objects
    raw = isolated_memory / "raw/test.md"
    raw.write_text("A public source for the ownership test.", encoding="utf-8")
    db_store.init_db()
    def extract_and_apply(page, kind):
        fm = {"id": page, "title": page, "type": kind, "domain": "General",
              "status": "Active", "epistemic-status": "seed", "sources": ["raw/test.md"],
              "categories": ["Uncategorized"], "updated": "2026-09-10T00:00:00+00:00"}
        body = "An attributable statement."
        if kind == "event":
            body = "## 1. 编译事实\n\nAn attributable statement.\n\n## 2. 证据时间线\n\n- [2026-09-10] [Observation] An attributable statement."
        from vector_lake.schema_validator import validate_schema
        validate_schema(fm, body, page + ".md")
        extracted = extract_page_objects(page + ".md", fm, body)
        change = {"affected_pages": [page + ".md"]}
        for field in ("entities", "claims", "evidence", "source_artifacts", "extraction_runs", "edges"):
            change["proposed_" + field] = extracted[field]
        change["proposed_source_updates"] = extracted["sources"]
        governance_store.apply_change_set(change)
        return extracted["sources"][0]["source_id"]
    source_id = extract_and_apply("Source_ActualOwner", "source")
    assert extract_and_apply("Event_SharedSource", "event") == source_id
    assert governance_store.load_sources()["items"][source_id]["canonical_source_page"] == "Source_ActualOwner.md"
    extract_and_apply("Event_SharedSource", "event")
    assert governance_store.load_sources()["items"][source_id]["canonical_source_page"] == "Source_ActualOwner.md"


@pytest.mark.parametrize("changed_declaration", [[], ["raw/other.md"]])
def test_apply_uses_same_batch_source_owner_and_is_stable(
    isolated_memory, changed_declaration
):
    source, proposed, owner, artifact = _records()
    db_store.init_db()

    def change_set(*, entity, source_update, source_artifacts):
        return {
            "affected_pages": ["Source_Owner.md"],
            "proposed_entities": [entity],
            "proposed_claims": [],
            "proposed_evidence": [],
            "proposed_source_updates": [source_update],
            "proposed_source_artifacts": source_artifacts,
            "proposed_extraction_runs": [],
            "proposed_edges": [],
        }

    governance_store.apply_change_set(
        change_set(entity=owner, source_update=source, source_artifacts=[artifact])
    )
    refresh = change_set(
        entity=copy.deepcopy(owner),
        source_update=copy.deepcopy(proposed),
        source_artifacts=[copy.deepcopy(artifact)],
    )
    governance_store.apply_change_set(copy.deepcopy(refresh))
    first = governance_store.load_sources()["items"]["source_test"]
    assert first["canonical_source_page"] == "Source_Owner.md"

    governance_store.apply_change_set(copy.deepcopy(refresh))
    second = governance_store.load_sources()["items"]["source_test"]
    assert second == first

    changed_owner = copy.deepcopy(owner)
    changed_owner["sources"] = changed_declaration
    governance_store.apply_change_set(
        change_set(
            entity=changed_owner,
            source_update=copy.deepcopy(proposed),
            source_artifacts=[copy.deepcopy(artifact)],
        )
    )
    assert governance_store.load_sources()["items"]["source_test"]["canonical_source_page"] == ""


def _ownership_change(owner, source_updates, artifact):
    return {
        "affected_pages": ["Source_Owner.md"],
        "proposed_entities": [owner],
        "proposed_claims": [],
        "proposed_evidence": [],
        "proposed_source_updates": source_updates,
        "proposed_source_artifacts": [artifact],
        "proposed_extraction_runs": [],
        "proposed_edges": [],
    }


def _canonical_source_entity_state():
    conn = governance_store.get_connection()
    return {
        table: [tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        ).fetchall()]
        for table in ("sources", "entities")
    }


@pytest.mark.parametrize("case", ["changed_raw", "revoked", "conflicting_mapping"])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_id_conflicting_group_rolls_back_full_apply(
    isolated_memory, case, reverse
):
    source, proposed, owner, artifact = _records()
    db_store.init_db()
    governance_store.apply_change_set(
        _ownership_change(owner, [source], artifact)
    )
    before = _canonical_source_entity_state()

    valid = copy.deepcopy(proposed)
    valid["title"] = "benign event refresh"
    conflict = copy.deepcopy(proposed)
    if case == "changed_raw":
        conflict["raw_ref"] = "raw/other.md"
    elif case == "revoked":
        conflict["revoked_at"] = "2026-01-01T00:00:00+00:00"
    else:
        conflict["canonical_source_page"] = "Source_Other.md"
    updates = [valid, conflict]
    if reverse:
        updates.reverse()

    changed_owner = copy.deepcopy(owner)
    changed_owner["title"] = "must be rolled back"
    with pytest.raises(ValueError, match="Conflicting same-source retention proposals"):
        governance_store.apply_change_set(
            _ownership_change(changed_owner, updates, copy.deepcopy(artifact))
        )
    assert _canonical_source_entity_state() == before


@pytest.mark.parametrize("reverse", [False, True])
def test_same_id_source_event_current_hint_and_blank_are_compatible(
    isolated_memory, reverse
):
    source, proposed, owner, artifact = _records()
    db_store.init_db()
    governance_store.apply_change_set(_ownership_change(owner, [source], artifact))

    source_refresh = copy.deepcopy(proposed)
    source_refresh["canonical_source_page"] = "Source_Owner.md"
    source_refresh["title"] = "source refresh"
    event_refresh = copy.deepcopy(proposed)
    event_refresh["title"] = "event refresh"
    event_refresh["ingested_at"] = "2026-09-10T00:00:00+00:00"
    updates = [source_refresh, event_refresh]
    if reverse:
        updates.reverse()
    governance_store.apply_change_set(
        _ownership_change(copy.deepcopy(owner), updates, copy.deepcopy(artifact))
    )
    stored = governance_store.load_sources()["items"]["source_test"]
    assert stored["canonical_source_page"] == "Source_Owner.md"


@pytest.mark.parametrize("reverse", [False, True])
def test_same_id_repeated_identical_blank_updates_are_order_independent(
    isolated_memory, reverse
):
    source, proposed, owner, artifact = _records()
    db_store.init_db()
    governance_store.apply_change_set(_ownership_change(owner, [source], artifact))
    first = copy.deepcopy(proposed)
    second = copy.deepcopy(proposed)
    updates = [first, second]
    if reverse:
        updates.reverse()
    governance_store.apply_change_set(
        _ownership_change(copy.deepcopy(owner), updates, copy.deepcopy(artifact))
    )
    assert governance_store.load_sources()["items"]["source_test"][
        "canonical_source_page"
    ] == "Source_Owner.md"
