"""``page_graph_edges`` must keep mirroring the published edge projection.

The audit measured the live table at 1,293,200 rows against 29,837 published
edges -- 1,263,363 rows (97.7%) the published file does not contain.  They were
written before the degree cap existed, and the incremental writer only rewrites
the nodes an update touches, so nothing ever cleans them.

This pins the check that makes that visible, and the repair that clears it.  The
repair is deliberately the same call the full-rebuild path already makes, so it
introduces no new code path into the live database.

Note the check is *not* a degree bound: the published set itself reaches degree 17
on 10 live nodes, so ``MAX_EDGES_PER_NODE`` is not the bound the artifact
satisfies.  Mirroring the published set is.
"""
from vector_lake import db_store, tool_doctor

_STAMP = "2026-09-17T00:00:00+00:00"


def _seed_projection(conn, pairs) -> None:
    """Fill ``page_index_edges``, the read projection of the published edges."""
    conn.executemany(
        "INSERT INTO page_index_edges (sequence, source_key, target_key, weight) VALUES (?, ?, ?, ?)",
        [(index, source, target, 1.0) for index, (source, target) in enumerate(pairs)],
    )
    conn.commit()


def _seed_graph(conn, pairs) -> None:
    """Fill ``page_graph_edges``, the mirror under test."""
    conn.executemany(
        "INSERT INTO page_graph_edges (source_id, target_id, relation, weight, updated_at) "
        "VALUES (?, ?, 'related', 1.0, ?)",
        [(source, target, _STAMP) for source, target in pairs],
    )
    conn.commit()


def test_a_fresh_database_mirrors_the_published_set(isolated_memory):
    db_store.init_db()

    assert db_store.page_graph_edges_mirror_drift() == {
        "projection_rows": 0,
        "published_rows": 0,
        "difference": 0,
        "extra_examples": [],
        "extra": False,
        "missing": False,
        "missing_example": None,
    }


def test_rows_the_published_set_does_not_contain_are_extra(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_projection(conn, [("A", "B")])
    _seed_graph(conn, [("A", "B"), ("A", "Stale")])

    drift = db_store.page_graph_edges_mirror_drift()

    assert drift["extra"] is True
    assert drift["missing"] is False
    assert drift["difference"] == 1
    assert drift["extra_examples"] == [("A", "Stale")]


def test_a_published_pair_missing_from_the_projection_is_reported(isolated_memory):
    """The other direction: the projection fell behind what was published."""
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_projection(conn, [("A", "B"), ("C", "D")])
    _seed_graph(conn, [("A", "B")])

    drift = db_store.page_graph_edges_mirror_drift()

    assert drift["extra"] is False
    assert drift["missing"] is True
    assert drift["missing_example"] == ("C", "D")
    assert drift["difference"] == -1


def test_reprojecting_the_published_set_clears_the_drift(isolated_memory):
    """What the one-time repair on the live database does, and what a full rebuild does."""
    db_store.init_db()
    conn = db_store.get_connection()
    published = [
        {"source": "A", "target": "B", "weight": 2.0},
        {"source": "C", "target": "D", "weight": 1.0},
    ]
    _seed_projection(conn, [("A", "B"), ("C", "D")])
    _seed_graph(conn, [("A", "Stale"), ("A", "B"), ("C", "D"), ("X", "Y")])
    assert db_store.page_graph_edges_mirror_drift()["extra"] is True

    db_store.replace_page_graph_edges(published)

    assert db_store.page_graph_edges_mirror_drift()["extra"] is False
    assert conn.execute("SELECT COUNT(*) FROM page_graph_edges").fetchone()[0] == 2


def test_doctor_reports_a_clean_projection(isolated_memory):
    db_store.init_db()

    report = tool_doctor.doctor_vector_lake()

    assert "[OK] Page Edge Projection: mirrors the published 0 edge(s)" in report
    assert "page_edge_projection_drift" not in report


def test_doctor_flags_drift_and_names_an_example(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_projection(conn, [("A", "B")])
    _seed_graph(conn, [("A", "B"), ("A", "Stale")])

    report = tool_doctor.doctor_vector_lake()

    assert "[FAIL] Page Edge Projection:" in report
    assert "A->Stale" in report
    assert "page_edge_projection_drift" in report


def test_doctor_names_a_missing_pair_too(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _seed_projection(conn, [("A", "B"), ("C", "D")])
    _seed_graph(conn, [("A", "B")])

    report = tool_doctor.doctor_vector_lake()

    assert "[FAIL] Page Edge Projection:" in report
    assert "missing from the projection, e.g. C" in report
