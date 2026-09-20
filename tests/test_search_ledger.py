"""The retrieval ledger: what it records, what it refuses to record, and its ceiling.

It exists because the A1/A2 ranking work needed a baseline that this lake never kept (no
``log.md``, no query table).  Two properties carry that purpose, and both are asserted here: the
query is identified by digest rather than stored, and the file cannot grow without limit.  A third
is the reason it is safe to put on a read path at all: a write failure must not fail the search.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import search_ledger


def test_the_query_is_identified_not_stored(isolated_memory):
    """The line answers "same query?" without retaining caller text."""
    query = "梅奥诊所 的 数据平台"

    search_ledger.record(
        query,
        mode="page",
        top_k=5,
        returned=[{"key": "Concept_A", "origin": "fts", "score": 0.9}],
        notes=["fell back"],
        elapsed_ms=12.34,
    )

    raw = search_ledger.ledger_path().read_text(encoding="utf-8")
    assert "梅奥" not in raw
    entry = json.loads(raw.strip())
    assert entry["q_hash"] == search_ledger.query_digest(query)
    # The length is what a digest cannot tell a reader looking at the file by eye.
    assert entry["q_chars"] == len(query)
    assert entry["returned"] == [{"key": "Concept_A", "origin": "fts", "score": 0.9}]
    assert entry["top_k"] == 5 and entry["notes"] == ["fell back"] and entry["ms"] == 12.3


def test_the_digest_is_stable_and_distinguishes_queries(isolated_memory):
    assert search_ledger.query_digest("a") == search_ledger.query_digest("a")
    assert search_ledger.query_digest("a") != search_ledger.query_digest("b")
    assert len(search_ledger.query_digest("anything")) == 16


def test_the_switch_turns_it_off_entirely(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")

    search_ledger.record("q", mode="page", top_k=1, returned=[])

    assert search_ledger.enabled() is False
    assert search_ledger.entries() == []
    assert search_ledger.summary()["entries"] == 0


def test_summary_counts_entries_and_distinct_queries(isolated_memory):
    for query in ("one", "one", "two"):
        search_ledger.record(query, mode="page", top_k=1, returned=[])

    summary = search_ledger.summary()

    assert summary["entries"] == 3
    assert summary["distinct_queries"] == 2


def test_it_rotates_one_generation_at_the_ceiling(isolated_memory, monkeypatch):
    """Bounded, not merely small: past the ceiling the old file is moved aside once."""
    monkeypatch.setattr(search_ledger, "MAX_BYTES", 400)
    for index in range(40):
        search_ledger.record(f"query {index}", mode="page", top_k=1, returned=[])

    path = search_ledger.ledger_path()
    rotated = path.with_name(path.name + ".1")

    assert rotated.exists(), "the ceiling was passed without rotating"
    assert path.stat().st_size < 400 * 2, "both generations together exceeded the 2x ceiling"


def test_a_write_failure_never_reaches_the_caller(isolated_memory, monkeypatch):
    """Fail-open: a measurement may not become a reason a search fails."""

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)

    search_ledger.record("q", mode="page", top_k=1, returned=[])  # must not raise


def test_entries_reads_back_what_was_written_and_skips_torn_lines(isolated_memory):
    search_ledger.record("first", mode="page", top_k=1, returned=[])
    with search_ledger.ledger_path().open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    search_ledger.record("second", mode="page", top_k=1, returned=[])

    rows = search_ledger.entries()

    assert [row["q_hash"] for row in rows] == [
        search_ledger.query_digest("first"),
        search_ledger.query_digest("second"),
    ]
    assert len(search_ledger.entries(limit=1)) == 1


@pytest.mark.parametrize("value", ["1", "", "true", "yes"])
def test_anything_but_zero_leaves_it_on(isolated_memory, monkeypatch, value):
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", value)
    assert search_ledger.enabled() is True
