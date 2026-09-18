"""``entities.ttl``/``decay_weight`` must hold what both write paths were told to write.

The batch path used ``INSERT OR REPLACE`` without naming those two columns, which resets any column
the statement omits -- so every batch write wiped them.  Measured before the fix: 7,919 of 7,924 rows
had a NULL ``ttl`` while the json carried one for 5,720 of them.  The decision was to keep the
columns (they are the only home ``decay_weight`` has) and make both paths write them, with the ttl
column backfilled from the json the indexer actually reads.
"""

import json

from vector_lake import db_store, governance_store


def _record(entity_id="e1", page_key="Concept_One", **extra):
    record = {
        "entity_id": entity_id,
        "canonical_name": page_key,
        "page_key": page_key,
        "type": "concept",
        "status": "Active",
        "aliases": [],
        "categories": ["Concept"],
    }
    record.update(extra)
    return record


def test_both_write_paths_write_the_same_ttl_and_decay_weight(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()

    governance_store.upsert_entity("e1", _record(ttl=120.0, decay_weight=0.5))
    governance_store._upsert_canonical_records(
        "entities", "entity_id", [_record("e2", "Concept_Two", ttl=240.0, decay_weight=0.25)]
    )

    rows = {
        row["entity_id"]: (row["ttl"], row["decay_weight"])
        for row in conn.execute("SELECT entity_id, ttl, decay_weight FROM entities")
    }

    assert rows["e1"] == (120.0, 0.5), rows
    assert rows["e2"] == (240.0, 0.25), (
        "the batch path did not write them, which is what wiped them before"
    )


def test_a_record_without_them_gets_the_same_default_in_both_paths(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()

    governance_store.upsert_entity("e1", _record())
    governance_store._upsert_canonical_records("entities", "entity_id", [_record("e2", "Concept_Two")])

    rows = {
        row["entity_id"]: (row["ttl"], row["decay_weight"])
        for row in conn.execute("SELECT entity_id, ttl, decay_weight FROM entities")
    }

    assert rows["e1"] == rows["e2"] == (0.0, 0.0), rows


def test_a_batch_write_no_longer_erases_them(isolated_memory):
    """The regression itself: write, then write again through the batch path."""
    db_store.init_db()
    conn = db_store.get_connection()
    governance_store.upsert_entity("e1", _record(ttl=90.0, decay_weight=0.75))

    governance_store._upsert_canonical_records(
        "entities", "entity_id", [_record(ttl=90.0, decay_weight=0.75)]
    )

    row = conn.execute("SELECT ttl, decay_weight FROM entities WHERE entity_id = 'e1'").fetchone()
    assert (row["ttl"], row["decay_weight"]) == (90.0, 0.75), row


def test_the_backfill_takes_ttl_from_the_json_and_is_idempotent(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, type, status, ttl, data_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "e1",
            "Concept_One",
            "concept",
            "Active",
            None,
            json.dumps({"page_key": "Concept_One", "ttl": 365.0}),
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-backfill-entities-ttl'")
    conn.commit()

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-backfill-entities-ttl" in applied
    row = conn.execute("SELECT ttl FROM entities WHERE entity_id = 'e1'").fetchone()
    assert row["ttl"] == 365.0, row
    # Idempotent: the WHERE matches nothing the second time, and the migration is recorded.
    assert db_store.apply_legacy_schema_prunes() == []


def test_a_non_numeric_ttl_is_absent_in_both_paths(isolated_memory):
    """A bad frontmatter value must not raise inside the change-set transaction.

    The batch path converts ``ttl`` while applying a change set, so an exception there rolls the
    whole batch back over one value; the rest of the tree already reads an unparseable ttl as absent.
    Numeric strings keep working, which is why this is a conversion rather than an isinstance check.
    """
    db_store.init_db()
    conn = db_store.get_connection()

    governance_store.upsert_entity("e1", _record(ttl="180", decay_weight=0.5))
    governance_store._upsert_canonical_records(
        "entities", "entity_id", [_record("e2", "Concept_Two", ttl="180d", decay_weight="0.25")]
    )

    rows = {
        row["entity_id"]: (row["ttl"], row["decay_weight"])
        for row in conn.execute("SELECT entity_id, ttl, decay_weight FROM entities")
    }

    assert rows["e1"] == (180.0, 0.5), rows          # a numeric string still parses
    assert rows["e2"] == (0.0, 0.25), rows            # "180d" is absent, not an exception
