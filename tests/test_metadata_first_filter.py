"""The filtered path must reject on metadata *before* it pays for the exact rerank -- invariantly.

The optimisation changes which eligible page fills a slot (the candidate pool becomes the binary
shortlist), so what has to hold is stated as invariants rather than as byte-equality with the old
ordering:

* every returned page satisfies the caller's selectors;
* the widening loop still widens when a filter rejects everything, and still stops when the source
  itself came back short;
* an absent shadow index falls back to the plain arm instead of returning nothing;
* the switch really disables the new order.
"""

from __future__ import annotations

import numpy as np
import pytest

from vector_lake import db_store, tool_search as ts, two_stage_index

DIM = 3072


def _seed(monkeypatch, count: int = 40) -> dict[str, np.ndarray]:
    """A closed index whose pages split across two domains, with the shadow built."""
    db_store.init_db()
    db_store.record_embedding_projection("test-embedding", DIM)
    rng = np.random.default_rng(5)
    base = rng.normal(size=DIM)
    base /= np.linalg.norm(base)
    scale = 0.2 / DIM**0.5
    vectors = {}
    for i in range(count):
        vec = (base + rng.normal(scale=scale, size=DIM))
        vectors[f"Doc_{i:03d}"] = vec / np.linalg.norm(vec)
    conn = db_store.get_connection()
    for key, vec in vectors.items():
        db_store.upsert_embedding(key, [float(x) for x in vec])
        domain = "Alpha" if int(key[-3:]) % 2 == 0 else "Beta"
        # Its own transaction: ``upsert_embedding`` opens one per call, and leaving an implicit
        # transaction open here makes the next BEGIN IMMEDIATE fail.
        with db_store.transaction():
            conn.execute(
                "INSERT OR REPLACE INTO page_index_nodes (node_key, node_id, title, type, status,"
                " domain, topic_cluster, node_json) VALUES (?, ?, ?, 'Concept', 'Active', ?, 'c', ?)",
                (key, key, key, domain, _node_json(key, domain)),
            )
    return vectors


def _node_json(key: str, domain: str) -> str:
    """A node payload as the projection stores it: ``nodes_by_key`` reads the JSON, not the columns."""
    import json

    return json.dumps({
        "_key": key,
        "title": key,
        "type": "Concept",
        "status": "Active",
        "domain": domain,
        "topic_cluster": "c",
    })


class _Catalog:
    """Just enough catalog for the filtered arm: node metadata from the projection."""

    def nodes_by_key(self, keys):
        from vector_lake import page_index_projection

        return page_index_projection.nodes_by_key(list(keys))

    def filter_fields(self, keys):
        from vector_lake import page_index_projection

        return page_index_projection.filter_fields(list(keys))


def test_filter_fields_projection_matches_full_nodes(monkeypatch):
    """The cheap lookup must carry exactly what the selectors would have read from the JSON.

    If a selector ever starts reading a fourth field, this comparison fails rather than the filter
    silently seeing ``None``.
    """
    vectors = _seed(monkeypatch)
    from vector_lake import page_index_projection

    keys = [*vectors, "Doc_missing"]
    cheap = page_index_projection.filter_fields(keys)
    full = page_index_projection.nodes_by_key(keys)
    assert set(cheap) == set(full), "missing keys must be absent from both lookups"
    for key, fields in cheap.items():
        assert set(fields) == set(page_index_projection.FILTER_FIELDS)
        for name in page_index_projection.FILTER_FIELDS:
            assert fields[name] == full[key].get(name), f"{key}.{name} differs from the node payload"


def test_arm_prefers_the_cheap_lookup_and_falls_back_for_expressions(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = [float(x) for x in vectors["Doc_000"]]

    class Recording:
        def __init__(self):
            self.calls: list[str] = []

        def nodes_by_key(self, keys):
            self.calls.append("nodes_by_key")
            return _Catalog().nodes_by_key(keys)

        def filter_fields(self, keys):
            self.calls.append("filter_fields")
            return _Catalog().filter_fields(keys)

    plain = Recording()
    ts._get_vector_search_results_filtered(query, 20, plain, "Alpha", None, False, None)
    assert plain.calls == ["filter_fields"], "a plain filter must not deserialise node payloads"

    expressed = Recording()
    results, error, _raw = ts._get_vector_search_results_filtered(
        query, 20, expressed, None, None, False, "title == 'Doc_000'"
    )
    assert error is None
    assert expressed.calls == ["nodes_by_key"], "a filter_expr can read any field, so it needs nodes"
    assert list(results) == ["Doc_000"]


def test_filtered_arm_returns_only_eligible_pages(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = [float(x) for x in vectors["Doc_000"]]

    results, error, examined = ts._get_vector_search_results_filtered(
        query, 20, _Catalog(), "Alpha", None, False, None
    )
    assert error is None and results
    assert examined > 0
    domains = {
        row["domain"]
        for row in conn.execute("SELECT domain FROM page_index_nodes WHERE node_key != ''")
    }
    assert domains == {"Alpha", "Beta"}
    for key in results:
        row = conn.execute("SELECT domain FROM page_index_nodes WHERE node_key = ?", (key,)).fetchone()
        assert row["domain"] == "Alpha", f"{key} does not satisfy the domain filter"
    assert len(results) <= 20


def test_filtered_arm_reports_the_raw_depth_not_the_survivor_count(monkeypatch):
    """Widening must not read a heavy filter as an exhausted corpus."""
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = [float(x) for x in vectors["Doc_000"]]

    results, _error, examined = ts._get_vector_search_results_filtered(
        query, 10, _Catalog(), "NoSuchDomain", None, False, None
    )
    assert results == {}
    assert examined >= 10, "the shortlist was full; only the filter rejected everything"


def test_filtered_recall_widens_to_the_cap_when_everything_is_rejected():
    calls: list[int] = []

    def fetch(limit):
        calls.append(limit)
        return {"k": 1.0}, None

    raw = {"value": 10**6}
    selected, error, note = ts._filtered_recall(
        fetch, _RejectingCatalog(), 5, "Alpha", None, False, None, raw_count=lambda: raw["value"]
    )
    assert error is None
    assert len(calls) > 1, "a filter that rejects everything must widen"
    assert selected == {}
    assert note and "cap" in note, "widening that reaches the cap must say so"


def test_filtered_recall_names_the_selectors_when_the_source_is_exhausted():
    """A short *raw* list with keys present means the filter decided, not an empty corpus."""

    def fetch(limit):
        return {"k": 1.0}, None

    selected, _error, note = ts._filtered_recall(
        fetch, _RejectingCatalog(), 5, "Alpha", None, False, None, raw_count=lambda: 5
    )
    assert selected == {}
    assert note and "selectors" in note


def test_filtered_recall_stops_when_the_raw_source_is_short():
    calls: list[int] = []

    def fetch(limit):
        calls.append(limit)
        return {"k": 1.0}, None

    selected, _error, _note = ts._filtered_recall(
        fetch, _RejectingCatalog(), 5, "Alpha", None, False, None, raw_count=lambda: 1
    )
    assert len(calls) == 1, "a short raw list means the source itself is exhausted"
    assert selected == {}


def test_absent_shadow_falls_back_to_the_plain_arm(monkeypatch):
    """No shadow index: the arm reports itself unavailable and the caller keeps working."""
    db_store.init_db()
    results, error, examined = ts._get_vector_search_results_filtered(
        [0.0] * DIM, 5, _Catalog(), "Alpha", None, False, None
    )
    assert results is None and examined == 0
    assert error is None, "unavailability is not an error the caller should surface"


def test_switch_off_skips_the_metadata_first_arm(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    monkeypatch.setenv("VECTOR_LAKE_METADATA_FIRST", "0")

    called = []
    monkeypatch.setattr(
        ts, "_get_vector_search_results_filtered",
        lambda *a, **k: called.append(a) or (None, None, 0),
    )
    real = ts._get_query_embedding
    ts._get_query_embedding = lambda q, _v=None: ([float(x) for x in vectors["Doc_000"]], None)
    try:
        ts._search_scored_pages("switch off", top_k=3, domain="Alpha")
    finally:
        ts._get_query_embedding = real
    assert called == [], "the switch must restore the legacy order without consulting the new arm"


class _RejectingCatalog:
    def nodes_by_key(self, keys):
        return {key: {"_key": key, "domain": "Other", "status": "Active"} for key in keys}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
