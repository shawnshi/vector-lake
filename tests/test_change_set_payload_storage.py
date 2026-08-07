import hashlib
import json

import pytest

from vector_lake import db_store, governance_store


def _change_set(
    change_set_id: str,
    *,
    idempotency_key: str | None = None,
    status: str = "pending",
    claim_text: str = "bounded delta",
) -> dict:
    created_at = "2026-08-03T00:00:00+00:00"
    result = {
        "change_set_id": change_set_id,
        "idempotency_key": idempotency_key or f"idem-{change_set_id}",
        "origin": "test",
        "created_at": created_at,
        "status": status,
        "summary": "payload storage test",
        "requires_human_review": status == "pending",
        "affected_ids": ["entity-a", "claim-a"],
        "affected_pages": ["Concept_A.md"],
        "proposed_entities": [
            {
                "entity_id": "entity-a",
                "canonical_name": "A",
                "page_key": "Concept_A",
            }
        ],
        "proposed_claims": [
            {
                "claim_id": "claim-a",
                "claim_family_id": "family-a",
                "claim_text": claim_text,
                "locator": {"page_key": "Concept_A"},
            }
        ],
        "proposed_evidence": [],
        "proposed_source_updates": [],
        "proposed_source_artifacts": [],
        "proposed_extraction_runs": [],
        "proposed_edges": [],
        "write_contract": {"transactional": True},
    }
    if status == "published":
        result["published_at"] = created_at
    return result


def test_pending_change_set_uses_content_addressed_payload(isolated_memory):
    db_store.init_db()
    change_set = _change_set("changeset-pending")

    assert governance_store.record_prepared_change_sets([change_set]) == 1

    conn = db_store.get_connection()
    raw = conn.execute(
        "SELECT data_json FROM change_sets WHERE change_set_id = ?",
        (change_set["change_set_id"],),
    ).fetchone()[0]
    manifest = json.loads(raw)
    assert manifest["manifest_version"] == 2
    assert manifest["delta_kind"] == "page_replace_v1"
    assert not any(section in manifest for section in governance_store._CHANGE_SET_PAYLOAD_SECTIONS)
    assert manifest["payload"]["available"] is True
    assert manifest["payload"]["raw_bytes"] <= governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES
    assert len(raw.encode("utf-8")) <= governance_store._CHANGE_SET_MAX_MANIFEST_BYTES
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM change_set_payload_refs").fetchone()[0] == 1

    hydrated = governance_store._load_change_set_by_idempotency_key(
        change_set["idempotency_key"]
    )
    assert hydrated["proposed_claims"] == change_set["proposed_claims"]
    assert hydrated["proposed_entities"] == change_set["proposed_entities"]


def test_terminal_status_cannot_detach_an_unapplied_payload(isolated_memory):
    db_store.init_db()
    change_set = _change_set("changeset-terminal", status="published")

    with pytest.raises(
        governance_store.ChangeSetPayloadCorrupt,
        match="pending deltas only",
    ):
        governance_store.record_prepared_change_sets([change_set])

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_set_payload_refs").fetchone()[0] == 0


def test_apply_and_record_terminalizes_only_after_canonical_apply(isolated_memory):
    db_store.init_db()
    change_set = _change_set("changeset-applied")

    terminal = governance_store.apply_and_record_change_sets_batch([change_set])[0]

    conn = db_store.get_connection()
    assert terminal["status"] == "published"
    assert terminal["payload"]["available"] is False
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_set_payload_refs").fetchone()[0] == 0
    lifecycle = conn.execute(
        "SELECT status, terminal_at FROM change_set_lifecycle_v6"
    ).fetchone()
    assert lifecycle["status"] == "published"
    assert lifecycle["terminal_at"] == terminal["terminal_at"]
    assert governance_store.load_change_sets(limit=1)["items"] == [terminal]


def test_public_apply_routes_through_persisted_terminal_lifecycle(isolated_memory):
    db_store.init_db()
    change_set = _change_set("changeset-public-safe")

    result = governance_store.apply_change_set(change_set)

    conn = db_store.get_connection()
    lifecycle = conn.execute(
        "SELECT status, terminal_at FROM change_set_lifecycle_v6 "
        "WHERE change_set_id = ?",
        (change_set["change_set_id"],),
    ).fetchone()
    assert result["status"] == "published"
    assert result["payload"]["available"] is False
    assert tuple(lifecycle) == ("published", result["terminal_at"])
    assert conn.execute(
        "SELECT COUNT(*) FROM change_set_payload_refs"
    ).fetchone()[0] == 0


def test_identical_pending_deltas_share_one_payload(isolated_memory):
    db_store.init_db()
    first = _change_set("changeset-a")
    second = _change_set("changeset-b")

    assert governance_store.record_prepared_change_sets([first]) == 1
    assert governance_store.record_prepared_change_sets([second]) == 1

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM change_set_payload_refs").fetchone()[0] == 2


def test_corrupt_payload_fails_closed(isolated_memory):
    db_store.init_db()
    change_set = _change_set("changeset-corrupt")
    governance_store.record_prepared_change_sets([change_set])
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE change_set_payloads SET payload_blob = X'00', stored_bytes = 1"
        )

    with pytest.raises(governance_store.ChangeSetPayloadCorrupt):
        governance_store._load_change_set_by_idempotency_key(
            change_set["idempotency_key"]
        )
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_change_set_limits_fail_before_any_persistence(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    change_set = _change_set("changeset-too-large", claim_text="x" * 1024)
    monkeypatch.setattr(governance_store, "_CHANGE_SET_MAX_PAYLOAD_BYTES", 128)

    with pytest.raises(governance_store.ChangeSetPayloadTooLarge):
        governance_store.record_prepared_change_sets([change_set])

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_set_idempotency").fetchone()[0] == 0


def test_same_idempotency_key_with_different_delta_is_rejected(isolated_memory):
    db_store.init_db()
    first = _change_set("changeset-first", idempotency_key="shared")
    second = _change_set(
        "changeset-second",
        idempotency_key="shared",
        claim_text="different",
    )
    assert governance_store.record_prepared_change_sets([first]) == 1

    with pytest.raises(governance_store.ChangeSetIdempotencyConflict):
        governance_store.record_prepared_change_sets([second])

    conn = db_store.get_connection()
    owner = conn.execute(
        "SELECT change_set_id FROM change_set_idempotency WHERE idempotency_key = 'shared'"
    ).fetchone()[0]
    assert owner == first["change_set_id"]
    stored_hash = json.loads(
        conn.execute("SELECT data_json FROM change_sets").fetchone()[0]
    )["payload"]["sha256"]
    _payload, first_bytes = governance_store._canonical_change_set_payload(first)
    assert stored_hash == hashlib.sha256(first_bytes).hexdigest()


def test_same_hash_pending_owner_is_applied_once_then_terminal_deduplicates(
    isolated_memory,
):
    db_store.init_db()
    first = _change_set("changeset-owner", idempotency_key="retry-key")
    retry = _change_set("changeset-retry", idempotency_key="retry-key")
    assert governance_store.record_prepared_change_sets([first]) == 1

    terminal = governance_store.apply_and_record_change_sets_batch([retry])[0]
    conn = db_store.get_connection()
    counts_after_apply = {
        "claims": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
        "versions": conn.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0],
    }
    replay = governance_store.apply_and_record_change_sets_batch([retry])[0]

    assert terminal["change_set_id"] == first["change_set_id"]
    assert terminal["status"] == "published"
    assert replay == terminal
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == counts_after_apply[
        "claims"
    ]
    assert (
        conn.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        == counts_after_apply["versions"]
    )


def test_batch_page_limit_is_global_and_overlap_fails_closed(isolated_memory):
    db_store.init_db()
    first = _change_set("changeset-pages-a")
    second = _change_set("changeset-pages-b")
    first["affected_pages"] = [f"Concept_A_{index}.md" for index in range(101)]
    second["affected_pages"] = [f"Concept_B_{index}.md" for index in range(100)]

    with pytest.raises(governance_store.ChangeSetBatchTooLarge, match="batch page count"):
        governance_store.record_prepared_change_sets([first, second])

    second["affected_pages"] = ["concept_a_0.MD"]
    with pytest.raises(governance_store.ChangeSetBatchTooLarge, match="overlapping pages"):
        governance_store.record_prepared_change_sets([first, second])

    second["affected_pages"] = ["Ｃｏｎｃｅｐｔ＿Ａ＿０．ｍｄ"]
    with pytest.raises(governance_store.ChangeSetBatchTooLarge, match="overlapping pages"):
        governance_store.record_prepared_change_sets([first, second])

    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM change_sets"
    ).fetchone()[0] == 0


def test_payload_metadata_cap_rejects_before_blob_selection(isolated_memory):
    db_store.init_db()
    change_set = _change_set("changeset-oversize-metadata")
    governance_store.record_prepared_change_sets([change_set])
    conn = db_store.get_connection()
    manifest = json.loads(
        conn.execute("SELECT data_json FROM change_sets").fetchone()[0]
    )
    with db_store.transaction():
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE change_set_payloads SET raw_bytes = ?",
            (governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES + 1,),
        )
        conn.execute("PRAGMA ignore_check_constraints = OFF")
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        with pytest.raises(
            governance_store.ChangeSetPayloadCorrupt,
            match="metadata is inconsistent",
        ):
            governance_store._load_change_set_payload(conn, manifest)
    finally:
        conn.set_trace_callback(None)

    assert not any(
        statement.lstrip().casefold().startswith("select payload_blob")
        for statement in statements
    )


def test_load_change_sets_does_not_parse_large_legacy_inline_rows(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    raw = json.dumps(
        {
            "change_set_id": "legacy-large-inline",
            "status": "published",
            "created_at": "2020-01-01T00:00:00+00:00",
            "published_at": "2020-01-01T00:00:00+00:00",
            "proposed_entities": [{"body": "中" * 40_000}],
        },
        ensure_ascii=False,
    )
    with db_store.transaction():
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES ('legacy-large-inline', ?, ?)",
            (raw, "2020-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO change_set_lifecycle_v6 "
            "(change_set_id, status, created_at, terminal_at, time_source, "
            "payload_guard_sha256) VALUES (?, 'published', ?, ?, 'published_at', ?)",
            (
                "legacy-large-inline",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            ),
        )
    monkeypatch.setattr(
        governance_store,
        "_history_json_object",
        lambda *_args, **_kwargs: pytest.fail("legacy inline JSON was parsed"),
    )

    item = governance_store.load_change_sets(limit=1)["items"][0]

    assert item["change_set_id"] == "legacy-large-inline"
    assert item["legacy_inline"] is True
    assert item["payload"]["raw_bytes"] == len(raw.encode("utf-8"))
