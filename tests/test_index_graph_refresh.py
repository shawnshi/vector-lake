"""The graph refresh must not take the write lock just to learn there is nothing to do.

It used to open the write transaction first and parse the 17 MB ``index.json`` inside it, so every
scheduled occurrence acquired the database write lock -- the last needless acquisition under
``global_task_lock``.  Measured on 2026-09-19 with ``PRAGMA query_only=ON``: the old code raised
``attempt to write a readonly database`` immediately, while the read-only lint completed a 13 s scan
without attempting a write.  The probe added here is what that measurement says it should be: a
pure read that decides, with the writing path re-loading inside the transaction as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vector_lake import db_store, indexer
from vector_lake.wiki_utils import get_index_path


def _write_index(nodes: dict, dirty: bool) -> Path:
    path = get_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"nodes": nodes, "weighted_edges": [], "graph_state": {"dirty": dirty}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def isolated_index(isolated_memory):
    db_store.init_db()
    return isolated_memory


def test_a_clean_index_is_not_refreshed_and_takes_no_write_lock(isolated_index):
    """The regression guard: with the database read-only, a clean index must return, not raise."""
    _write_index({"Concept_A": {"title": "A", "links": []}}, dirty=False)
    conn = db_store.get_connection()
    conn.execute("PRAGMA query_only=ON")
    try:
        assert indexer.refresh_graph_topology_if_dirty() is False
    finally:
        conn.execute("PRAGMA query_only=OFF")


def test_a_dirty_index_is_still_refreshed(isolated_index):
    _write_index({"Concept_A": {"title": "A", "links": []}}, dirty=True)

    assert indexer.refresh_graph_topology_if_dirty() is True

    refreshed = json.loads(get_index_path().read_text(encoding="utf-8"))
    assert refreshed["graph_state"]["dirty"] is False, "the refresh did not clear the flag"


def test_a_missing_index_triggers_a_rebuild(isolated_index):
    """The cheap probe must not swallow the absent-file path."""
    path = get_index_path()
    if path.exists():
        path.unlink()

    assert indexer.refresh_graph_topology_if_dirty() is True
    assert get_index_path().exists()
