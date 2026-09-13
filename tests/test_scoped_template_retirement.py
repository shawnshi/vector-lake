"""Bounded template retirement preserves knowledge, pages and forensic history."""
import copy
import json
import sqlite3
from contextlib import closing, contextmanager

import pytest

from vector_lake import db_store, governance_store
from vector_lake import tool_governance_maintenance as maintenance
from vector_lake.claim_extractor import _iter_blocks, classify_non_claim_text


@pytest.mark.parametrize("subject", ["Alpha", "Concept_Alpha", "Winning Health", "病案质控"])
def test_exact_templates_require_matching_page_identity(subject):
    page = subject if subject.startswith("Concept_") else "Concept_" + subject.replace(" ", "_")
    texts = [
        f"Auto-generated stub for {subject}. (Last Reshaped: 2026-07-12)",
        f"{subject} Auto-generated stub.",
    ]
    for text in texts:
        assert classify_non_claim_text(text, page_key=page + ".md")
        assert classify_non_claim_text(text) is None
        assert classify_non_claim_text(text, page_key="Concept_Other") is None
        assert not _iter_blocks(text, page_key=page)
        assert _iter_blocks(text + " This is a substantive claim.", page_key=page)


@pytest.mark.parametrize("text", [
    "This is not an auto-generated stub.",
    "We use an Auto-generated stub.",
    "Alpha Auto-generated stub. Access requires consent.",
    "Auto-generated stub for Alpha. (Last Reshaped: pending)",
    "A clinical product uses automatically generated notes.",
])
def test_substantive_or_ambiguous_text_is_not_filtered(text):
    assert classify_non_claim_text(text, page_key="Concept_Alpha") is None


def _seed():
    db_store.init_db()
    claims = []
    for key, text in [
        ("Alpha", "Alpha Auto-generated stub."),
        ("Beta", "Beta Auto-generated stub."),
        ("Real", "Consent is required before disclosure."),
    ]:
        claims.append({
            "claim_id": "claim_" + key,
            "claim_text": text,
            "status": "Active",
            "source_ids": [], "evidence_ids": [], "subject_entity_ids": [],
            "source_page": "Concept_" + key + ".md",
            "locator": {"page_key": "Concept_" + key},
            "confidence": 0.8,
        })
    governance_store.apply_change_set({
        "affected_pages": [cl["source_page"] for cl in claims],
        "proposed_entities": [], "proposed_claims": claims,
        "proposed_evidence": [], "proposed_source_updates": [], "proposed_edges": [],
    })
    store = governance_store.load_memory_objects()
    store["items"] = {
        "memory_" + cl["claim_id"]: {
            "memory_id": "memory_" + cl["claim_id"],
            "source_claim_id": cl["claim_id"], "text": cl["claim_text"],
            "source_page": cl["source_page"], "status": "Active",
            "validity_state": "unsupported", "validity_reasons": [],
            "memory_type": "fact", "memory_score": 0.5,
        } for cl in claims
    }
    governance_store.save_memory_objects(store)
    return claims


def _preview():
    return governance_store.remediate_operational_memory_pollution(
        source_claim_ids=["claim_Alpha"], sample_size=0,
    )


def test_scoped_archive_preserves_claims_other_memories_and_history(isolated_memory):
    _seed()
    page = isolated_memory / "wiki" / "Concept_Alpha.md"
    page.parent.mkdir(exist_ok=True)
    page.write_text("Alpha Auto-generated stub.", encoding="utf-8")
    before_page = page.read_bytes()
    before_versions = [row[0] for row in db_store.get_connection().execute(
        "SELECT data_json FROM claim_versions ORDER BY data_json"
    )]
    before_claims = copy.deepcopy(governance_store.load_claims())
    before_memories = copy.deepcopy(governance_store.load_memory_objects())
    preview = _preview()
    assert preview["selected_count"] == preview["scope_claim_count"] == 1
    assert preview["sample"] == []
    assert governance_store.load_memory_objects()["items"] == before_memories["items"]
    with pytest.raises(ValueError, match="fingerprint"):
        governance_store.remediate_operational_memory_pollution(
            dry_run=False, source_claim_ids=["claim_Alpha"], confirmation="wrong",
        )
    result = governance_store.remediate_operational_memory_pollution(
        dry_run=False, source_claim_ids=["claim_Alpha"],
        confirmation=preview["candidate_fingerprint"],
    )
    assert result["archived_count"] == 1
    after = governance_store.load_memory_objects()["items"]
    assert after["memory_claim_Alpha"]["validity_state"] == "archived"
    assert after["memory_claim_Alpha"]["text"] == before_memories["items"]["memory_claim_Alpha"]["text"]
    assert after["memory_claim_Alpha"]["template_retirement"]["fingerprint"] == preview["candidate_fingerprint"]
    assert after["memory_claim_Beta"] == before_memories["items"]["memory_claim_Beta"]
    assert after["memory_claim_Real"] == before_memories["items"]["memory_claim_Real"]
    assert governance_store.load_claims()["items"] == before_claims["items"]
    assert page.read_bytes() == before_page
    assert [row[0] for row in db_store.get_connection().execute(
        "SELECT data_json FROM claim_versions ORDER BY data_json"
    )] == before_versions
    prior = after["memory_claim_Alpha"]["template_retirement"]["prior_fields"]
    assert prior == {k: v for k, v in before_memories["items"]["memory_claim_Alpha"].items()
                     if k in {"status", "validity_state", "validity_reasons", "archived_at", "updated_at", "template_retirement"}}
    assert _preview()["selected_count"] == 0
    rebuilt = governance_store.rebuild_operational_memory()["items"]
    assert rebuilt["memory_claim_Alpha"]["validity_state"] == "archived"
    assert not any(m.get("source_claim_id") == "claim_Alpha" and m.get("validity_state") != "archived" for m in rebuilt.values())


@pytest.mark.parametrize("scope", [[], ["claim_Alpha", "claim_Alpha"], ["claim_Missing"], ["claim_Real"], [True], [" claim_Alpha"], "claim_Alpha"])
def test_invalid_scope_fails_closed(isolated_memory, scope):
    _seed()
    before = copy.deepcopy(governance_store.load_memory_objects())
    with pytest.raises(ValueError):
        governance_store.remediate_operational_memory_pollution(source_claim_ids=scope)
    assert governance_store.load_memory_objects()["items"] == before["items"]


def test_scoped_limit_and_text_drift_fail_closed(isolated_memory):
    _seed()
    with pytest.raises(ValueError, match="partial"):
        governance_store.remediate_operational_memory_pollution(source_claim_ids=["claim_Alpha"], limit=1)
    memories = governance_store.load_memory_objects()
    memories["items"]["memory_claim_Alpha"]["text"] += " Changed."
    governance_store.save_memory_objects(memories)
    with pytest.raises(ValueError, match="no longer matches"):
        _preview()


def test_transaction_recheck_detects_race(isolated_memory, monkeypatch):
    _seed()
    preview = _preview()
    original_transaction = governance_store.transaction

    @contextmanager
    def race_transaction():
        memories = governance_store.load_memory_objects()
        memories["items"]["memory_claim_Alpha"]["race_marker"] = True
        governance_store.save_memory_objects(memories)
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(governance_store, "transaction", race_transaction)
    # Avoid recursively invoking the patched transaction in the injected competing write.
    original_save = governance_store.save_memory_objects

    def save_without_race(data):
        with monkeypatch.context() as scoped:
            scoped.setattr(governance_store, "transaction", original_transaction)
            original_save(data)

    monkeypatch.setattr(governance_store, "save_memory_objects", save_without_race)
    with pytest.raises(RuntimeError, match="changed before archival"):
        governance_store.remediate_operational_memory_pollution(
            dry_run=False, source_claim_ids=["claim_Alpha"],
            confirmation=preview["candidate_fingerprint"],
        )
    assert governance_store.load_memory_objects()["items"]["memory_claim_Alpha"]["validity_state"] == "unsupported"


def test_wrapper_requires_consistent_backup_before_write(isolated_memory, monkeypatch):
    _seed()
    preview = _preview()
    backup = isolated_memory / "fake_backup"
    backup.mkdir()
    (backup / "manifest.json").write_text(json.dumps({
        "complete": True, "restorable_as_consistent_canonical_projection_snapshot": False,
    }), encoding="utf-8")
    monkeypatch.setattr("vector_lake.tool_projection.require_maintenance_backup", lambda _label: str(backup))
    with pytest.raises(RuntimeError, match="consistent backup"):
        maintenance.cleanup_operational_memory(
            dry_run=False, source_claim_ids=["claim_Alpha"],
            confirmation=preview["candidate_fingerprint"],
        )
    assert governance_store.load_memory_objects()["items"]["memory_claim_Alpha"]["validity_state"] == "unsupported"
    (backup / "manifest.json").write_text(json.dumps({
        "complete": True, "restorable_as_consistent_canonical_projection_snapshot": True,
    }), encoding="utf-8")
    with closing(sqlite3.connect(backup / "vector_lake.db")) as conn:
        db_store.get_connection().backup(conn)
    result = json.loads(maintenance.cleanup_operational_memory(
        dry_run=False, source_claim_ids=["claim_Alpha"],
        confirmation=preview["candidate_fingerprint"],
    ))
    assert result["archived_count"] == 1
    assert result["backup"] == str(backup)
    assert result["backup_candidate_fingerprint"] == preview["candidate_fingerprint"]
    assert result["sample"] == []


def test_legacy_generated_stub_keeps_case_insensitive_contract():
    text = "This is an auto-generated stub page to prevent broken links from Concept_X."
    assert classify_non_claim_text(text) == "generated_stub"
    assert classify_non_claim_text(text.upper()) == "generated_stub"


def test_backup_content_must_match_confirmed_candidates(isolated_memory, monkeypatch):
    _seed()
    preview = _preview()
    backup = isolated_memory / "stale_backup"
    backup.mkdir()
    (backup / "manifest.json").write_text(json.dumps({
        "complete": True, "restorable_as_consistent_canonical_projection_snapshot": True,
    }), encoding="utf-8")
    with closing(sqlite3.connect(backup / "vector_lake.db")) as conn:
        db_store.get_connection().backup(conn)
        conn.execute("UPDATE operational_memory SET data_json = json_set(data_json, '$.changed', 1) WHERE memory_id = ?", ("memory_claim_Alpha",))
        conn.commit()
    monkeypatch.setattr("vector_lake.tool_projection.require_maintenance_backup", lambda _label: str(backup))
    with pytest.raises(RuntimeError, match="backup does not match"):
        maintenance.cleanup_operational_memory(
            dry_run=False, source_claim_ids=["claim_Alpha"],
            confirmation=preview["candidate_fingerprint"],
        )
    assert governance_store.load_memory_objects()["items"]["memory_claim_Alpha"]["validity_state"] == "unsupported"


def test_backup_connection_cannot_be_used_for_writes(isolated_memory):
    _seed()
    with pytest.raises(ValueError, match="read-only previews"):
        governance_store.remediate_operational_memory_pollution(
            dry_run=False, source_claim_ids=["claim_Alpha"],
            _read_connection=db_store.get_connection(),
        )
