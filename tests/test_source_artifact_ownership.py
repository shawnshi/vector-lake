import copy
import json

import pytest

from vector_lake import db_store, governance_store


def _artifact(source_id="source_a"):
    return {
        "artifact_id": "artifact_shared",
        "source_id": source_id,
        "content_hash": "a" * 64,
        "sha256": "a" * 64,
        "integrity_status": "verified",
    }


def _change(artifacts, title="owner"):
    return {
        "affected_pages": ["Source_Owner.md"],
        "proposed_entities": [{
            "entity_id": "entity_owner", "page_key": "Source_Owner",
            "title": title, "type": "source", "status": "Active",
        }],
        "proposed_claims": [],
        "proposed_evidence": [],
        "proposed_source_updates": [],
        "proposed_source_artifacts": artifacts,
        "proposed_extraction_runs": [],
        "proposed_edges": [],
    }


def _state():
    conn = governance_store.get_connection()
    return {
        table: [tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        )]
        for table in ("entities", "entity_identities", "source_artifacts")
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_same_artifact_id_different_incoming_owners_roll_back(reverse, isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(_change([_artifact()]))
    before = _state()
    artifacts = [_artifact("source_a"), _artifact("source_b")]
    if reverse:
        artifacts.reverse()
    with pytest.raises(ValueError, match="^Conflicting source artifact ownership$"):
        governance_store.apply_change_set(_change(artifacts, title="must roll back"))
    assert _state() == before


def test_existing_artifact_owner_cannot_change(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(_change([_artifact()]))
    before = _state()
    with pytest.raises(ValueError, match="^Conflicting source artifact ownership$"):
        governance_store.apply_change_set(_change([_artifact("source_b")]))
    assert _state() == before


def test_physical_and_json_ownership_mismatch_fails_closed(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(_change([_artifact()]))
    conn = governance_store.get_connection()
    conn.execute(
        "UPDATE source_artifacts SET source_id = ? WHERE artifact_id = ?",
        ("source_physical", "artifact_shared"),
    )
    conn.commit()
    before = _state()
    with pytest.raises(ValueError, match="^Invalid source artifact ownership record$"):
        governance_store.apply_change_set(_change([_artifact()], title="new mutation after drift"))
    assert _state() == before


@pytest.mark.parametrize("key,value", [("artifact_id", " artifact_shared"), ("source_id", "source_a "), ("source_id", 7)])
def test_identity_is_not_trimmed_or_coerced_before_guard(isolated_memory, key, value):
    db_store.init_db()
    governance_store.apply_change_set(_change([_artifact()]))
    before = _state()
    artifact = _artifact()
    artifact[key] = value
    with pytest.raises(ValueError, match="^Invalid source artifact ownership record$"):
        governance_store.apply_change_set(_change([artifact], title="new invalid identity"))
    assert _state() == before


def test_nonobject_stored_json_is_rejected_without_writes(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(_change([_artifact()]))
    conn = governance_store.get_connection()
    conn.execute("UPDATE source_artifacts SET data_json='[]' WHERE artifact_id='artifact_shared'")
    conn.commit()
    before = _state()
    with pytest.raises(ValueError, match="^Invalid source artifact ownership record$"):
        governance_store.apply_change_set(_change([_artifact()], title="new invalid JSON"))
    assert _state() == before


def test_unchanged_owner_can_update_artifact(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(_change([_artifact()]))
    updated = _artifact()
    updated["classification"] = "public"
    governance_store.apply_change_set(_change([updated]))
    row = governance_store.get_connection().execute(
        "SELECT source_id, data_json FROM source_artifacts WHERE artifact_id = ?",
        ("artifact_shared",),
    ).fetchone()
    assert row["source_id"] == "source_a"
    assert json.loads(row["data_json"])["classification"] == "public"


def test_equal_file_bytes_do_not_select_an_artifact_owner(isolated_memory):
    db_store.init_db()
    left = _artifact("source_left")
    right = copy.deepcopy(left)
    right["source_id"] = "source_right"
    with pytest.raises(ValueError, match="^Conflicting source artifact ownership$"):
        governance_store.apply_change_set(_change([left, right]))
    assert _state() == {"entities": [], "entity_identities": [], "source_artifacts": []}


def _page_batch_item(page_key: str, artifact: dict):
    return {
        "page_key": page_key,
        "claims": [],
        "evidence": [],
        "sources": [],
        "entities": [],
        "source_artifacts": [artifact],
        "extraction_runs": [{
            "run_id": f"run_{page_key}",
            "page_key": page_key,
            "input_fingerprint": f"fp_{page_key}",
            "extractor_name": "test",
            "extractor_version": "1.0",
        }],
    }


def test_batch_backfill_merges_complementary_metadata_for_same_owner_artifacts(isolated_memory):
    db_store.init_db()
    art1 = _artifact("source_shared")
    art1["classification"] = "public"
    art1["retention_policy"] = "unspecified"
    art1["generation_parent_refs"] = ["parent_ref_1"]

    art2 = _artifact("source_shared")
    art2["classification"] = "unspecified"
    art2["retention_policy"] = "retain_audit"
    art2["generation_parent_refs"] = ["parent_ref_2"]

    page1 = _page_batch_item("Page_One", art1)
    page2 = _page_batch_item("Page_Two", art2)

    with db_store.transaction():
        result = governance_store.backfill_evidence_foundation_batch([page1, page2])

    assert result["pages"] == 2
    assert result["source_artifacts"] == 1

    conn = governance_store.get_connection()
    row = conn.execute(
        "SELECT source_id, data_json FROM source_artifacts WHERE artifact_id = ?",
        ("artifact_shared",),
    ).fetchone()
    assert row["source_id"] == "source_shared"
    stored = json.loads(row["data_json"])
    assert stored["classification"] == "public"
    assert stored["retention_policy"] == "retain_audit"
    assert set(stored["generation_parent_refs"]) == {"parent_ref_1", "parent_ref_2"}


def test_batch_backfill_reverse_order_produces_equivalent_merged_metadata(isolated_memory):
    db_store.init_db()
    art1 = _artifact("source_shared")
    art1["classification"] = "public"
    art1["retention_policy"] = "unspecified"
    art1["generation_parent_refs"] = ["parent_ref_1"]

    art2 = _artifact("source_shared")
    art2["classification"] = "unspecified"
    art2["retention_policy"] = "retain_audit"
    art2["generation_parent_refs"] = ["parent_ref_2"]

    page1 = _page_batch_item("Page_One", art1)
    page2 = _page_batch_item("Page_Two", art2)

    with db_store.transaction():
        result = governance_store.backfill_evidence_foundation_batch([page2, page1])

    assert result["pages"] == 2
    assert result["source_artifacts"] == 1

    conn = governance_store.get_connection()
    row = conn.execute(
        "SELECT source_id, data_json FROM source_artifacts WHERE artifact_id = ?",
        ("artifact_shared",),
    ).fetchone()
    assert row["source_id"] == "source_shared"
    stored = json.loads(row["data_json"])
    assert stored["classification"] == "public"
    assert stored["retention_policy"] == "retain_audit"
    assert set(stored["generation_parent_refs"]) == {"parent_ref_1", "parent_ref_2"}


def test_batch_backfill_rejects_conflicting_owners_across_pages_with_rollback(isolated_memory):
    db_store.init_db()
    page1 = _page_batch_item("Page_One", _artifact("source_a"))
    page2 = _page_batch_item("Page_Two", _artifact("source_b"))

    before = _state()
    with pytest.raises(ValueError, match="^Conflicting source artifact ownership$"):
        with db_store.transaction():
            governance_store.backfill_evidence_foundation_batch([page1, page2])
    assert _state() == before

