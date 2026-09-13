"""Deterministic proof of the projection-v2 write amplification (EFF-01).

Root cause (measured against the live object store on 2026-09-13):

``projection_format_v2`` keyed every edge, claim node/edge and error-log entry by
its ``enumerate()`` position in the caller's list:

    _edge_key(edge, ordinal)        -> f"{pair}\\x1f{ordinal:08d}\\x1f{signature}"
    _claim_item_key(item, ordinal)  -> f"{identity}\\x1f{ordinal:08d}\\x1f{digest}"
    _error_key(item, ordinal)       -> f"{filename}\\x1f{ordinal:08d}\\x1f{digest}"

``pair``/``identity``/``signature``/``digest`` are content-derived; the ordinal
was the caller's list position, and ``_edge_components`` also stored it in the
payload as ``__ordinal__``.  So the same logical record hashed differently
whenever the caller's order changed.

Measured on the live store (60,000-object sample across generations):

* edges: 211,848 distinct ``(pair, signature)`` identities, 155,757 (73.5%)
  observed with more than one ordinal, up to 73 vintages each;
* claim/error: 38,409 distinct identities, 34,950 (91.0%) with more than one
  ordinal, up to 52 vintages each.

Each drift rewrote the leaf and its whole ancestor path, so a publish cost a full
~2,777-object tree rewrite instead of "affected HAMT paths plus a bounded
frontier".  The live object store held 7.64 GB against a 96.2 MiB reachable
closure (98.9% orphan).

Fix under test: identity is content-derived, and a per-identity occurrence counter
disambiguates only *identical* records, so nothing can be renumbered by adding or
removing an unrelated record.  Incidence lists are stored in canonical order
because they were previously appended in caller order.
"""

from __future__ import annotations

import random

from vector_lake import db_store, indexer
from vector_lake.projection_format_v2 import build_projection_roots
from vector_lake.wiki_utils import get_wiki_dir

NODE_COUNT = 300
DEGREE = 6
CLAIM_COUNT = 120


def _edges() -> list[dict]:
    """A fixed logical edge corpus; only the caller's ordering is varied."""
    keys = [f"Concept_Page{index:04d}" for index in range(NODE_COUNT)]
    edges = []
    for index, key in enumerate(keys):
        for step in range(1, DEGREE + 1):
            target = keys[(index * 7 + step * 13) % NODE_COUNT]
            if target == key:
                continue
            edges.append(
                {
                    "source": key,
                    "target": target,
                    "weight": round(0.5 + (index % 5) * 0.1, 3),
                }
            )
    return edges


def _claim_graph() -> dict:
    nodes = [
        {
            "id": f"claim_{index:04d}",
            "claim_id": f"claim_{index:04d}",
            "text": f"claim text {index}",
            "page_key": f"Concept_Page{index % NODE_COUNT:04d}",
        }
        for index in range(CLAIM_COUNT)
    ]
    edges = [
        {
            "source": f"claim_{index:04d}",
            "target": f"claim_{(index + 7) % CLAIM_COUNT:04d}",
            "relation": "supports",
        }
        for index in range(CLAIM_COUNT)
    ]
    return {"nodes": nodes, "edges": edges, "meta": {}, "schema_version": "1.0"}


def _index_data(edges: list[dict]) -> dict:
    keys = [f"Concept_Page{index:04d}" for index in range(NODE_COUNT)]
    nodes = {}
    for index, key in enumerate(keys):
        links = [
            keys[(index * 7 + step * 13) % NODE_COUNT]
            for step in range(1, DEGREE + 1)
        ]
        nodes[key] = {
            "id": key,
            "title": key,
            "summary": f"summary for {key}",
            "raw_text": f"body text for {key}",
            "type": "concept",
            "links": links,
        }
    return {
        "nodes": nodes,
        "aliases": {},
        "weighted_edges": [dict(edge) for edge in edges],
        "_projection_edge_candidates": [dict(edge) for edge in edges],
        "categories": [],
        "error_log": [],
    }


def _build(index_data: dict, claim_graph: dict | None = None):
    db_store.init_db()
    return build_projection_roots(
        get_wiki_dir(),
        index_data,
        claim_graph if claim_graph is not None else _claim_graph(),
        canonical_generation=indexer.canonical_runtime_generation_snapshot(),
        published_at_utc="2026-09-13T00:00:00+00:00",
    )


def _shuffled(items: list) -> list:
    copied = list(items)
    random.Random(20260913).shuffle(copied)
    assert [repr(item) for item in copied] != [repr(item) for item in items]
    return copied


def test_identical_projection_rebuild_reuses_every_object(isolated_memory):
    """A byte-identical rebuild must allocate nothing (guards the fix)."""
    edges = _edges()
    first = _build(_index_data(edges))
    assert first.object_new_count > 0

    second = _build(_index_data(edges))

    assert second.object_new_count == 0
    assert second.object_reused_count == (
        first.object_new_count + first.object_reused_count
    )


def test_edge_identity_is_position_independent(isolated_memory):
    """Permuting an unchanged edge set must not rewrite the projection.

    Before the fix this rewrote 35 of 132 objects (26.5%) on a 300-node corpus;
    the live ratio was near total because 73.5% of edge identities had drifted.
    """
    edges = _edges()
    first = _build(_index_data(edges))
    assert first.object_new_count > 0

    rebuilt = _build(_index_data(_shuffled(edges)))

    assert rebuilt.object_new_count == 0, (
        "permuting an unchanged edge set rewrote "
        f"{rebuilt.object_new_count} objects; edge identity is still positional"
    )
    assert rebuilt.index_root_sha256 == first.index_root_sha256


def test_claim_identity_is_position_independent(isolated_memory):
    """Permuting an unchanged claim graph must not rewrite the projection.

    91.0% of live claim/error identities had drifted ordinals, the worst family.
    """
    edges = _edges()
    claim_graph = _claim_graph()
    first = _build(_index_data(edges), claim_graph)
    assert first.object_new_count > 0

    permuted = {
        **claim_graph,
        "nodes": _shuffled(claim_graph["nodes"]),
        "edges": _shuffled(claim_graph["edges"]),
    }
    rebuilt = _build(_index_data(edges), permuted)

    assert rebuilt.object_new_count == 0, (
        "permuting an unchanged claim graph rewrote "
        f"{rebuilt.object_new_count} objects; claim identity is still positional"
    )
    assert rebuilt.claim_graph_root_sha256 == first.claim_graph_root_sha256


def test_edge_identity_never_encodes_list_position(isolated_memory):
    """The key set must be identical for any permutation of the same records."""
    from vector_lake.projection_format_v2 import (
        _edge_components,
        _edge_identity,
        _edge_key,
    )

    edges = _edges()[:40]
    baseline, baseline_incidence = _edge_components(edges)
    permuted, permuted_incidence = _edge_components(_shuffled(edges))

    assert set(permuted) == set(baseline)
    assert permuted == baseline
    # Incidence is stored in canonical order, not caller order.
    assert permuted_incidence == baseline_incidence
    for keys in baseline_incidence.values():
        assert keys == sorted(keys)

    # Only an identical duplicate is disambiguated, and only by occurrence count.
    duplicate, _ = _edge_components([edges[0], edges[0]])
    assert len(duplicate) == 2
    assert _edge_key(edges[0], 0) in duplicate
    assert _edge_key(edges[0], 1) in duplicate

    # Endpoint order is normalized in the pair field.  The signature still hashes
    # the raw record, so a caller that flips orientation produces a distinct
    # record; that is pre-existing behaviour and must stay stable per caller.
    mirrored = {"source": "Concept_A", "target": "Concept_B", "weight": 0.5}
    flipped = {"source": "Concept_B", "target": "Concept_A", "weight": 0.5}
    assert _edge_identity(mirrored)[0] == _edge_identity(flipped)[0]
    assert _edge_key(mirrored) == _edge_key(mirrored)


def test_projection_build_reports_its_object_cost(isolated_memory, caplog, monkeypatch):
    """A publish path must report new/reused objects.

    The 10-40x write amplification went unnoticed because a publish allocated its
    whole tree with no telemetry at all.  A non-zero ``new_objects`` on an
    unchanged corpus is now the regression signal.
    """
    import logging

    from vector_lake import indexer

    db_store.init_db()
    generation = indexer.canonical_runtime_generation_snapshot()
    edges = _edges()
    published = []

    monkeypatch.setattr(
        indexer,
        "publish_prepared_projection",
        lambda *args, **kwargs: published.append(args[1]) or {},
    )

    with caplog.at_level(logging.INFO, logger="vector-lake-indexer"):
        indexer._publish_projection_pair(
            str(indexer.get_index_path()),
            _index_data(edges),
            _claim_graph(),
            generation,
            generation,
        )

    builds = [
        record.getMessage()
        for record in caplog.records
        if "Projection build (" in record.getMessage()
    ]
    assert len(builds) == 1, builds
    assert "Projection build (staged-pair)" in builds[0]
    assert "new_objects=" in builds[0]
    assert "reused_objects=" in builds[0]
    assert "new_bytes=" in builds[0]
    assert len(published) == 1
    assert int(
        builds[0].split("new_objects=", 1)[1].split()[0]
    ) == published[0].object_new_count
