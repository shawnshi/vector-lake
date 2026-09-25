"""P0: the outbox ledger stays useful while losing its payload weight, and disk is reclaimed.

Measured before this existed (2026-09-25): 41 787 outbox rows holding 79.6 MB of ``payload_text``,
and a 2 301 MB database file with 151 760 free pages (593 MB) that ``auto_vacuum=0`` would never
return.  The retention here is deliberately *payload-only*: three readers depend on the row itself,
and the most load-bearing of them is that ``enqueue_mutation`` revives a terminal row with a matching
idempotency key rather than inserting a second one -- so deleting old rows would turn a repeated
logical write into a duplicate mutation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vector_lake import db_store, outbox_retention, search_ledger


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _row(filename, status="completed", *, days=90, payload=None, key=None, mutation="update"):
    conn = db_store.get_connection()
    with db_store.transaction():
        cursor = conn.execute(
            "INSERT INTO mutation_outbox (filename, mutation_type, payload_text, status,"
            " attempt_count, created_at, available_at, completed_at, idempotency_key)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (
                filename, mutation, payload if payload is not None else f"content of {filename}",
                status, _iso(days), _iso(days), _iso(days), key,
            ),
        )
    return int(cursor.lastrowid)


@pytest.fixture
def populated(isolated_memory):
    db_store.init_db()
    ids = {
        "old_completed": _row("Concept_Old.md", "completed", days=90),
        "recent_completed": _row("Concept_Recent.md", "completed", days=1),
        "old_pending": _row("Concept_Pending.md", "pending", days=90),
        "old_superseded": _row("Concept_Old.md", "superseded", days=90),
    }
    return ids


def test_plan_counts_only_terminal_rows_outside_the_window(populated):
    plan = outbox_retention.plan_outbox_payload_retention(days=30)

    assert plan["rows_to_strip"] == 2, plan  # old_completed + old_superseded
    assert plan["bytes_to_strip"] > 0
    assert plan["rows_with_payload"] == 4


def test_prune_nulls_the_payload_and_keeps_every_row(populated):
    before = db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0]

    result = outbox_retention.prune_outbox_payloads(days=30)

    conn = db_store.get_connection()
    assert result["stripped"] == 2
    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == before, "no row may go"
    payloads = dict(conn.execute("SELECT id, payload_text FROM mutation_outbox"))
    assert payloads[populated["old_completed"]] is None
    assert payloads[populated["old_superseded"]] is None
    assert payloads[populated["recent_completed"]] is not None, "inside the window"
    assert payloads[populated["old_pending"]] is not None, "non-terminal rows are never touched"


def test_prune_is_idempotent(populated):
    outbox_retention.prune_outbox_payloads(days=30)

    assert outbox_retention.prune_outbox_payloads(days=30)["stripped"] == 0


def test_a_stripped_row_still_revives_by_idempotency_key(isolated_memory):
    """The invariant the payload-only design exists for: the dedup/revival record survives."""
    db_store.init_db()
    row_id = _row("Concept_Key.md", "completed", days=90, payload="old content", key="plan-42")
    outbox_retention.prune_outbox_payloads(days=30)

    revived = db_store.enqueue_mutation(
        "Concept_Key.md", "update", payload_text="new content", idempotency_key="plan-42"
    )

    assert revived == row_id, "a repeated write must revive the original row, not insert a second"
    row = db_store.get_connection().execute(
        "SELECT status, payload_text FROM mutation_outbox WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["payload_text"] == "new content"


def test_reclaim_refuses_without_permission(isolated_memory):
    db_store.init_db()

    result = db_store.reclaim_free_space(min_free_mb=0)

    assert result["vacuumed"] is False
    assert "not permitted" in result["reason"]
    assert db_store.free_space_report()["page_count"] > 0


def test_reclaim_reports_below_threshold(isolated_memory):
    db_store.init_db()

    result = db_store.reclaim_free_space(min_free_mb=10 ** 6, allow=True)

    assert result["vacuumed"] is False
    assert "below the" in result["reason"]


def test_reclaim_vacuums_when_permitted(isolated_memory):
    """A real VACUUM on the hermetic database: the path that runs in a maintenance window."""
    db_store.init_db()
    conn = db_store.get_connection()
    payload = "x" * 200_000
    for index in range(20):
        _row(f"Concept_Bulk{index}.md", "completed", days=0, payload=payload)
    with db_store.transaction():
        conn.execute("DELETE FROM mutation_outbox WHERE filename LIKE 'Concept_Bulk%'")
    before = db_store.free_space_report()
    assert before["free_pages"] > 0

    result = db_store.reclaim_free_space(min_free_mb=0, allow=True)

    assert result["vacuumed"] is True
    assert result["freed_bytes"] >= 0
    assert db_store.free_space_report()["free_pages"] <= before["free_pages"]


def test_ledger_records_the_retrieval_configuration(isolated_memory):
    """Without these two fields a past answer cannot be explained, only observed to differ."""
    search_ledger.record(
        "电子病历 六级", mode="page", top_k=5, returned=[], fusion="rrf", fts_backend="tantivy"
    )

    entry = search_ledger.entries()[-1]

    assert entry["fusion"] == "rrf"
    assert entry["fts_backend"] == "tantivy"


def test_ledger_stays_backward_compatible(isolated_memory):
    """Callers that pass no configuration keep producing the same shape as before."""
    search_ledger.record("信创 医院", mode="page", top_k=5, returned=[])

    entry = search_ledger.entries()[-1]

    assert "fusion" not in entry
    assert "fts_backend" not in entry


def test_default_backend_name_is_fts5(isolated_memory, monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_FTS", raising=False)

    from vector_lake import tool_search

    assert tool_search._fts_backend_name() == "fts5"
