"""The outbox retention selector must not re-scan the outbox per candidate.

The pre-fix form used a correlated ``NOT EXISTS`` against
``mutation_outbox.superseded_by``, which has no index.  On the live 27,761-row
outbox the preview did not finish inside 30 minutes.  These tests pin the
replacement as both *equivalent* (against the original query kept here as the
reference implementation) and *bounded* (the query plan must not scan the table
once per candidate row).
"""

from __future__ import annotations

import random
import sqlite3
import time

import pytest

from vector_lake import governance_store

TERMINAL = governance_store._HISTORY_TERMINAL_OUTBOX_STATUSES
NON_TERMINAL = ("pending", "processing")


def _reference_query(terminal: str) -> str:
    """The exact pre-fix form, retained as the equivalence oracle."""
    return (
        "WITH terminal AS ("
        " SELECT id, completed_at AS retained_at"
        " FROM mutation_outbox "
        f" WHERE status IN ({terminal})"
        "), ranked AS ("
        " SELECT id, retained_at,"
        " ROW_NUMBER() OVER (ORDER BY retained_at DESC, id DESC) AS retain_rank"
        " FROM terminal"
        ") SELECT candidate.id FROM ranked AS candidate "
        "WHERE julianday(candidate.retained_at) IS NOT NULL "
        "AND julianday(candidate.retained_at) < julianday(?) "
        "AND candidate.retain_rank > ? "
        "AND NOT EXISTS ("
        " SELECT 1 FROM mutation_outbox AS active "
        " WHERE active.superseded_by = candidate.id "
        f" AND COALESCE(active.status, '') NOT IN ({terminal})"
        ") ORDER BY candidate.retained_at ASC, candidate.id ASC LIMIT ?"
    )


def _seed(conn: sqlite3.Connection, rows: list[tuple[int, str, str | None, int | None]]) -> None:
    conn.executemany(
        "INSERT INTO mutation_outbox(id, filename, status, completed_at, superseded_by) "
        "VALUES (?, 'page.md', ?, ?, ?)",
        rows,
    )
    conn.commit()


def _reference(conn, cutoff, keep_latest, batch_size) -> list[int]:
    terminal = ",".join("?" for _ in TERMINAL)
    rows = conn.execute(
        _reference_query(terminal),
        (*TERMINAL, cutoff, keep_latest, *TERMINAL, batch_size),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def test_selector_matches_the_pre_fix_form(governed_outbox_db):
    conn = governed_outbox_db
    cutoff = "2026-08-16T00:00:00+00:00"
    for keep_latest in (0, 1, 5, 40):
        for batch_size in (1, 7, 500):
            expected = _reference(conn, cutoff, keep_latest, batch_size)
            observed = governance_store._select_outbox_retention_candidates(
                conn, cutoff, batch_size, keep_latest
            )
            assert observed == expected, (keep_latest, batch_size)


def test_selector_matches_the_pre_fix_form_on_randomized_data(governed_outbox_db):
    """Fuzz the shapes the guard has to survive: live successors, orphaned
    successors, NULL completion times and non-terminal candidates."""
    conn = governed_outbox_db
    rng = random.Random(20260915)
    for round_index in range(6):
        conn.execute("DELETE FROM mutation_outbox")
        rows = []
        for row_id in range(1, 401):
            roll = rng.random()
            if roll < 0.72:
                status = rng.choice(TERMINAL)
                completed = (
                    None
                    if rng.random() < 0.06
                    else f"2026-0{rng.randint(5, 9)}-{rng.randint(10, 28):02d}T00:00:00+00:00"
                )
            else:
                status = rng.choice(NON_TERMINAL)
                completed = None
            successor = rng.choice([None, None, None, rng.randint(1, 400)])
            rows.append((row_id, status, completed, successor))
        _seed(conn, rows)

        for cutoff in ("2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"):
            for keep_latest in (0, 3, 25):
                expected = _reference(conn, cutoff, keep_latest, 500)
                observed = governance_store._select_outbox_retention_candidates(
                    conn, cutoff, 500, keep_latest
                )
                assert observed == expected, (round_index, cutoff, keep_latest)


def test_selector_never_selects_a_row_a_live_successor_needs(
    governed_outbox_db,
):
    conn = governed_outbox_db
    conn.execute("DELETE FROM mutation_outbox")
    _seed(
        conn,
        [
            (1, "completed", "2026-07-01T00:00:00+00:00", None),
            (2, "completed", "2026-07-02T00:00:00+00:00", None),
            # Row 3 is still pending and points at superseded rows 1 and 2.
            (3, "pending", None, 1),
            (4, "completed", "2026-07-03T00:00:00+00:00", 2),
        ],
    )

    selected = governance_store._select_outbox_retention_candidates(
        conn, "2026-08-01T00:00:00+00:00", 500, 0
    )

    assert 1 not in selected, "a pending row still references candidate 1"
    assert 2 in selected, "only a terminal successor references candidate 2"
    assert 3 not in selected, "non-terminal rows are not candidates"
    assert 4 in selected, (
        "a candidate that is itself terminal does not block the row it supersedes; "
        "only non-terminal successors do"
    )


def test_selector_plan_does_not_scan_the_outbox_per_candidate(governed_outbox_db):
    conn = governed_outbox_db
    terminal = ",".join("?" for _ in TERMINAL)

    # The pre-fix form is genuinely quadratic: the plan proves it.
    reference_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN " + _reference_query(terminal),
            (*TERMINAL, "2026-08-16T00:00:00+00:00", 0, *TERMINAL, 500),
        )
    ).upper()
    assert "CORRELATED" in reference_plan
    assert "SCAN ACTIVE" in reference_plan

    # The shipped selector must not contain a correlated scan of the outbox.
    shipped_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN " + governance_store._outbox_retention_select(),
            (*TERMINAL, *TERMINAL, "2026-08-16T00:00:00+00:00", 0, 500),
        )
    ).upper()
    assert "CORRELATED" not in shipped_plan
    assert "BLOCKED" in shipped_plan, shipped_plan


@pytest.fixture
def governed_outbox_db(isolated_memory):
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    yield conn
    db_store.close_all_connections()


def test_selector_stays_fast_at_scale(governed_outbox_db):
    """A 20k-row outbox must stay well inside an interactive preview budget."""
    conn = governed_outbox_db
    rows = []
    for row_id in range(1, 20_001):
        rows.append(
            (
                row_id,
                "completed",
                f"2026-07-{(row_id % 28) + 1:02d}T00:00:00+00:00",
                None,
            )
        )
    _seed(conn, rows)
    _seed(conn, [(30_001, "pending", None, 7)])

    started = time.perf_counter()
    selected = governance_store._select_outbox_retention_candidates(
        conn, "2026-08-16T00:00:00+00:00", 500, 1000
    )
    elapsed = time.perf_counter() - started

    assert len(selected) == 500
    assert elapsed < 5.0, f"selector took {elapsed:.1f}s on 20k rows"


@pytest.mark.parametrize(
    "label,rows,keep_latest,cutoff,expect_blocked,expect_selected",
    [
        (
            "orphan superseded_by points at no row",
            [
                (1, "completed", "2026-07-01T00:00:00+00:00", None),
                (2, "completed", "2026-07-02T00:00:00+00:00", 999),
                (3, "pending", None, None),
            ],
            0,
            "2026-08-01T00:00:00+00:00",
            [],
            [1, 2],
        ),
        (
            "two live successors share one target",
            [
                (1, "completed", "2026-07-01T00:00:00+00:00", None),
                (2, "completed", "2026-07-02T00:00:00+00:00", None),
                (3, "pending", None, 1),
                (4, "processing", None, 1),
            ],
            0,
            "2026-08-01T00:00:00+00:00",
            [1],
            [2],
        ),
        (
            "self-reference cannot block a terminal candidate",
            [
                (1, "completed", "2026-07-01T00:00:00+00:00", 1),
                (2, "completed", "2026-07-02T00:00:00+00:00", None),
            ],
            0,
            "2026-08-01T00:00:00+00:00",
            [],
            [1, 2],
        ),
        (
            "NULL status counts as live (COALESCE to empty string)",
            [
                (1, "completed", "2026-07-01T00:00:00+00:00", None),
                (2, None, None, 1),
            ],
            0,
            "2026-08-01T00:00:00+00:00",
            [1],
            [],
        ),
        (
            "empty-string status counts as live",
            [
                (1, "completed", "2026-07-01T00:00:00+00:00", None),
                (2, "", None, 1),
            ],
            0,
            "2026-08-01T00:00:00+00:00",
            [1],
            [],
        ),
        (
            "keep_latest protects the newest terminal rows regardless of age",
            [
                (1, "completed", "2026-07-01T00:00:00+00:00", None),
                (2, "completed", "2026-07-02T00:00:00+00:00", None),
                (3, "completed", "2026-07-03T00:00:00+00:00", None),
            ],
            2,
            "2026-08-01T00:00:00+00:00",
            [],
            [1],
        ),
    ],
)
def test_selector_equivalence_edge_cases(
    governed_outbox_db, label, rows, keep_latest, cutoff, expect_blocked, expect_selected
):
    """Deterministic coverage for the shapes the fuzz only reaches by luck.

    ``superseded_by`` has INTEGER affinity, so a text literal is coerced to an
    integer and cannot create a distinct storage class; there is no case to add
    for type affinity.
    """
    conn = governed_outbox_db
    conn.execute("DELETE FROM mutation_outbox")
    _seed(conn, rows)

    expected = _reference(conn, cutoff, keep_latest, 500)
    observed = governance_store._select_outbox_retention_candidates(
        conn, cutoff, 500, keep_latest
    )

    assert observed == expected, label
    assert observed == expect_selected, label
    blocked_ids = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT superseded_by FROM mutation_outbox "
            "WHERE superseded_by IS NOT NULL AND COALESCE(status, '') "
            "NOT IN ('completed', 'failed', 'superseded')"
        )
    }
    assert blocked_ids == set(expect_blocked), label
