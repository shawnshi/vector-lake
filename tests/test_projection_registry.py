"""P2: derived projections report health as a fact, and reconcile one at a time.

Pilots are the two the 2026-09-25 session had to repair by hand: the operational-memory gram index
(in the exact projected scan for up to 13 hours) and the vector projection (501 pages short after a
mass edit).  The registry adds no new mechanism -- it gives the *existing* health checks and repair
entry points one shape, so "unhealthy" is a queryable state instead of a sentence in a log.
"""
from __future__ import annotations

import pytest

from vector_lake import db_store, periodic_catch_up, projection_registry


def test_every_projection_declares_its_contract():
    pilots = projection_registry.registry()

    assert {p.name for p in pilots} == {"memory_gram", "vectors"}
    for projection in pilots:
        assert projection.authority, f"{projection.name} must name what it derives from"
        assert projection.cost, f"{projection.name} must state what a repair costs"
        assert projection.degrades, f"{projection.name} must state what degrades"


def test_gram_health_reports_the_degradation_with_counts(monkeypatch):
    from vector_lake import memory_gram_index

    monkeypatch.setattr(memory_gram_index, "gram_index_usable", lambda: False)
    monkeypatch.setattr(memory_gram_index, "dirty_breakdown", lambda conn=None: (20241, 9212, 11029))

    state, detail = projection_registry._gram_health()

    assert state == projection_registry.DEGRADED
    assert "9212" in detail and "exact scan" in detail


def test_a_failing_health_probe_is_reported_as_degraded_not_raised(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (projection_registry.Projection("x", "auth", "cost", boom, lambda: "n/a", "something"),),
    )

    report = projection_registry.status()

    assert report["x"]["state"] == projection_registry.DEGRADED
    assert "probe exploded" in report["x"]["detail"]


@pytest.fixture
def empty_lake(isolated_memory):
    db_store.init_db()
    return db_store.get_connection()


def _add_node(conn, key):
    with db_store.transaction():
        conn.execute(
            "INSERT INTO page_index_nodes (node_key, node_id, title, type, status, domain,"
            " topic_cluster, node_json) VALUES (?, ?, ?, 'concept', 'Active', 'General', 'x', '{}')",
            (key, key.lower(), key),
        )


def _add_vector(conn, key, stamp=True):
    import struct

    # ``vec_embeddings`` is a sqlite-vec ``vec0`` table fixed at 3072 dimensions, so the payload has
    # to be that wide even for a test fixture.
    blob = struct.pack("3072f", *([0.0] * 3072))
    with db_store.transaction():
        conn.execute(
            "INSERT INTO vec_embeddings (page_key, embedding) VALUES (?, ?)",
            (key, blob),
        )
        if stamp:
            conn.execute(
                "INSERT INTO vec_embedding_inputs (node_key, content_digest, updated_at)"
                " VALUES (?, 'digest', '2026-09-25T00:00:00+00:00')",
                (key,),
            )


def test_vector_health_counts_missing(empty_lake):
    _add_node(empty_lake, "Concept_One")
    _add_node(empty_lake, "Concept_Two")
    _add_vector(empty_lake, "Concept_One")

    state, detail = projection_registry._vector_health()

    assert state == projection_registry.DEGRADED
    assert "1/2 vectors" in detail and "1 missing" in detail


def test_vector_health_counts_unstamped(empty_lake):
    _add_node(empty_lake, "Concept_One")
    _add_vector(empty_lake, "Concept_One", stamp=False)

    state, detail = projection_registry._vector_health()

    assert state == projection_registry.DEGRADED
    assert "1 unstamped" in detail and "0 missing" in detail


def test_vector_health_is_green_when_complete(empty_lake):
    _add_node(empty_lake, "Concept_One")
    _add_vector(empty_lake, "Concept_One")

    assert projection_registry._vector_health() == (
        projection_registry.HEALTHY,
        "1/1 vectors, 0 missing, 0 unstamped",
    )


def test_reconcile_repairs_only_the_unhealthy_and_contains_failures(monkeypatch):
    called: list[str] = []

    def healthy():
        return projection_registry.HEALTHY, "fine"

    def sick():
        return projection_registry.DEGRADED, "behind"

    def good_repair():
        called.append("good")
        return "repaired"

    def bad_repair():
        called.append("bad")
        raise RuntimeError("repair exploded")

    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection("ok", "a", "c", healthy, lambda: called.append("never"), "none"),
            projection_registry.Projection("good", "a", "c", sick, good_repair, "something"),
            projection_registry.Projection("bad", "a", "c", sick, bad_repair, "something"),
        ),
    )

    results = projection_registry.reconcile()

    assert results["ok"]["action"] == "none", "a healthy projection is not repaired"
    assert "never" not in called
    assert results["good"]["action"] == "repaired"
    assert results["bad"]["action"] == "failed", "a repair fault is contained, not raised"
    assert "repair exploded" in results["bad"]["error"]


def test_status_line_is_compact_and_marks_degradation(monkeypatch):
    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection(
                "ok", "a", "c", lambda: (projection_registry.HEALTHY, "fine"), lambda: "", "none"
            ),
            projection_registry.Projection(
                "sick", "a", "c", lambda: (projection_registry.DEGRADED, "12 behind"), lambda: "", "x"
            ),
        ),
    )

    line = projection_registry.status_line()

    assert "ok=healthy" in line
    assert "sick=degraded(12 behind)" in line


@pytest.fixture
def raw_tree(isolated_memory):
    """One un-ingested source, so a sweep has something to do while reporting projections."""
    raw = isolated_memory / "raw"
    (raw / "news").mkdir(parents=True, exist_ok=True)
    (raw / "news" / "one.md").write_text("# one\n", encoding="utf-8")
    db_store.init_db()
    return raw


def test_the_sweep_reports_projection_health(raw_tree, monkeypatch):
    monkeypatch.setattr(
        projection_registry,
        "PILOTS",
        (
            projection_registry.Projection(
                "pilot", "authority", "cheap",
                lambda: (projection_registry.DEGRADED, "7 behind"), lambda: "", "search",
            ),
        ),
    )

    summary = periodic_catch_up.catch_up_once()
    line = periodic_catch_up.describe(summary)

    assert "projections=[" in line
    assert "pilot=degraded(7 behind)" in line
    assert summary["errors"] == []
