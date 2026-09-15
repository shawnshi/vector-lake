"""Change-set identity (design A): a key is idempotent over the semantic delta.

The key used to be derived only from ``(origin, page_key, sha1(content))`` while
``record_prepared_change_sets`` rejects a re-used key whose payload digest
differs.  Because ``proposed_extraction_runs[].recorded_at`` is stamped with the
wall clock, the payload is not reproducible for unchanged content, so a
re-attempt was *guaranteed* to collide with its own history and the source became
permanently un-ingestible (two sources were wedged on 2026-09-15).

The identity digest is now taken over the canonical payload with pure
bookkeeping fields removed, and it participates in both the key derivation and
the guard's comparison.  The persisted payload keeps its real bytes; only the
*identity* ignores the bookkeeping.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import db_store, governance_store
from vector_lake.governance_store import (
    ChangeSetIdempotencyConflict,
    _change_set_identity_digest_for,
    _change_set_payload_digest,
    _canonical_change_set_payload,
)

PAGE = "Concept_Identity-Design-A.md"


def _content(body: str) -> str:
    return (
        "---\n"
        "id: concept_identity_a\n"
        "title: Identity Design A\n"
        "type: concept\n"
        "domain: General\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories: [Uncategorized]\n"
        "created: 2026-07-21T00:00:00+00:00\n"
        "updated: 2026-07-21T00:00:00+00:00\n"
        "sources: []\n"
        "---\n"
        "## 1. 编译事实\n"
        f"{body}\n\n"
        "## 2. 证据时间线\n"
    )


def _manifest(change_set_id: str) -> dict:
    row = db_store.get_connection().execute(
        "SELECT data_json FROM change_sets WHERE change_set_id = ?",
        (change_set_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["data_json"])


def _with_extra_claim(monkeypatch, claim_id: str) -> None:
    real_extract = governance_store.extract_page_objects

    def altered_extract(*args, **kwargs):
        extracted = real_extract(*args, **kwargs)
        return {
            **extracted,
            "claims": [
                *extracted.get("claims", []),
                {
                    "claim_id": claim_id,
                    "claim_text": "Extraction changed but page content did not.",
                    "claim_type": "assertion",
                    "status": "active",
                    "source_page": PAGE,
                    "locator": {"page_key": PAGE[:-3]},
                    "evidence_ids": [],
                },
            ],
        }

    monkeypatch.setattr(governance_store, "extract_page_objects", altered_extract)


def test_identity_digest_is_reproducible_while_the_payload_is_not():
    """The tripwire.  If this ever fails, a new wall-clock field crept in and the
    identity needs extending -- otherwise the wedge returns silently."""
    content = _content("Claim body.")

    first = governance_store.prepare_change_set_from_content(PAGE, content, "origin")
    second = governance_store.prepare_change_set_from_content(PAGE, content, "origin")

    assert _change_set_identity_digest_for(first) == _change_set_identity_digest_for(
        second
    )
    assert first["idempotency_key"] == second["idempotency_key"]

    _payload_a, bytes_a = _canonical_change_set_payload(first)
    _payload_b, bytes_b = _canonical_change_set_payload(second)
    assert _change_set_payload_digest(bytes_a) != _change_set_payload_digest(bytes_b), (
        "the persisted payload keeps its real bytes, so the raw digest still "
        "differs; that is exactly why the identity cannot use it"
    )


def test_normalization_covers_only_the_bookkeeping_timestamp():
    """Semantics-bearing fields must still change the identity."""
    content = _content("Claim body.")
    base = governance_store.prepare_change_set_from_content(PAGE, content, "origin")

    def digest_with(**run_overrides):
        mutated = {**base}
        run = dict(base["proposed_extraction_runs"][0])
        run.update(run_overrides)
        mutated["proposed_extraction_runs"] = [run]
        return _change_set_identity_digest_for(mutated)

    pristine = _change_set_identity_digest_for(base)

    # Pure bookkeeping: ignored.
    assert digest_with(recorded_at="2000-01-01T00:00:00+00:00") == pristine

    # Meaning-bearing: part of the identity.
    for field in (
        "run_id",
        "extractor_name",
        "extractor_version",
        "model_name",
        "model_version",
        "parser_name",
        "parser_version",
        "prompt_version",
        "input_fingerprint",
    ):
        assert digest_with(**{field: "changed-by-the-test"}) != pristine, field

    assert set(governance_store._CHANGE_SET_IDENTITY_VOLATILE_FIELDS) == {
        "proposed_extraction_runs"
    }
    assert governance_store._CHANGE_SET_IDENTITY_VOLATILE_FIELDS[
        "proposed_extraction_runs"
    ] == ("recorded_at",)


def test_manifest_records_the_identity_digest(isolated_memory):
    content = _content("Claim body.")
    change_set = governance_store.prepare_change_set_from_content(
        PAGE, content, "origin"
    )
    governance_store.record_prepared_change_sets([change_set])

    manifest = _manifest(change_set["change_set_id"])
    descriptor = manifest["payload"]
    assert descriptor["identity_sha256"] == _change_set_identity_digest_for(change_set)
    assert descriptor["sha256"] != descriptor["identity_sha256"]


def test_identical_repeat_is_still_deduplicated(isolated_memory):
    content = _content("Claim body.")
    first = governance_store.prepare_change_set_from_content(PAGE, content, "origin")
    assert governance_store.record_prepared_change_sets([first]) == 1

    second = governance_store.prepare_change_set_from_content(PAGE, content, "origin")
    assert second["idempotency_key"] == first["idempotency_key"]
    # 0 == the owner already exists, i.e. deduplicated rather than re-inserted.
    assert governance_store.record_prepared_change_sets([second]) == 0

    count = db_store.get_connection().execute(
        "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
        (first["change_set_id"],),
    ).fetchone()[0]
    assert count == 1


def test_changed_extraction_no_longer_conflicts(isolated_memory, monkeypatch):
    """The wedged-source regression, reproduced directly."""
    content = _content("Claim body.")
    baseline = governance_store.prepare_change_set_from_content(PAGE, content, "origin")
    governance_store.record_prepared_change_sets([baseline])

    _with_extra_claim(monkeypatch, "claim_added_by_a_newer_extractor")
    rederived = governance_store.prepare_change_set_from_content(PAGE, content, "origin")

    assert rederived["idempotency_key"] != baseline["idempotency_key"], (
        "a changed semantic delta must get a new identity"
    )
    # Must not raise, and must record a second, distinct change set.
    assert governance_store.record_prepared_change_sets([rederived]) == 1

    conn = db_store.get_connection()
    rows = conn.execute(
        "SELECT COUNT(*) FROM change_set_idempotency WHERE change_set_id IN (?, ?)",
        (baseline["change_set_id"], rederived["change_set_id"]),
    ).fetchone()[0]
    assert rows == 2


def test_legacy_manifest_without_an_identity_digest_still_fails_closed(
    isolated_memory,
):
    """A manifest written before design A has no identity digest.

    It must keep the old, fail-closed comparison: the raw payload digest is not
    reproducible, so silently accepting a differing payload would let one key
    address two deltas.
    """
    content = _content("Claim body.")
    baseline = governance_store.prepare_change_set_from_content(PAGE, content, "origin")
    governance_store.record_prepared_change_sets([baseline])

    manifest = _manifest(baseline["change_set_id"])
    del manifest["payload"]["identity_sha256"]
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "UPDATE change_sets SET data_json = ? WHERE change_set_id = ?",
            (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                baseline["change_set_id"],
            ),
        )

    conflicting = dict(baseline)
    conflicting["change_set_id"] = "changeset_legacy_conflict"
    conflicting["proposed_claims"] = [
        *baseline["proposed_claims"],
        {
            "claim_id": "claim_under_the_same_key",
            "claim_text": "Same key, different payload, no identity digest.",
            "claim_type": "assertion",
            "status": "active",
            "source_page": PAGE,
            "locator": {"page_key": PAGE[:-3]},
            "evidence_ids": [],
        },
    ]

    with pytest.raises(ChangeSetIdempotencyConflict) as error:
        governance_store.record_prepared_change_sets([conflicting])

    assert "already owned by a different payload" in str(error.value)
