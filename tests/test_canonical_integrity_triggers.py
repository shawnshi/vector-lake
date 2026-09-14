"""The canonical tables must reject a row no reader can use.

The five canonical tables are wide, nullable and JSON-backed, and they carried no
NOT NULL or CHECK at all, so the database accepted rows that silently break every
reader downstream. SQLite cannot add a constraint to an existing column, and a
twelve-step rebuild per table over ~386k canonical rows is too expensive to run
on the live corpus, so the invariant is expressed as triggers instead.

These tests pin the behaviour, not the implementation detail: they derive the
required column sets from the same constants the runtime uses, so adding a table
or a column to the contract cannot leave the tests asserting a stale shape.
"""

import sqlite3

import pytest

from vector_lake import db_store


REQUIRED_COLUMNS = db_store._CANONICAL_REQUIRED_COLUMNS_V1

# A representative non-NULL value per column, so a test can build a row that is
# legal except for the one column it deliberately blanks.
SAMPLE_VALUES = {
    "claim_text": "text",
    "status": "Active",
    "data_json": "{}",
    "updated_at": "2026-01-01T00:00:00+00:00",
    "canonical_name": "Canonical",
    "memory_type": "fact",
}

# (table, primary key column) -- the key is never nullable, so it is not part of
# the required set but every insert must still supply it.
PRIMARY_KEYS = {
    "claims": "claim_id",
    "entities": "entity_id",
    "evidence": "evidence_id",
    "sources": "source_id",
    "operational_memory": "memory_id",
}


def _row(table: str, key: str, *, blank: str | None = None) -> tuple[str, list, list]:
    """Build a fully-populated insert for ``table``, optionally blanking ``blank``."""
    columns = [PRIMARY_KEYS[table], *dict(REQUIRED_COLUMNS)[table]]
    values = [
        key if column == PRIMARY_KEYS[table] else SAMPLE_VALUES[column]
        for column in columns
    ]
    if blank is not None:
        values[columns.index(blank)] = None
    placeholders = ", ".join("?" for _ in columns)
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    )
    return statement, values, columns


def test_init_db_creates_the_canonical_integrity_triggers():
    db_store.init_db()
    conn = db_store.get_connection()
    expected = {
        db_store._canonical_required_columns_trigger_name(table, operation)
        for table, _required in REQUIRED_COLUMNS
        for operation in ("insert", "update")
    }
    observed = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'trg_%_required_columns_v1_%'"
        )
    }
    assert observed == expected
    assert len(expected) == 2 * len(REQUIRED_COLUMNS)


def test_integrity_triggers_stay_out_of_the_generation_trigger_namespace():
    """``_runtime_generation_schema_issues`` asserts over ``trg_*_generation_v*_*``.

    A name that collided there would be reported as
    ``runtime_generation_schema_unexpected`` and block the runtime generation
    contract, so the namespace must stay disjoint.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    collisions = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name GLOB 'trg_*_generation_v*_*'"
        )
        if "required_columns" in str(row[0])
    ]
    assert collisions == []


def test_init_db_is_idempotent_for_the_integrity_triggers():
    db_store.init_db()
    db_store.init_db()
    conn = db_store.get_connection()
    count = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
        "AND name LIKE 'trg_%_required_columns_v1_%'"
    ).fetchone()[0]
    assert count == 2 * len(REQUIRED_COLUMNS)


@pytest.mark.parametrize("table", sorted(dict(REQUIRED_COLUMNS)))
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_required_columns_reject_null(table: str, operation: str):
    db_store.init_db()
    conn = db_store.get_connection()
    key = PRIMARY_KEYS[table]
    pk = f"{table}-null-probe"

    statement, values, columns = _row(table, pk)
    conn.execute(statement, values)

    for column in dict(REQUIRED_COLUMNS)[table]:
        with pytest.raises(sqlite3.IntegrityError, match="canonical_required_column_null"):
            if operation == "insert":
                bad_statement, bad_values, _ = _row(table, f"{pk}-{column}", blank=column)
                conn.execute(bad_statement, bad_values)
            else:
                conn.execute(
                    f"UPDATE {table} SET {column} = NULL WHERE {key} = ?",
                    (pk,),
                )


@pytest.mark.parametrize("table", sorted(dict(REQUIRED_COLUMNS)))
def test_fully_populated_rows_still_write(table: str):
    """The invariant must not obstruct the shapes the store actually writes."""
    db_store.init_db()
    conn = db_store.get_connection()
    key = PRIMARY_KEYS[table]
    pk = f"{table}-ok-probe"

    statement, values, columns = _row(table, pk)
    conn.execute(statement, values)
    # INSERT OR REPLACE is an insert as far as the trigger is concerned, and the
    # store's upsert helpers rely on it.
    replace_statement = statement.replace("INSERT INTO", "INSERT OR REPLACE INTO")
    conn.execute(replace_statement, values)
    conn.execute(replace_statement, values)
    assert (
        conn.execute(
            f"SELECT count(*) FROM {table} WHERE {key} = ?", (pk,)
        ).fetchone()[0]
        == 1
    )

    # A legitimate update of a non-key column must pass.
    if "status" in columns:
        conn.execute(f"UPDATE {table} SET status = 'Draft' WHERE {key} = ?", (pk,))
