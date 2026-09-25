"""The gram-index rebuild policy: when the catch-up sweep may spend the rebuild.

Until 2026-09-25 the only caller was the scheduled occurrence at 10:00 and 23:00, and the only gate
was a write count, so a day of churn (9 212 live dirty documents) left every memory search on the
exact projected scan -- 1 240 ms instead of 782 ms -- for up to 13 hours.  The gate is now
denominated in the unit the decision is actually about: searches, because the index either saves
~0.46 s per search or it does not, and the ledger is the only record that a search happened.

These pin the arithmetic and the containment, not the measured constants: the break-even is computed
from `REBUILD_COST_SECONDS / SEARCH_SECONDS_SAVED` so changing either number cannot silently move
the bar by an order of magnitude.
"""
from __future__ import annotations

import math

import pytest

from vector_lake import db_store, memory_gram_index, periodic_catch_up


@pytest.fixture
def healthy_base(isolated_memory, monkeypatch):
    """A ready base with a controllable dirty count and search count."""
    db_store.init_db()
    monkeypatch.setattr(memory_gram_index, "gram_index_state", lambda: {"ready": True, "updated_at": "2026-09-25T00:00:00"})

    def _dirty(live=5):
        monkeypatch.setattr(memory_gram_index, "writes_since_rebuild", lambda conn=None: live)

    def _searched(count):
        monkeypatch.setattr(memory_gram_index, "_searches_since_last_rebuild", lambda: count)

    yield _dirty, _searched


def test_churn_alone_below_the_write_threshold_is_not_due(healthy_base):
    dirty, searched = healthy_base
    dirty(5)
    searched(10)

    assert memory_gram_index.rebuild_due_reason() is None


def test_the_break_even_is_computed_from_the_measured_cost(healthy_base):
    """`bar` searches repay a rebuild; `bar - 1` do not.  This is the whole policy."""
    dirty, searched = healthy_base
    bar = math.ceil(memory_gram_index.REBUILD_COST_SECONDS / memory_gram_index.SEARCH_SECONDS_SAVED)
    dirty(5)

    searched(bar)
    reason = memory_gram_index.rebuild_due_reason()
    assert reason is not None and "search(es)" in reason

    searched(bar - 1)
    assert memory_gram_index.rebuild_due_reason() is None


def test_an_unknowable_search_count_falls_back_to_the_write_gate(healthy_base):
    """A disabled or unreadable ledger must not read as "0 searches, nothing to repay"."""
    dirty, searched = healthy_base
    searched(None)
    dirty(5)
    assert memory_gram_index.rebuild_due_reason() is None

    dirty(memory_gram_index.REBUILD_AFTER_WRITES)
    reason = memory_gram_index.rebuild_due_reason()
    assert reason is not None and "threshold" in reason


def test_a_clean_index_is_never_due_however_many_searches_ran(healthy_base):
    """No churn means the base already serves every search; there is no debt to repay."""
    dirty, searched = healthy_base
    dirty(0)
    searched(10 ** 6)

    assert memory_gram_index.rebuild_due_reason() is None


@pytest.fixture
def raw_tree(isolated_memory):
    """Two un-ingested raw sources, so the sweep's scan half has something to find.

    Local rather than shared: the one in ``test_periodic_catch_up.py`` is module-scoped to that
    file, and a test that depends on another test module's fixture is a hidden coupling.
    """
    raw = isolated_memory / "raw"
    (raw / "news").mkdir(parents=True, exist_ok=True)
    (raw / "news" / "one.md").write_text("# one\n", encoding="utf-8")
    (raw / "news" / "two.md").write_text("# two\n", encoding="utf-8")
    db_store.init_db()
    return raw


def test_the_sweep_reports_the_gram_half(raw_tree, monkeypatch):
    monkeypatch.setattr(
        memory_gram_index, "maybe_rebuild_memory_gram_index", lambda *a, **k: "not due (test)"
    )

    summary = periodic_catch_up.catch_up_once()

    assert summary["gram"] == "not due (test)"
    assert "gram=not due (test)" in periodic_catch_up.describe(summary)
    assert summary["errors"] == []


def test_a_gram_fault_does_not_stop_the_other_halves(raw_tree, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("rebuild exploded")

    monkeypatch.setattr(memory_gram_index, "maybe_rebuild_memory_gram_index", boom)

    summary = periodic_catch_up.catch_up_once()

    assert any(error.startswith("gram:") for error in summary["errors"])
    # The scan half is what the sweep was built for; a gram fault must not cost it.
    assert "enqueued 2" in summary["enqueued"]
    assert "scan=" in periodic_catch_up.describe(summary)
