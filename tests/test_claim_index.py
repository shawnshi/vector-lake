"""`claim_index` is only allowed to be a *performance* change.

`trace` scans every claim's text and `debt` annotates every claim, and both read a fixed handful of
fields while `load_claims` decodes all 101 323 JSON payloads to get them (3.2 s of a 4.2 s trace).
The projection carries exactly the fields those two consume, so the assertion that matters is
differential: what the projection produces must equal what a full decode produces, field by field
and verdict by verdict.  That is the same contract `operational_memory_index` is held to.

The corpus below deliberately includes the awkward shapes -- arrays, a scalar where an array is
expected, a missing key, an explicit null, an empty array -- because the projection computes counts
in SQL while the oracle uses ``len()``.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import db_store, governance_metrics

NOW = "2026-09-19T00:00:00+00:00"

CLAIMS = [
    # (claim_id, extra fields) -- the annotation inputs plus the fields the scan reads.
    ("c_active", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "claim_text": "HIS 集成平台"}),
    ("c_provisional", {"status": "Active", "confidence": 0.3, "evidence_ids": ["e1"], "review_after": "2027-01-01T00:00:00+00:00"}),
    ("c_expired_status", {"status": "Archived", "confidence": 0.9, "evidence_ids": ["e1"]}),
    ("c_expired_valid_to", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "valid_to": "2020-01-01T00:00:00+00:00"}),
    ("c_review_due", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "review_after": "2020-01-01T00:00:00+00:00"}),
    ("c_conflicted", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "contradicts": ["other"]}),
    ("c_unsupported", {"status": "Active", "confidence": 0.9, "evidence_ids": []}),
    ("c_needs_review", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "freshness_tier": "volatile"}),
    ("c_missing_fields", {}),
    ("c_scalar_evidence", {"status": "Active", "confidence": 0.9, "evidence_ids": "not-a-list"}),
    ("c_null_evidence", {"status": "Active", "confidence": 0.9, "evidence_ids": None}),
    ("c_source_ids", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "source_ids": ["s1", "s2"]}),
    ("c_no_source_ids", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"]}),
    ("c_entities", {"status": "Active", "confidence": 0.9, "evidence_ids": ["e1"], "subject_entity_ids": ["x", "y"]}),
]


def _seed(conn) -> None:
    for claim_id, extra in CLAIMS:
        payload = {
            "claim_id": claim_id,
            "claim_type": extra.get("claim_type", "fact"),
            "validity_state": "active",
            "updated_at": NOW,
            "confidence": 0.5,
            "locator": {"page_key": "Concept_X"},
            **extra,
        }
        conn.execute(
            "INSERT OR REPLACE INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                claim_id,
                str(payload.get("claim_text") or claim_id),
                str(payload.get("status") or "Active"),
                json.dumps(payload, ensure_ascii=False),
                NOW,
            ),
        )


@pytest.fixture
def populated(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _seed(conn)
    return conn


def test_the_triggers_keep_the_projection_in_step(populated):
    conn = populated
    projected = {
        row[0] for row in conn.execute("SELECT claim_id FROM claim_index")
    }
    assert projected == {claim_id for claim_id, _ in CLAIMS}

    with db_store.transaction():
        conn.execute("DELETE FROM claims WHERE claim_id = 'c_active'")
    assert "c_active" not in {row[0] for row in conn.execute("SELECT claim_id FROM claim_index")}

    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET data_json = ? WHERE claim_id = 'c_provisional'",
            (json.dumps({"claim_id": "c_provisional", "status": "Archived", "evidence_ids": ["e"]}),),
        )
    assert conn.execute(
        "SELECT status FROM claim_index WHERE claim_id = 'c_provisional'"
    ).fetchone()[0] == "archived"


def test_the_annotation_inputs_equal_a_full_decode(populated):
    """The differential: per claim, the projected fields must reproduce the decoded ones."""
    conn = populated
    decoded = {
        row["claim_id"]: json.loads(row["data_json"])
        for row in conn.execute("SELECT claim_id, data_json FROM claims")
    }
    projection = {
        row["claim_id"]: row
        for row in conn.execute(
            "SELECT claim_id, status, confidence, freshness_tier, valid_to, review_after, "
            "evidence_count, contradicts_count, subject_entity_count, source_page, claim_text, "
            "source_ids FROM claim_index"
        )
    }

    assert set(decoded) == set(projection)
    for claim_id, claim in decoded.items():
        row = projection[claim_id]
        assert row["status"] == str(claim.get("status", "Active")).lower(), claim_id
        assert row["confidence"] == float(claim.get("confidence", 0) or 0), claim_id
        assert row["freshness_tier"] == str(claim.get("freshness_tier", "unknown")).lower(), claim_id
        assert row["valid_to"] == str(claim.get("valid_to") or ""), claim_id
        assert row["review_after"] == str(claim.get("review_after") or ""), claim_id
        assert row["source_page"] == str(claim.get("source_page") or "").lower(), claim_id
        assert row["claim_text"] == str(claim.get("claim_text") or "").lower(), claim_id
        if "source_ids" in claim:
            assert json.loads(row["source_ids"]) == claim["source_ids"], claim_id
        else:
            assert row["source_ids"] is None, claim_id


def test_the_validity_verdict_equals_the_json_decoded_one(populated):
    """End to end: the same `infer_claim_validity` verdict from either input.

    One deliberate divergence, asserted rather than hidden: a claim whose ``evidence_ids`` is
    explicitly ``null`` makes the *oracle* raise (``len(None)``), while the projection reports a
    count of 0 and yields a verdict.  No writer produces that shape, and the projection is
    strictly more robust there, so the difference is recorded instead of being papered over.
    """
    conn = populated
    divergences = []
    for row in conn.execute("SELECT claim_id, data_json FROM claims"):
        claim = json.loads(row["data_json"])
        projected = dict(
            conn.execute(
                "SELECT status, confidence, freshness_tier, valid_to, review_after, "
                "evidence_count, contradicts_count FROM claim_index WHERE claim_id = ?",
                (row["claim_id"],),
            ).fetchone()
        )
        stub = {
            "claim_id": row["claim_id"],
            "status": projected["status"],
            "confidence": projected["confidence"],
            "freshness_tier": projected["freshness_tier"],
            "valid_to": projected["valid_to"] or None,
            "review_after": projected["review_after"] or None,
            "evidence_ids": [None] * projected["evidence_count"],
            "contradicts": [None] * projected["contradicts_count"],
        }
        try:
            from_json = governance_metrics.infer_claim_validity(claim)
        except TypeError as exc:
            divergences.append((row["claim_id"], str(exc)))
            assert governance_metrics.infer_claim_validity(stub)["validity_state"], row["claim_id"]
            continue
        assert governance_metrics.infer_claim_validity(stub) == from_json, row["claim_id"]

    assert [claim_id for claim_id, _ in divergences] == ["c_null_evidence"]


def test_a_missing_key_and_an_explicit_null_are_distinguishable(populated):
    """`source_ids` absent must stay NULL, so a reader can apply the same default the JSON path did."""
    conn = populated
    assert conn.execute(
        "SELECT source_ids FROM claim_index WHERE claim_id = 'c_no_source_ids'"
    ).fetchone()[0] is None
    assert json.loads(
        conn.execute("SELECT source_ids FROM claim_index WHERE claim_id = 'c_source_ids'").fetchone()[0]
    ) == ["s1", "s2"]


def test_a_forced_rebuild_reproduces_the_same_rows(populated):
    conn = populated
    before = list(conn.execute("SELECT * FROM claim_index ORDER BY claim_id"))

    result = db_store.ensure_claim_index(conn, force=True)

    assert result["rebuilt"] == len(CLAIMS)
    assert list(conn.execute("SELECT * FROM claim_index ORDER BY claim_id")) == before


def test_the_reconciliation_fills_a_dropped_projection(populated):
    conn = populated
    before = list(conn.execute("SELECT * FROM claim_index ORDER BY claim_id"))
    with db_store.transaction():
        conn.execute("DELETE FROM claim_index")

    result = db_store.ensure_claim_index(conn)

    assert result["projected"] == 0 and result["rebuilt"] == len(CLAIMS)
    assert list(conn.execute("SELECT * FROM claim_index ORDER BY claim_id")) == before


def test_the_table_is_a_schema_sentinel(isolated_memory):
    """Without the sentinel an existing database is 'complete' and never gets the DDL."""
    assert "claim_index" in db_store._SCHEMA_SENTINELS
