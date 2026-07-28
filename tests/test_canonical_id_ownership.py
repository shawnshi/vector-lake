from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from vector_lake import db_store, governance_store


def _change_set(
    page_key: str,
    *,
    entity_id: str,
    claim_id: str,
    title: str | None = None,
    heading: str = "Facts",
    block_index: int = 1,
    claim_text: str = "Stable claim",
    evidence_id: str | None = None,
    evidence_heading: str = "Evidence",
    evidence_block_index: int = 1,
) -> dict:
    title = title or page_key
    entity = {
        "entity_id": entity_id,
        "id": entity_id,
        "page_key": page_key,
        "canonical_name": title,
        "title": title,
        "type": "concept",
        "status": "Active",
        "aliases": [],
        "sources": [],
    }
    claim = {
        "claim_id": claim_id,
        "claim_text": claim_text,
        "claim_type": "assertion",
        "claim_scope": "block",
        "status": "Active",
        "confidence": 0.8,
        "subject_entity_ids": [entity_id],
        "evidence_ids": [],
        "source_ids": [],
        "locator": {
            "page_key": page_key,
            "heading": heading,
            "block_index": block_index,
        },
        "source_page": f"{page_key}.md",
    }
    evidence = []
    if evidence_id is not None:
        evidence.append(
            {
                "evidence_id": evidence_id,
                "evidence_type": "quote",
                "quote": "Stable evidence",
                "source_id": "",
                "claim_ids": [claim_id],
                "locator": {
                    "page_key": page_key,
                    "heading": evidence_heading,
                    "block_index": evidence_block_index,
                },
            }
        )
    return {
        "affected_pages": [f"{page_key}.md"],
        "proposed_entities": [entity],
        "proposed_claims": [claim],
        "proposed_evidence": evidence,
        "proposed_source_updates": [],
        "proposed_source_artifacts": [],
        "proposed_extraction_runs": [],
        "proposed_edges": [],
    }


def _canonical_json(table: str, id_field: str, record_id: str) -> dict:
    row = db_store.get_connection().execute(
        f"SELECT data_json FROM {table} WHERE {id_field} = ?",
        (record_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["data_json"])


def test_same_page_and_locator_allow_stable_id_update(isolated_memory):
    db_store.init_db()
    first = _change_set(
        "Concept_Alpha",
        entity_id="entity_stable",
        claim_id="claim_stable",
    )
    governance_store.apply_change_set(first)

    updated = _change_set(
        "Concept_Alpha",
        entity_id="entity_stable",
        claim_id="claim_stable",
        title="Alpha Updated",
        claim_text="Updated stable claim",
    )
    governance_store.apply_change_set(updated)

    assert (
        _canonical_json("entities", "entity_id", "entity_stable")["canonical_name"]
        == "Alpha Updated"
    )
    assert (
        _canonical_json("claims", "claim_id", "claim_stable")["claim_text"]
        == "Updated stable claim"
    )


def test_entity_id_cannot_move_to_another_page(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_global",
            claim_id="claim_alpha",
        )
    )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"entity_id 'entity_global'.*Concept_Alpha.*Concept_Beta",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Beta",
                entity_id="entity_global",
                claim_id="claim_beta",
            )
        )

    assert (
        _canonical_json("entities", "entity_id", "entity_global")["page_key"]
        == "Concept_Alpha"
    )
    assert db_store.get_connection().execute(
        "SELECT 1 FROM claims WHERE claim_id = 'claim_beta'"
    ).fetchone() is None


def test_claim_id_cannot_move_to_another_locator(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_global",
        )
    )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"claim_id 'claim_global'.*Concept_Alpha.*Concept_Beta",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Beta",
                entity_id="entity_beta",
                claim_id="claim_global",
            )
        )

    assert (
        _canonical_json("claims", "claim_id", "claim_global")["locator"]["page_key"]
        == "Concept_Alpha"
    )
    assert db_store.get_connection().execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_beta'"
    ).fetchone() is None


def test_claim_id_can_follow_locator_edits_within_the_same_page(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_stable_page",
            heading="Facts",
            block_index=1,
        )
    )

    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_stable_page",
            title="Updated In Place",
            heading="Decisions",
            block_index=7,
            claim_text="Updated claim",
        )
    )

    entity = _canonical_json("entities", "entity_id", "entity_alpha")
    claim = _canonical_json("claims", "claim_id", "claim_stable_page")
    assert entity["canonical_name"] == "Updated In Place"
    assert claim["claim_text"] == "Updated claim"
    assert claim["locator"] == {
        "page_key": "Concept_Alpha",
        "heading": "Decisions",
        "block_index": 7,
    }

def test_identity_registry_reserves_deleted_entity_id(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_reserved",
            claim_id="claim_alpha",
        )
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM entities WHERE entity_id = 'entity_reserved'")

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"entity_id 'entity_reserved'.*Concept_Alpha.*Concept_Beta",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Beta",
                entity_id="entity_reserved",
                claim_id="claim_beta",
            )
        )

    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_reserved'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT page_key FROM entity_identities WHERE entity_id = 'entity_reserved'"
    ).fetchone()["page_key"] == "Concept_Alpha"


def test_legacy_claim_without_locator_fails_closed(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    legacy = {
        "claim_id": "claim_legacy",
        "claim_text": "Legacy claim without locator",
        "status": "Active",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims "
            "(claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                legacy["claim_id"],
                legacy["claim_text"],
                legacy["status"],
                json.dumps(legacy),
                "2026-07-27T00:00:00+00:00",
            ),
        )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"claim_id 'claim_legacy'.*no locator owner",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Alpha",
                entity_id="entity_alpha",
                claim_id="claim_legacy",
            )
        )

    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_alpha'"
    ).fetchone() is None
    assert _canonical_json("claims", "claim_id", "claim_legacy") == legacy


def test_claim_registry_reserves_id_after_current_and_versions_are_deleted(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_reserved_history",
        )
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM claims WHERE claim_id = 'claim_reserved_history'")
        conn.execute(
            "DELETE FROM claim_versions WHERE claim_id = 'claim_reserved_history'"
        )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"claim_id 'claim_reserved_history'.*identity page.*Concept_Alpha.*Concept_Beta",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Beta",
                entity_id="entity_beta",
                claim_id="claim_reserved_history",
            )
        )

    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_beta'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT page_key FROM canonical_identities "
        "WHERE record_kind = 'claim' AND record_id = 'claim_reserved_history'"
    ).fetchone()["page_key"] == "Concept_Alpha"
    assert conn.execute(
        "SELECT 1 FROM claim_versions WHERE claim_id = 'claim_reserved_history'"
    ).fetchone() is None


def test_evidence_id_cannot_overwrite_another_page_and_batch_rolls_back(
    isolated_memory,
):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_alpha",
            evidence_id="evidence_global",
        )
    )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"evidence_id 'evidence_global'.*Concept_Alpha.*Concept_Beta",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Beta",
                entity_id="entity_beta",
                claim_id="claim_beta",
                evidence_id="evidence_global",
            )
        )

    conn = db_store.get_connection()
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_beta'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM claims WHERE claim_id = 'claim_beta'"
    ).fetchone() is None
    evidence = _canonical_json(
        "evidence", "evidence_id", "evidence_global"
    )
    assert evidence["locator"]["page_key"] == "Concept_Alpha"


def test_evidence_registry_reserves_id_after_current_and_versions_are_deleted(
    isolated_memory,
):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_alpha",
            evidence_id="evidence_reserved_history",
        )
    )
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "DELETE FROM evidence WHERE evidence_id = 'evidence_reserved_history'"
        )
        conn.execute(
            "DELETE FROM evidence_versions "
            "WHERE evidence_id = 'evidence_reserved_history'"
        )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"evidence_id 'evidence_reserved_history'.*identity page.*Concept_Alpha.*Concept_Beta",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Beta",
                entity_id="entity_beta",
                claim_id="claim_beta",
                evidence_id="evidence_reserved_history",
            )
        )

    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_beta'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT page_key FROM canonical_identities "
        "WHERE record_kind = 'evidence' "
        "AND record_id = 'evidence_reserved_history'"
    ).fetchone()["page_key"] == "Concept_Alpha"
    assert conn.execute(
        "SELECT 1 FROM evidence_versions "
        "WHERE evidence_id = 'evidence_reserved_history'"
    ).fetchone() is None

def test_evidence_id_can_follow_locator_edits_within_same_page(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_alpha",
            evidence_id="evidence_stable_page",
            evidence_heading="Evidence",
            evidence_block_index=1,
        )
    )

    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_alpha",
            evidence_id="evidence_stable_page",
            evidence_heading="Updated Evidence",
            evidence_block_index=9,
        )
    )

    evidence = _canonical_json(
        "evidence", "evidence_id", "evidence_stable_page"
    )
    assert evidence["locator"] == {
        "page_key": "Concept_Alpha",
        "heading": "Updated Evidence",
        "block_index": 9,
    }

def test_batch_conflict_rejects_all_page_changes(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_global",
            title="Alpha Original",
        )
    )

    valid_update = _change_set(
        "Concept_Alpha",
        entity_id="entity_alpha",
        claim_id="claim_global",
        title="Alpha Should Not Commit",
    )
    conflicting_page = _change_set(
        "Concept_Beta",
        entity_id="entity_beta",
        claim_id="claim_global",
    )
    with pytest.raises(governance_store.CanonicalIdOwnershipError):
        governance_store.apply_change_sets_batch([valid_update, conflicting_page])

    assert (
        _canonical_json("entities", "entity_id", "entity_alpha")["canonical_name"]
        == "Alpha Original"
    )
    assert db_store.get_connection().execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_beta'"
    ).fetchone() is None
def test_same_page_locator_change_keeps_append_only_identity_row(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_identity_stable",
            heading="Facts",
            block_index=1,
        )
    )
    conn = db_store.get_connection()
    before = tuple(
        conn.execute(
            "SELECT page_key, identity_origin, data_json, recorded_at "
            "FROM canonical_identities WHERE record_kind = 'claim' "
            "AND record_id = 'claim_identity_stable'"
        ).fetchone()
    )

    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_identity_stable",
            heading="Decisions",
            block_index=8,
            claim_text="Changed without moving ownership",
        )
    )
    after = tuple(
        conn.execute(
            "SELECT page_key, identity_origin, data_json, recorded_at "
            "FROM canonical_identities WHERE record_kind = 'claim' "
            "AND record_id = 'claim_identity_stable'"
        ).fetchone()
    )

    assert after == before
    assert after[0:2] == ("Concept_Alpha", "canonical_write")


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM canonical_identities WHERE record_kind = 'claim'",
        "UPDATE canonical_identities SET page_key = 'Concept_Beta' "
        "WHERE record_kind = 'claim'",
    ],
)
def test_identity_registry_rejects_update_and_delete(isolated_memory, statement):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_alpha",
            claim_id="claim_append_only",
        )
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db_store.transaction() as conn:
            conn.execute(statement)

    assert db_store.get_connection().execute(
        "SELECT page_key FROM canonical_identities "
        "WHERE record_kind = 'claim' AND record_id = 'claim_append_only'"
    ).fetchone()["page_key"] == "Concept_Alpha"


def test_corrupt_identity_registry_fails_closed_before_canonical_write(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO canonical_identities "
            "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
            "VALUES ('claim', 'claim_corrupt', 'Concept_Alpha', "
            "'manual_seed', '{', '2026-07-27T00:00:00+00:00')"
        )

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match=r"claim_id 'claim_corrupt'.*invalid identity registry metadata",
    ):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Alpha",
                entity_id="entity_corrupt",
                claim_id="claim_corrupt",
            )
        )

    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = 'entity_corrupt'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM claims WHERE claim_id = 'claim_corrupt'"
    ).fetchone() is None


def test_identity_reservations_roll_back_with_later_canonical_failure(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("injected canonical failure")

    monkeypatch.setattr(governance_store, "_upsert_canonical_records", fail_upsert)
    with pytest.raises(RuntimeError, match="injected canonical failure"):
        governance_store.apply_change_set(
            _change_set(
                "Concept_Alpha",
                entity_id="entity_rollback",
                claim_id="claim_rollback",
                evidence_id="evidence_rollback",
            )
        )

    conn = db_store.get_connection()
    assert conn.execute(
        "SELECT 1 FROM canonical_identities WHERE record_id IN (?, ?)",
        ("claim_rollback", "evidence_rollback"),
    ).fetchone() is None
def test_legacy_full_map_claim_and_evidence_writers_are_closed(isolated_memory):
    db_store.init_db()
    change_set = _change_set(
        "Concept_Alpha",
        entity_id="entity_legacy_writer",
        claim_id="claim_legacy_writer",
        evidence_id="evidence_legacy_writer",
    )
    claim = change_set["proposed_claims"][0]
    evidence = change_set["proposed_evidence"][0]

    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match="Full-map claim writes are disabled",
    ):
        governance_store.save_claims(
            {"items": {claim["claim_id"]: claim}}
        )
    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match="Full-map evidence writes are disabled",
    ):
        governance_store.save_evidence(
            {"items": {evidence["evidence_id"]: evidence}}
        )
    with pytest.raises(
        governance_store.CanonicalIdOwnershipError,
        match="Full-map writes to claims are disabled",
    ):
        governance_store._save_db_map(
            "claims",
            "claim_id",
            {"items": {claim["claim_id"]: claim}},
        )

    conn = db_store.get_connection()
    assert conn.execute(
        "SELECT 1 FROM claims WHERE claim_id = 'claim_legacy_writer'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM evidence WHERE evidence_id = 'evidence_legacy_writer'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM canonical_identities WHERE record_id IN (?, ?)",
        ("claim_legacy_writer", "evidence_legacy_writer"),
    ).fetchone() is None


def test_identity_registry_rejects_all_duplicate_insert_conflict_modes(
    isolated_memory,
):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_replace",
            claim_id="claim_replace",
        )
    )
    conn = db_store.get_connection()
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    before = tuple(
        conn.execute(
            "SELECT record_kind, record_id, page_key, identity_origin, "
            "data_json, recorded_at FROM canonical_identities "
            "WHERE record_kind = 'claim' AND record_id = 'claim_replace'"
        ).fetchone()
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db_store.transaction():
            conn.execute(
                "INSERT OR IGNORE INTO canonical_identities "
                "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                before,
            )
    assert tuple(
        conn.execute(
            "SELECT record_kind, record_id, page_key, identity_origin, "
            "data_json, recorded_at FROM canonical_identities "
            "WHERE record_kind = 'claim' AND record_id = 'claim_replace'"
        ).fetchone()
    ) == before

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db_store.transaction():
            conn.execute(
                "INSERT OR REPLACE INTO canonical_identities "
                "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                before,
            )
    assert tuple(
        conn.execute(
            "SELECT record_kind, record_id, page_key, identity_origin, "
            "data_json, recorded_at FROM canonical_identities "
            "WHERE record_kind = 'claim' AND record_id = 'claim_replace'"
        ).fetchone()
    ) == before


@pytest.mark.parametrize(
    ("field_index", "replacement"),
    [
        (2, "Concept_Beta"),
        (3, "rewritten_origin"),
        (4, "{}"),
        (5, "2099-01-01T00:00:00+00:00"),
        (None, None),
    ],
)
def test_raw_sqlite_replace_cannot_change_any_registry_value(
    isolated_memory,
    field_index,
    replacement,
):
    db_store.init_db()
    governance_store.apply_change_set(
        _change_set(
            "Concept_Alpha",
            entity_id="entity_raw_replace",
            claim_id="claim_raw_replace",
        )
    )
    path = db_store.get_db_path()
    before_row = tuple(
        db_store.get_connection().execute(
            "SELECT rowid, record_kind, record_id, page_key, identity_origin, "
            "data_json, recorded_at FROM canonical_identities "
            "WHERE record_kind = 'claim' AND record_id = 'claim_raw_replace'"
        ).fetchone()
    )
    before = before_row[1:]
    db_store.close_all_connections()

    replacement_row = list(before)
    if field_index is not None:
        replacement_row[field_index] = replacement
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "INSERT OR REPLACE INTO canonical_identities "
                "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                replacement_row,
            )
        raw.rollback()
        assert tuple(
            raw.execute(
                "SELECT rowid, record_kind, record_id, page_key, identity_origin, "
                "data_json, recorded_at FROM canonical_identities "
                "WHERE record_kind = 'claim' AND record_id = 'claim_raw_replace'"
            ).fetchone()
        ) == before_row
    finally:
        raw.close()

def test_cached_identity_validation_ignores_unrelated_local_writes(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    calls = 0
    real_validate = db_store._validate_canonical_identity_coverage

    def counted_validate(conn):
        nonlocal calls
        calls += 1
        return real_validate(conn)

    monkeypatch.setattr(
        db_store,
        "_validate_canonical_identity_coverage",
        counted_validate,
    )
    with db_store.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, status) VALUES ('job_unrelated', 'queued')"
        )
    db_store.init_db()
    assert calls == 0

    governance_store.apply_change_set(
        _change_set(
            "Concept_Relevant",
            entity_id="entity_relevant",
            claim_id="claim_relevant",
        )
    )
    db_store.init_db()
    assert calls == 1


def test_cached_identity_validation_ignores_unrelated_cross_connection_writes(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    calls = 0
    real_validate = db_store._validate_canonical_identity_coverage

    def counted_validate(conn):
        nonlocal calls
        calls += 1
        return real_validate(conn)

    monkeypatch.setattr(
        db_store,
        "_validate_canonical_identity_coverage",
        counted_validate,
    )
    external = sqlite3.connect(db_store.get_db_path())
    try:
        external.execute(
            "INSERT INTO jobs (job_id, status) VALUES ('job_external', 'queued')"
        )
        external.commit()
    finally:
        external.close()

    db_store.init_db()
    assert calls == 0


def test_cached_identity_validation_detects_relevant_cross_connection_writes(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    calls = 0
    real_validate = db_store._validate_canonical_identity_coverage

    def counted_validate(conn):
        nonlocal calls
        calls += 1
        return real_validate(conn)

    monkeypatch.setattr(
        db_store,
        "_validate_canonical_identity_coverage",
        counted_validate,
    )
    errors = []

    def write_relevant_identity():
        try:
            governance_store.apply_change_set(
                _change_set(
                    "Concept_External-Relevant",
                    entity_id="entity_external_relevant",
                    claim_id="claim_external_relevant",
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            db_store.close_connection()

    writer = threading.Thread(target=write_relevant_identity)
    writer.start()
    writer.join(timeout=10)

    assert writer.is_alive() is False
    assert errors == []
    db_store.init_db()
    assert calls == 1

def test_cached_identity_validation_caches_only_stable_snapshot(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    db_store._IDENTITY_VALIDATION_TOKENS.pop(id(conn), None)
    token_before = (1, 1, (("claims", 1),))
    token_after = (1, 2, (("claims", 2),))
    tokens = iter((token_before, token_after, token_after, token_after))
    coverage_calls = 0

    monkeypatch.setattr(
        db_store,
        "_identity_validation_token",
        lambda _conn: next(tokens),
    )
    monkeypatch.setattr(
        db_store,
        "_validate_canonical_identity_registry",
        lambda _conn: None,
    )

    def count_coverage(_conn):
        nonlocal coverage_calls
        coverage_calls += 1

    monkeypatch.setattr(
        db_store,
        "_validate_canonical_identity_coverage",
        count_coverage,
    )

    db_store._validate_cached_identity_state(conn)

    assert coverage_calls == 2
    assert db_store._IDENTITY_VALIDATION_TOKENS[id(conn)] == token_after


def test_concurrent_cross_page_claim_reuse_has_one_winner(isolated_memory):
    db_store.init_db()
    barrier = threading.Barrier(2)

    def publish(page_key: str) -> tuple[str, str]:
        barrier.wait(timeout=5)
        try:
            governance_store.apply_change_set(
                _change_set(
                    page_key,
                    entity_id=f"entity_{page_key}",
                    claim_id="claim_concurrent_global",
                )
            )
        except governance_store.CanonicalIdOwnershipError:
            return "rejected", page_key
        return "published", page_key

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(publish, ("Concept_Alpha", "Concept_Beta"))
        )

    published = [page for status, page in results if status == "published"]
    rejected = [page for status, page in results if status == "rejected"]
    assert len(published) == len(rejected) == 1

    conn = db_store.get_connection()
    owner = conn.execute(
        "SELECT page_key FROM canonical_identities WHERE record_kind = 'claim' "
        "AND record_id = 'claim_concurrent_global'"
    ).fetchone()["page_key"]
    claim_page = json.loads(
        conn.execute(
            "SELECT data_json FROM claims WHERE claim_id = 'claim_concurrent_global'"
        ).fetchone()["data_json"]
    )["locator"]["page_key"]
    assert owner == claim_page == published[0]
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id = ?",
        (f"entity_{rejected[0]}",),
    ).fetchone() is None
