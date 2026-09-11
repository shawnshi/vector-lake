import json

import pytest

from vector_lake import db_store, governance_store, indexer
from vector_lake import tool_claim_cleanup as cleanup


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _install_fixture(*, claim_id="claim_stub", with_bystander=True, with_owner=False):
    claim = {
        "claim_id": claim_id,
        "claim_text": "Auto-generated stub for Alpha. (Last reshaped: 2026-08-01)",
        "status": "Active",
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": ["entity_alpha"] if with_owner else [],
        "locator": {"page_key": "Concept_Alpha", "heading": "Facts", "block_index": 1},
    }
    claims = [claim]
    if with_bystander:
        bystander = {**claim, "claim_id": "claim_bystander", "claim_text": "A genuine fact.",
                     "locator": {"page_key": "Concept_Alpha", "heading": "Facts", "block_index": 2}}
        claims.append(bystander)
    governance_store.apply_change_set({
        "affected_pages": ["Concept_Alpha"], "proposed_entities": ([{
            "entity_id": "entity_alpha", "page_key": "Concept_Alpha",
            "canonical_name": "Alpha", "type": "concept", "status": "Active",
        }] if with_owner else []),
        "proposed_claims": claims, "proposed_evidence": [], "proposed_source_updates": [],
        "proposed_source_artifacts": [], "proposed_extraction_runs": [], "proposed_edges": [],
    })
    conn = db_store.get_connection()
    row = conn.execute(
        "SELECT memory_id,data_json FROM operational_memory "
        "WHERE json_extract(data_json,'$.source_claim_id')=?", (claim_id,),
    ).fetchone()
    assert row is not None
    memory = json.loads(row["data_json"])
    memory.update(memory_id="memory_stub", memory_type="fact", validity_state="archived",
                  validity_reasons=["missing_evidence_and_source", "infrastructure_artifact:generated_reshaped_stub"])
    conn.execute(
        "UPDATE operational_memory SET memory_id='memory_stub',memory_type='fact',status='Archived',data_json=? "
        "WHERE memory_id=?", (_canonical(memory), row["memory_id"]),
    )
    queue = {"item_id": "queue_stub", "type": "evidence-gap", "status": "acknowledged",
             "resolution": "research-required", "claim_id": claim_id}
    conn.execute("INSERT INTO governance_queue VALUES (?,?,?)", ("queue_stub", _canonical(queue), "2026-08-01T00:00:00+00:00"))
    conn.commit()
    return claim


def test_cleanup_preserves_subject_entity_and_other_claim(isolated_memory):
    db_store.init_db()
    _install_fixture(with_owner=True)
    indexer.generate_index()
    conn = db_store.get_connection()
    entity_before = tuple(conn.execute("SELECT * FROM entities WHERE entity_id='entity_alpha'").fetchone())
    bystander_before = tuple(conn.execute("SELECT * FROM claims WHERE claim_id='claim_bystander'").fetchone())
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    assert preview["eligible"]
    result = cleanup.cleanup_placeholder_claims(["claim_stub"], dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert result["committed"]
    assert tuple(conn.execute("SELECT * FROM entities WHERE entity_id='entity_alpha'").fetchone()) == entity_before
    assert tuple(conn.execute("SELECT * FROM claims WHERE claim_id='claim_bystander'").fetchone()) == bystander_before


def test_preview_is_read_only_and_scope_validation_is_strict(isolated_memory, monkeypatch):
    db_store.init_db()
    _install_fixture()
    monkeypatch.setattr(db_store, "init_db", lambda: (_ for _ in ()).throw(AssertionError("write path")))
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    assert preview["eligible"] is True
    assert preview["claim_count"] == preview["memory_count"] == preview["related_queue_count"] == 1
    assert "claim_text" not in json.dumps(preview)
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
    with pytest.raises(ValueError, match="unique nonempty"):
        cleanup.cleanup_placeholder_claims(["claim_stub", "claim_stub"])


def test_apply_uses_verified_backup_and_preserves_history_and_bystander(isolated_memory):
    db_store.init_db()
    _install_fixture()
    indexer.generate_index()
    conn = db_store.get_connection()
    history_before = conn.execute("SELECT data_json FROM claim_versions").fetchall()
    bystander_before = conn.execute("SELECT data_json FROM claims WHERE claim_id='claim_bystander'").fetchone()[0]
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    with pytest.raises(ValueError, match="exact preview fingerprint"):
        cleanup.cleanup_placeholder_claims(["claim_stub"], dry_run=False, confirmation="wrong")
    result = cleanup.cleanup_placeholder_claims(
        ["claim_stub"], dry_run=False, confirmation=preview["candidate_fingerprint"]
    )
    assert result["committed"] is True
    assert result["deleted_claims"] == result["deleted_memories"] == result["resolved_governance_items"] == 1
    assert conn.execute("SELECT 1 FROM claims WHERE claim_id='claim_stub'").fetchone() is None
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='memory_stub'").fetchone() is None
    assert conn.execute("SELECT data_json FROM claims WHERE claim_id='claim_bystander'").fetchone()[0] == bystander_before
    assert conn.execute("SELECT data_json FROM claim_versions").fetchall() == history_before
    queue = json.loads(conn.execute("SELECT data_json FROM governance_queue WHERE item_id='queue_stub'").fetchone()[0])
    assert queue["resolution"] == "removed-generated-placeholder"
    assert queue["previous_resolution"]["resolution"] == "research-required"
    with pytest.raises(ValueError, match="ineligible"):
        cleanup.cleanup_placeholder_claims(
            ["claim_stub"], dry_run=False, confirmation=preview["candidate_fingerprint"]
        )


@pytest.mark.parametrize("dependency", ["assessment", "edge", "evidence"])
def test_dependencies_block_all_or_nothing(isolated_memory, dependency):
    db_store.init_db()
    _install_fixture(with_bystander=False)
    conn = db_store.get_connection()
    if dependency == "assessment":
        conn.execute("INSERT INTO claim_assessments VALUES (?,?,?,?,?,?,?,?)",
                     ("assessment", "claim_stub", "review", "pass", "tester", "v1", "{}", "2026-08-01T00:00:00+00:00"))
    elif dependency == "edge":
        conn.execute("INSERT INTO claim_graph_edges VALUES (?,?,?,?,?)",
                     ("claim_stub", "other", "supports", 1.0, "2026-08-01T00:00:00+00:00"))
    else:
        evidence = {"evidence_id": "evidence", "supports_claim_ids": ["claim_stub"], "contradicts_claim_ids": []}
        conn.execute("INSERT INTO evidence VALUES (?,?,?)", ("evidence", json.dumps(evidence), "2026-08-01T00:00:00+00:00"))
    conn.commit()
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    assert preview["eligible"] is False
    expected = {"assessment": "claim_assessment_dependency", "edge": "claim_graph_dependency",
                "evidence": "evidence_dependency"}[dependency]
    assert expected in preview["blockers"]


@pytest.mark.parametrize("field", ["claim_id", "claim_text", "status", "memory_id", "memory_type", "archived"])
def test_physical_json_and_runtime_type_guards(isolated_memory, field):
    db_store.init_db()
    _install_fixture(with_bystander=False)
    conn = db_store.get_connection()
    if field in {"claim_id", "claim_text", "status"}:
        conn.execute(f"UPDATE claims SET data_json=json_set(data_json, '$.{field}', 'wrong')")
    elif field == "archived":
        conn.execute("UPDATE operational_memory SET data_json=json_set(data_json, '$.validity_state', 'active')")
    else:
        conn.execute(f"UPDATE operational_memory SET data_json=json_set(data_json, '$.{field}', 'wrong')")
    conn.commit()
    assert cleanup.cleanup_placeholder_claims(["claim_stub"])["eligible"] is False


@pytest.mark.parametrize("relation", ["contradicts", "supersedes"])
def test_real_claim_relationships_block(isolated_memory, relation):
    db_store.init_db()
    _install_fixture()
    conn = db_store.get_connection()
    conn.execute(f"UPDATE claims SET data_json=json_set(data_json, '$.{relation}', json('[\"claim_stub\"]')) "
                 "WHERE claim_id='claim_bystander'")
    conn.commit()
    assert "other_claim_dependency" in cleanup.cleanup_placeholder_claims(["claim_stub"])["blockers"]


@pytest.mark.parametrize("field,value,blocker", [
    ("evidence_ids", ["evidence_real"], "claim_has_provenance:claim_stub"),
])
def test_claim_approval_is_not_inferred_from_placeholder_text(isolated_memory, field, value, blocker):
    db_store.init_db()
    _install_fixture(with_bystander=False)
    conn = db_store.get_connection()
    conn.execute(f"UPDATE claims SET data_json=json_set(data_json, '$.{field}', json(?))",
                 (_canonical(value),))
    conn.commit()
    assert blocker in cleanup.cleanup_placeholder_claims(["claim_stub"])["blockers"]


def test_terminal_history_allowed_but_active_reference_blocked(isolated_memory):
    db_store.init_db()
    _install_fixture(with_bystander=False)
    conn = db_store.get_connection()
    assert cleanup.cleanup_placeholder_claims(["claim_stub"])["eligible"] is True
    conn.execute(
        "INSERT INTO jobs(job_id,task_type,payload,status,retries,created_at,updated_at) "
        "VALUES ('active','ingest',?,'awaiting_subagent',0,'2026-08-01','2026-08-01')",
        (_canonical({"claim_id": "claim_stub"}),),
    )
    conn.commit()
    assert "job_dependency" in cleanup.cleanup_placeholder_claims(["claim_stub"])["blockers"]


@pytest.mark.parametrize("field,value,blocker", [
    ("validity_reasons", ["infrastructure_artifact:test"], "memory_preserved_infrastructure_artifact:memory_stub"),
    ("text", "A separate retained observation.", "memory_claim_text_mismatch:memory_stub"),
])
def test_unrelated_forensic_or_edited_projection_is_ineligible(isolated_memory, monkeypatch, field, value, blocker):
    from vector_lake import tool_projection
    db_store.init_db()
    _install_fixture()
    conn = db_store.get_connection()
    memory = json.loads(conn.execute("SELECT data_json FROM operational_memory WHERE memory_id='memory_stub'").fetchone()[0])
    memory[field] = value
    conn.execute("UPDATE operational_memory SET data_json=? WHERE memory_id='memory_stub'", (_canonical(memory),))
    conn.commit()
    before = _state_rows()
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    assert not preview["eligible"] and blocker in preview["blockers"]
    monkeypatch.setattr(tool_projection, "create_maintenance_backup", lambda *_: pytest.fail("ineligible preview must not start backup"))
    with pytest.raises(ValueError, match="ineligible"):
        cleanup.cleanup_placeholder_claims(["claim_stub"], dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _state_rows() == before


def test_derived_timeline_id_blocks_before_backup(isolated_memory, monkeypatch):
    from vector_lake.tool_timeline import _event_from_claim_row
    from vector_lake import tool_projection
    db_store.init_db()
    _install_fixture()
    conn = db_store.get_connection()
    event = _event_from_claim_row(conn.execute("SELECT * FROM claims WHERE claim_id='claim_stub'").fetchone())
    conn.execute("INSERT INTO timeline_events (id,event_date,description,action) VALUES (?,?,?,?)",
                 (event["id"], event["event_date"], event["description"], event["action"]))
    conn.commit()
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    assert not preview["eligible"] and "timeline_dependency" in preview["blockers"]
    before = _state_rows()
    monkeypatch.setattr(tool_projection, "create_maintenance_backup", lambda *_: pytest.fail("timeline dependency must block before backup"))
    with pytest.raises(ValueError, match="ineligible"):
        cleanup.cleanup_placeholder_claims(["claim_stub"], dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _state_rows() == before
    assert conn.execute("SELECT id FROM timeline_events WHERE id=?", (event["id"],)).fetchone()


def _state_rows():
    conn = db_store.get_connection()
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("claims", "operational_memory", "governance_queue", "claim_versions",
                          "canonical_identities", "entities", "sources", "evidence")}


def test_backup_generation_mismatch_does_not_delete(isolated_memory):
    from vector_lake.tool_projection import create_maintenance_backup
    db_store.init_db()
    _install_fixture()
    indexer.generate_index()
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    backup = create_maintenance_backup("cleanup_test")
    generations = dict(preview["runtime_generations"])
    generations["claims"] += 1
    before = _state_rows()
    with pytest.raises(ValueError, match="backup_generation_mismatch"):
        cleanup._validate_backup(backup, generations)
    assert _state_rows() == before


def test_change_during_backup_validation_is_fenced(isolated_memory, monkeypatch):
    db_store.init_db()
    _install_fixture()
    indexer.generate_index()
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    verify = cleanup._validate_backup
    raced = {}
    def change_after_verification(path, expected):
        result = verify(path, expected)
        with db_store.transaction() as conn:
            conn.execute("UPDATE governance_queue SET data_json=json_set(data_json, '$.reason', 'concurrent edit') WHERE item_id='queue_stub'")
        raced.update(_state_rows())
        return result
    monkeypatch.setattr(cleanup, "_validate_backup", change_after_verification)
    with pytest.raises(RuntimeError, match="changed after preview"):
        cleanup.cleanup_placeholder_claims(["claim_stub"], dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _state_rows() == raced


def test_injected_late_failure_rolls_back_claim_memory_and_queue(
    isolated_memory, monkeypatch,
):
    db_store.init_db()
    _install_fixture(with_bystander=False)
    indexer.generate_index()
    preview = cleanup.cleanup_placeholder_claims(["claim_stub"])
    before = _state_rows()
    monkeypatch.setattr(
        cleanup, "_late_apply_hook",
        lambda: (_ for _ in ()).throw(RuntimeError("injected late failure")),
    )
    with pytest.raises(RuntimeError, match="injected late failure"):
        cleanup.cleanup_placeholder_claims(
            ["claim_stub"], dry_run=False,
            confirmation=preview["candidate_fingerprint"],
        )
    conn = db_store.get_connection()
    assert conn.execute("SELECT 1 FROM claims WHERE claim_id='claim_stub'").fetchone()
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='memory_stub'").fetchone()
    queue = json.loads(conn.execute(
        "SELECT data_json FROM governance_queue WHERE item_id='queue_stub'"
    ).fetchone()[0])
    assert queue["status"] == "acknowledged"
    assert _state_rows() == before
