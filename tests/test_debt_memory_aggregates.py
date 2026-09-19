"""The debt metrics' memory counters must equal the decoded oracle.

``compute_debt_metrics`` used to answer four counters and one histogram by decoding all
147 231 ``operational_memory`` payloads (5.2 s of the 12.7 s the function took).  It now
reads the ``operational_memory_index`` projection columns that triggers keep in step with
``data_json``.  The projection is derived data, so the assertion has to be differential:
the aggregate equals what a full decode of the same rows produces, on a corpus that
contains every validity state and every memory type, including an empty state.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import db_store, governance_metrics

TYPES = ["fact", "decision", "preference", "task_state"]
STATES = ["active", "provisional", "unsupported", "expired", "superseded", "archived"]

ROWS = [
    (f"mem_{index}", TYPES[index % len(TYPES)], STATES[index % len(STATES)])
    for index in range(len(TYPES) * len(STATES))
]


def _put(conn, memory_id: str, memory_type: str, validity_state: str) -> None:
    payload = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": memory_id,
        "text": f"{memory_id} payload",
        "source_page": f"Concept_{memory_id}.md",
        "validity_state": validity_state,
        "memory_score": 0.6,
        "updated_at": "2026-07-14T00:00:00+00:00",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO operational_memory "
            "(memory_id, memory_type, score, status, ttl, data_json, updated_at) VALUES (?,?,?,?,?,?,?)",
            (memory_id, memory_type, 0.6, "Active", 365.0,
             json.dumps(payload, ensure_ascii=False), payload["updated_at"]),
        )


def _oracle(conn) -> tuple[int, dict, dict]:
    """What the pre-change implementation derived, straight from ``data_json``."""
    states: dict[str, int] = {}
    types: dict[str, int] = {}
    total = 0
    for row in conn.execute("SELECT data_json FROM operational_memory"):
        data = json.loads(row[0])
        total += 1
        states[data.get("validity_state", "active")] = states.get(data.get("validity_state", "active"), 0) + 1
        types[data.get("memory_type", "fact")] = types.get(data.get("memory_type", "fact"), 0) + 1
    return total, states, types


@pytest.fixture
def populated(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    for memory_id, memory_type, validity_state in ROWS:
        _put(conn, memory_id, memory_type, validity_state)
    return conn


def test_aggregates_match_a_full_decode(populated):
    conn = populated
    total, states, types = governance_metrics._memory_projection_aggregates()
    assert (total, states, types) == _oracle(conn)
    assert total == len(ROWS)
    assert set(states) == set(STATES)
    assert types == {memory_type: len(STATES) for memory_type in TYPES}


def test_debt_memory_counters_match_the_decoded_oracle(populated):
    conn = populated
    _, states, _ = _oracle(conn)
    metrics = governance_metrics.compute_debt_metrics(skip_heavy=True)

    assert metrics["operational_memory_count"] == len(ROWS)
    assert metrics["superseded_memory_count"] == states.get("superseded", 0)
    assert metrics["conflicted_memory_count"] == states.get("conflicted", 0)


def test_aggregates_are_empty_when_the_projection_is_empty(isolated_memory):
    db_store.init_db()
    assert governance_metrics._memory_projection_aggregates() == (0, {}, {})


def test_supplied_merge_candidates_are_used_instead_of_recomputing(populated, monkeypatch):
    """``debt_vector_lake`` prints the list, so it must not be derived a second time."""
    calls = []

    def _explode(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("find_merge_candidates must not run when a list is supplied")

    monkeypatch.setattr(governance_metrics, "find_merge_candidates", _explode)

    supplied = [{"pair_key": "a|b", "score": 1}]
    metrics = governance_metrics.compute_debt_metrics(merge_candidates=supplied)

    assert calls == []
    assert metrics["merge_candidate_count"] == 1


def test_skip_heavy_still_suppresses_the_candidate_pass(populated, monkeypatch):
    monkeypatch.setattr(
        governance_metrics, "find_merge_candidates",
        lambda *args, **kwargs: pytest.fail("skip_heavy must not compute candidates"),
    )
    assert governance_metrics.compute_debt_metrics(skip_heavy=True)["merge_candidate_count"] == 0
