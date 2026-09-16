import json

from vector_lake import db_store, governance_store
from vector_lake.tool_timeline import rebuild_timeline_events_from_claims, search_timeline_events


def test_timeline_projection_rebuilds_from_claims(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "temporal_anchor": "2026-07-13",
        "subject_entity_ids": ["Vendor_Test"],
        "source_ids": ["Source_Test"],
        "action": "Release",
        "sentiment": "positive",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "claim_timeline_1",
                "Vendor_Test released a new product.",
                "active",
                json.dumps(payload),
                "2026-07-13T00:00:00+00:00",
            ),
        )

    dry = rebuild_timeline_events_from_claims(dry_run=True)
    assert "Would rebuild 1 timeline_events" in dry
    result = rebuild_timeline_events_from_claims(dry_run=False)
    assert "Rebuilt 1 timeline_events" in result

    output = search_timeline_events(entity_name="Vendor_Test", limit=5)
    assert "2026-07-13" in output
    assert "Vendor_Test released a new product." in output
    assert "Release" in output


def _claim(claim_id: str, page_key: str, text: str, claim_type: str = "timeline-event") -> dict:
    return {
        "claim_id": claim_id,
        "claim_text": text,
        "claim_type": claim_type,
        "status": "active",
        "temporal_anchor": "2026-07-14",
        "subject_entity_ids": ["Vendor_Shared"],
        "source_ids": [f"Source_{page_key}"],
        "locator": {"page_key": page_key},
        "source_page": f"{page_key}.md",
        "updated_at": "2026-07-14T00:00:00+00:00",
    }


def _apply_page(page_key: str, claims: list[dict]):
    with db_store.transaction():
        governance_store.apply_change_sets_batch([{
            "affected_pages": [f"{page_key}.md"],
            "proposed_entities": [],
            "proposed_claims": claims,
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])


def test_timeline_projection_tracks_add_update_and_type_conversion(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    first = _claim("claim_delta", "Source_Delta", "First timeline value")
    _apply_page("Source_Delta", [first])
    assert conn.execute("SELECT description FROM timeline_events").fetchone()[0] == "First timeline value"

    ordinary = _claim("claim_delta", "Source_Delta", "Now an ordinary claim", claim_type="assertion")
    _apply_page("Source_Delta", [ordinary])
    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 0

    restored = _claim("claim_delta", "Source_Delta", "Restored timeline value")
    _apply_page("Source_Delta", [restored])
    assert conn.execute("SELECT description FROM timeline_events").fetchone()[0] == "Restored timeline value"


def test_page_delete_only_removes_its_own_timeline_event(isolated_memory):
    db_store.init_db()
    first = _claim("claim_page_a", "Source_PageA", "Event A")
    second = _claim("claim_page_b", "Source_PageB", "Event B")
    _apply_page("Source_PageA", [first])
    _apply_page("Source_PageB", [second])

    db_store.delete_node_cascade("Source_PageA")

    rows = db_store.get_connection().execute(
        "SELECT description FROM timeline_events ORDER BY description"
    ).fetchall()
    assert [row["description"] for row in rows] == ["Event B"]


def test_timeline_search_falls_back_when_projection_count_is_stale(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    first = _claim("claim_current", "Source_Current", "Canonical current event")
    _apply_page("Source_Current", [first])
    with db_store.transaction():
        conn.execute("DELETE FROM timeline_events")
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale", "2000-01-01", "old", "neutral", "Stale event", "", "", "", "2000-01-01"),
        )
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "claim_second",
                "Second canonical event",
                "active",
                json.dumps(_claim("claim_second", "Source_Second", "Second canonical event")),
                "2026-07-14T01:00:00+00:00",
            ),
        )

    output = search_timeline_events(limit=10)
    assert "Canonical current event" in output
    assert "Second canonical event" in output
    assert "Stale event" not in output


def test_timeline_search_rejects_equal_count_wrong_event_ids(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    current = _claim("claim_equal", "Source_Equal", "Canonical equal-count event")
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                current["claim_id"],
                current["claim_text"],
                "active",
                json.dumps(current),
                current["updated_at"],
            ),
        )
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wrong-id", "2000-01-01", "old", "neutral", "Stale equal-count event", "", "", "", "2000-01-01"),
        )

    output = search_timeline_events(limit=10)

    assert "Canonical equal-count event" in output
    assert "Stale equal-count event" not in output


def test_apply_change_set_rolls_back_claim_when_timeline_projection_fails(isolated_memory, monkeypatch):
    db_store.init_db()
    from vector_lake import tool_timeline

    def fail_projection(old_rows, proposed_claims):
        raise RuntimeError("timeline projection failed")

    monkeypatch.setattr(tool_timeline, "sync_timeline_events_for_claim_delta", fail_projection)
    claim = _claim("claim_rollback", "Source_Rollback", "Rollback event")

    try:
        governance_store.apply_change_sets_batch([{
            "affected_pages": ["Source_Rollback.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])
    except RuntimeError as exc:
        assert "timeline projection failed" in str(exc)
    else:
        raise AssertionError("projection failure must abort the canonical transaction")

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE claim_id = ?", (claim["claim_id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 0


def _insert_timeline_claim(conn, claim_id: str, text: str, payload: dict, updated_at: str) -> None:
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim_id, text, "active", json.dumps(payload), updated_at),
        )


def test_filtered_search_works_on_the_canonical_fallback(isolated_memory):
    """The fallback must not reference ``claims.entity_id``, which does not exist.

    ``timeline_events`` is left deliberately stale so the projection branch is
    skipped, which is the only state in which the canonical fallback runs.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Vendor_Fallback"],
        "source_ids": ["Source_Fallback"],
        "action": "Release",
        "sentiment": "positive",
    }
    _insert_timeline_claim(
        conn,
        "claim_fallback",
        "[2026-05-02] [Release] Vendor_Fallback shipped.",
        payload,
        "2026-07-14T00:00:00+00:00",
    )

    # Leave the projection non-empty but wrong so the drift note is exercised too.
    with db_store.transaction():
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale-row", "2000-01-01", "old", "neutral", "Stale", "", "", "", "2000-01-01"),
        )

    output = search_timeline_events(entity_name="Vendor_Fallback", limit=5)

    assert "no such column" not in output
    assert "Vendor_Fallback shipped." in output
    assert output.startswith("[DEGRADED]")

    sentiment_miss = search_timeline_events(sentiment="negative", limit=5)
    assert "Vendor_Fallback shipped." not in sentiment_miss
    assert "no such column" not in sentiment_miss


def test_event_date_comes_from_the_claim_text_prefix(isolated_memory):
    """Timeline claims carry their event date as a ``[YYYY-MM-DD]`` text prefix."""
    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_prefix",
        "[2026-03-01] [Observation] Event with a text date.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Prefix"],
            "source_ids": ["Source_Prefix"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    assert "Rebuilt 1 timeline_events" in rebuild_timeline_events_from_claims(dry_run=False)

    row = conn.execute("SELECT event_date FROM timeline_events").fetchone()

    assert row["event_date"] == "2026-03-01", row["event_date"]


def test_parity_cache_follows_claim_writes(isolated_memory):
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_cached",
        "[2026-04-04] [Release] Cached event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Cache"],
            "source_ids": ["Source_Cache"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    rebuild_timeline_events_from_claims(dry_run=False)
    assert tool_timeline.timeline_projection_parity()["missing"] == 0

    # A new canonical claim must invalidate the memoised proof.
    _insert_timeline_claim(
        conn,
        "claim_cached_2",
        "[2026-04-05] [Release] Second cached event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Cache"],
            "source_ids": ["Source_Cache"],
        },
        "2026-07-15T00:00:00+00:00",
    )

    assert tool_timeline.timeline_projection_parity()["missing"] == 1


def test_parity_cache_follows_out_of_band_projection_writes(isolated_memory):
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_oob",
        "[2026-04-06] [Release] Out of band event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Oob"],
            "source_ids": ["Source_Oob"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    rebuild_timeline_events_from_claims(dry_run=False)
    assert tool_timeline.timeline_projection_parity()["missing"] == 0

    with db_store.transaction():
        conn.execute("DELETE FROM timeline_events")
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("zzz-wrong", "2000-01-01", "old", "neutral", "Wrong", "", "", "", "2000-01-01"),
        )

    assert tool_timeline.timeline_projection_parity()["extra"] == 1
