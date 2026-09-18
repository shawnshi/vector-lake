"""The search edge projection must hold the published edge set.

``page_index_edges`` is the read projection of ``index.json``'s ``weighted_edges`` -- the set
``page_index_projection.adjacency`` walks for the personalised PageRank step.  A partial update
rewrites only the nodes it touches, so the projection can fall behind the file, and the file can
gain pairs the projection never saw.

This check used to compare the projection against ``page_graph_edges``: a *second* SQLite table
holding the same pairs, whose only reader was that check.  Two projections of one source being
compared to each other is not the contract; comparing the projection with the source is.  The table
and its writers were removed on 2026-09-18, and these tests pin the comparison that replaced it --
including that it is exact in both directions, because the legacy machinery it replaced existed for a
1.29M-row state the file never contained and that no longer occurs.

Not a degree bound: the published set itself reaches degree 17 on live nodes, so
``MAX_EDGES_PER_NODE`` is not the bound the artifact satisfies.  Holding the published set is.
"""
import json

from vector_lake import db_store, tool_doctor
from vector_lake.wiki_utils import get_index_path


def _publish(pairs, *, weighted=True) -> None:
    """Write ``weighted_edges`` into the published index, creating a minimal file if needed."""
    path = get_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data["weighted_edges"] = (
        [{"source": s, "target": t, "weight": 1.0} for s, t in pairs] if weighted else []
    )
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _seed_projection(conn, pairs) -> None:
    """Fill ``page_index_edges``, the read projection of the published edges."""
    conn.executemany(
        "INSERT INTO page_index_edges (sequence, source_key, target_key, weight) VALUES (?, ?, ?, ?)",
        [(index, source, target, 1.0) for index, (source, target) in enumerate(pairs)],
    )
    conn.commit()


def test_a_database_with_neither_side_reports_no_drift(isolated_memory):
    db_store.init_db()

    assert db_store.published_edge_projection_drift() == {
        "projection_rows": 0,
        "published_rows": 0,
        "difference": 0,
        "extra_examples": [],
        "extra": False,
        "missing": False,
        "missing_example": None,
        "duplicate_pairs": False,
        "published_read_error": "",
    }


def test_a_projected_pair_the_file_does_not_contain_is_extra(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B")])
    _seed_projection(conn, [("A", "B"), ("A", "Stale")])

    drift = db_store.published_edge_projection_drift()

    assert drift["extra"] is True
    assert drift["missing"] is False
    assert drift["difference"] == 1
    assert drift["extra_examples"] == [("A", "Stale")]


def test_a_published_pair_missing_from_the_projection_is_reported(isolated_memory):
    """The other direction: the projection fell behind what the file publishes."""
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B"), ("C", "D")])
    _seed_projection(conn, [("A", "B")])

    drift = db_store.published_edge_projection_drift()

    assert drift["extra"] is False
    assert drift["missing"] is True
    assert drift["missing_example"] == ("C", "D")
    assert drift["difference"] == -1


def test_an_empty_file_with_a_populated_projection_is_drift(isolated_memory):
    """The projection must not keep answering from a file that no longer publishes edges."""
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([], weighted=False)
    _seed_projection(conn, [("A", "B")])

    drift = db_store.published_edge_projection_drift()

    assert drift["published_rows"] == 0
    assert drift["projection_rows"] == 1
    assert drift["extra"] is True


def test_reprojecting_the_published_set_clears_the_drift(isolated_memory):
    """What a rebuild does: the projection is written from the file."""
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B"), ("C", "D")])
    _seed_projection(conn, [("A", "B"), ("C", "D"), ("X", "Y")])
    assert db_store.published_edge_projection_drift()["extra"] is True

    from vector_lake.page_index_projection import refresh_page_index_projection

    index_data = json.loads(get_index_path().read_text(encoding="utf-8"))
    with db_store.transaction():
        refresh_page_index_projection(index_data)

    assert db_store.published_edge_projection_drift()["extra"] is False
    assert conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0] == 2


def test_doctor_reports_a_clean_projection(isolated_memory):
    db_store.init_db()
    _publish([])

    report = tool_doctor.doctor_vector_lake()

    assert "[OK] Page Edge Projection: mirrors the published 0 edge(s)" in report
    assert "page_edge_projection_drift" not in report


def test_doctor_flags_a_projected_pair_the_file_lacks(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B")])
    _seed_projection(conn, [("A", "B"), ("A", "Stale")])

    report = tool_doctor.doctor_vector_lake()

    assert "[FAIL] Page Edge Projection:" in report
    assert "A->Stale" in report
    assert "page_edge_projection_drift" in report


def test_doctor_names_a_missing_pair_too(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B"), ("C", "D")])
    _seed_projection(conn, [("A", "B")])

    report = tool_doctor.doctor_vector_lake()

    assert "[FAIL] Page Edge Projection:" in report
    assert "missing from the projection, e.g. C" in report


def test_duplicate_pairs_in_the_projection_are_reported(isolated_memory):
    """The comparison is over distinct pairs, so multiplicity needs its own flag.

    The projection is re-inserted from the file, which holds one row per unordered pair, so a
    repeated row means something wrote the table outside that path -- and a pair-set comparison
    cannot see it.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B")])
    _seed_projection(conn, [("A", "B"), ("A", "B")])

    drift = db_store.published_edge_projection_drift()

    assert drift["extra"] is False and drift["missing"] is False
    assert drift["duplicate_pairs"] is True
    report = tool_doctor.doctor_vector_lake()
    assert "[FAIL] Page Edge Projection:" in report
    assert "duplicate pairs in the projection" in report


def test_an_unreadable_index_is_named_rather_than_reported_as_drift(isolated_memory):
    """``index.json`` missing the edge list is a different finding from the projection lying."""
    db_store.init_db()
    conn = db_store.get_connection()
    path = get_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    _seed_projection(conn, [("A", "B")])

    drift = db_store.published_edge_projection_drift()

    assert drift["published_read_error"], "an unreadable file must be named, not treated as empty"
    report = tool_doctor.doctor_vector_lake()
    assert "index.json unreadable" in report


def test_the_difference_is_exact_not_a_probe(isolated_memory):
    """More violations than any probe limit must still be counted.

    The check this replaced used LIMIT probes to answer "is there a violation", which was right at
    1.29M rows and is unnecessary at tens of thousands; the count is exact now, and this pins it so a
    future probe-based implementation cannot pass by reporting a boolean.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    _publish([("A", "B")])
    _seed_projection(conn, [("A", "B"), ("C", "D"), ("E", "F"), ("G", "H"), ("I", "J")])

    drift = db_store.published_edge_projection_drift()

    assert drift["difference"] == 4
    assert len(drift["extra_examples"]) == 3, "the example list stays bounded"
