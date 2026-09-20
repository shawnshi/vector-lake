"""Reads must not compete for the write lock.

Regression cover for the MCP 60 s timeouts: a search opened with the ``init_db``
DDL transaction, and a stale projection was rebuilt by whoever read next -- so
every query queued behind the writer, and the loser returned no answer at all.
The contract now is:

* a reader never rebuilds (``watchdog_app.heal_page_index_projection`` is the
  single writer for that repair);
* a stale projection is served from ``index.json``, which is the sovereign
  artifact and more current than the projection anyway.
"""

import json

import pytest

from vector_lake import db_store, page_index_projection
from vector_lake.tool_search import _search_scored_pages, search_vector_lake
from vector_lake.wiki_utils import get_index_path


def _nodes(titles):
    return {
        key: {
            "title": title,
            "type": "Concept",
            "status": "Active",
            "domain": "AI_Engineering",
            "summary": f"{title} 梅奥 诊所 人工智能 实践",
        }
        for key, title in titles.items()
    }


def _write_index(memory_dir, nodes, edges):
    path = get_index_path()
    path.write_text(
        json.dumps({"nodes": nodes, "weighted_edges": edges}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def searchable_memory(isolated_memory):
    """An index.json, matching FTS rows, and a current SQLite projection."""
    db_store.init_db()
    nodes = _nodes({"Concept_A": "Alpha", "Concept_B": "Beta", "Concept_C": "Gamma"})
    edges = [
        {"source": "Concept_A", "target": "Concept_B", "weight": 3.0},
        {"source": "Concept_B", "target": "Concept_C", "weight": 2.0},
    ]
    _write_index(isolated_memory, nodes, edges)
    conn = db_store.get_connection()
    with db_store.transaction():
        for key, node in nodes.items():
            conn.execute(
                "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
                (key, node["title"], node["summary"], node["summary"]),
            )
    page_index_projection.refresh_page_index_projection(
        json.loads(get_index_path().read_text(encoding="utf-8"))
    )
    assert page_index_projection.projection_is_current() is True
    return isolated_memory


def test_lock_budget_stays_inside_the_client_timeout():
    """The invariant behind the whole fix: server worst case < client patience."""
    assert db_store.BEGIN_LOCK_BUDGET_SECONDS < db_store.MCP_CALL_TIMEOUT_SECONDS


def test_current_projection_needs_no_write_lock(isolated_memory, monkeypatch):
    """An already-current projection must never reach the DDL transaction."""
    monkeypatch.setattr(page_index_projection, "projection_is_current", lambda: True)

    def _explode(*_args, **_kwargs):
        raise AssertionError("init_db must not run when the projection is current")

    monkeypatch.setattr(page_index_projection, "init_db", _explode)
    assert page_index_projection.ensure_page_index_projection() is True


def test_projection_state_tolerates_a_cold_database(isolated_memory):
    """A database without the projection tables reads as 'not current', not as an error."""
    db_store.close_connection()
    assert page_index_projection.projection_state() is None
    assert page_index_projection.projection_is_current() is False


def test_schema_probe_accepts_an_initialised_database(isolated_memory):
    """The read-only probe must agree with the DDL it is allowed to skip."""
    db_store.init_db()
    assert db_store._schema_is_complete(db_store.get_db_path()) is True
    assert db_store._om_index_format_is_stale(db_store.get_connection()) is False


def test_schema_probe_rejects_a_missing_database(isolated_memory):
    assert db_store._schema_is_complete(db_store.get_db_path()) is False


def test_init_db_is_a_read_only_no_op_on_a_current_schema(isolated_memory, monkeypatch):
    """A fresh process on an initialised lake must not run the DDL transaction."""
    db_store.init_db()
    db_store._INITIALIZED_DB_PATHS.clear()
    db_store.close_connection()

    def _explode(_key):
        raise AssertionError("schema DDL must not run on a complete database")

    monkeypatch.setattr(db_store, "_init_db_once", _explode)
    db_store.init_db()
    assert str(db_store.get_db_path().resolve()) in db_store._INITIALIZED_DB_PATHS


# --- readers never rebuild --------------------------------------------------


def test_reader_never_rebuilds_the_projection(searchable_memory, monkeypatch):
    """The read path must not reach for the write lock, current or stale."""

    def _explode():
        raise AssertionError("a reader must not rebuild the page index projection")

    monkeypatch.setattr(page_index_projection, "ensure_page_index_projection", _explode)
    monkeypatch.setattr(page_index_projection, "refresh_page_index_projection", lambda *a, **k: _explode())

    assert "Alpha" in search_vector_lake("梅奥 诊所 人工智能", top_k=3)

    # ... and the same holds once the projection is behind.
    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha", "Concept_B": "Beta"}), [])
    assert "Alpha" in search_vector_lake("梅奥 诊所 人工智能", top_k=3)


def test_stale_projection_is_served_from_index_json(searchable_memory):
    """A stale projection must not block or blank the answer; the file wins."""
    nodes = _nodes({"Concept_A": "Renamed in the file", "Concept_B": "Beta", "Concept_C": "Gamma"})
    _write_index(searchable_memory, nodes, [{"source": "Concept_A", "target": "Concept_B", "weight": 3.0}])
    assert page_index_projection.projection_is_current() is False

    result = search_vector_lake("梅奥 诊所 人工智能", top_k=3)

    # The projection still holds the old title, so this proves the file answered.
    assert "Renamed in the file" in result
    assert "fell back to index.json" in result
    assert page_index_projection.nodes_by_key(["Concept_A"])["Concept_A"]["title"] == "Alpha"


def test_file_catalog_matches_the_sqlite_catalog(searchable_memory):
    """The fallback must return what the projection would have returned."""
    index_data = json.loads(get_index_path().read_text(encoding="utf-8"))
    catalog, note = page_index_projection.read_catalog()

    assert note is None  # the projection is current: no fallback in play
    file_catalog = page_index_projection._FileCatalog(index_data)
    keys = ["Concept_A", "Concept_B", "Concept_C", "Concept_missing"]
    assert file_catalog.nodes_by_key(keys) == page_index_projection.nodes_by_key(keys)
    assert file_catalog.adjacency() == page_index_projection.adjacency()
    assert file_catalog.node_summary_lines(5) == page_index_projection.node_summary_lines(5)
    assert catalog.nodes_by_key(keys) == page_index_projection.nodes_by_key(keys)


def test_missing_index_json_reports_drying(isolated_memory):
    db_store.init_db()

    catalog, note = page_index_projection.read_catalog()
    assert catalog is None
    result = search_vector_lake("anything", top_k=3)
    assert "drying" in result.lower()


# --- single writer heals ----------------------------------------------------


def test_heal_rebuilds_a_stale_projection(searchable_memory):
    from vector_lake.watchdog_app import heal_page_index_projection

    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha", "Concept_B": "Beta"}), [])
    assert page_index_projection.projection_is_current() is False

    report = heal_page_index_projection(force=True)

    assert report["healed"] is True
    assert page_index_projection.projection_is_current() is True


def test_heal_clears_a_stale_marker_without_rewriting(searchable_memory):
    from vector_lake.watchdog_app import heal_page_index_projection

    page_index_projection.mark_projection_stale("DatabaseLockTimeout: injected")
    assert page_index_projection.projection_stale() is not None

    report = heal_page_index_projection(force=True)

    assert report == {"healed": False, "reason": "current"}
    assert page_index_projection.projection_stale() is None


def test_heal_defers_instead_of_raising_when_the_lock_is_taken(searchable_memory, monkeypatch):
    from vector_lake.watchdog_app import heal_page_index_projection

    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha", "Concept_B": "Beta"}), [])

    def _locked():
        raise db_store.DatabaseLockTimeout("injected contention")

    monkeypatch.setattr(page_index_projection, "ensure_page_index_projection", _locked)

    report = heal_page_index_projection(force=True)

    assert report["healed"] is False
    assert "write lock unavailable" in report["reason"]


def test_heal_is_throttled_unless_forced(searchable_memory, monkeypatch):
    from vector_lake import watchdog_app

    calls = []
    monkeypatch.setattr(
        page_index_projection,
        "heal_projection_if_stale",
        lambda: calls.append(1) or {"healed": True, "reason": "rebuilt"},
    )
    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha"}), [])
    monkeypatch.setattr(watchdog_app, "_last_projection_heal", 0.0)

    first = watchdog_app.heal_page_index_projection()
    second = watchdog_app.heal_page_index_projection()

    assert first["healed"] is True
    assert second == {"healed": False, "reason": "throttled"}
    assert len(calls) == 1


def test_held_write_lock_no_longer_blocks_a_search(searchable_memory, monkeypatch):
    """The end-to-end shape of the original incident, in-process."""
    from vector_lake.db_store import DatabaseLockTimeout

    def _locked(*_args, **_kwargs):
        raise DatabaseLockTimeout("Could not acquire the Vector Lake write lock within 20s")

    monkeypatch.setattr(page_index_projection, "ensure_page_index_projection", _locked)
    monkeypatch.setattr(page_index_projection, "refresh_page_index_projection", _locked)
    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha", "Concept_B": "Beta"}), [])

    scored, notes, error = _search_scored_pages("梅奥 诊所 人工智能", top_k=3)

    assert error is None
    assert scored, "a stale projection must still yield results"
    assert any("fell back to index.json" in note for note in notes)


# --- fallback catalog caching and request-scoped reuse ----------------------


def test_fallback_catalog_is_cached_on_the_index_stamp(searchable_memory, monkeypatch):
    """A stale window must not re-parse index.json for every query."""
    from vector_lake import tool_search

    parses = []
    original = page_index_projection._load_index_json

    def _counted():
        parses.append(1)
        return original()

    monkeypatch.setattr(page_index_projection, "_load_index_json", _counted)
    page_index_projection.reset_catalog_cache()

    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha", "Concept_B": "Beta"}), [])
    assert page_index_projection.projection_is_current() is False

    first, note = page_index_projection.read_catalog()
    second, _ = page_index_projection.read_catalog()

    assert parses == [1]  # one parse, two reads
    assert first is second
    assert note and "fell back to index.json" in note


def test_cached_catalog_is_dropped_once_the_projection_catches_up(searchable_memory):
    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha"}), [])
    stale_catalog, _ = page_index_projection.read_catalog()
    assert page_index_projection._CATALOG_CACHE["catalog"] is stale_catalog

    page_index_projection.refresh_page_index_projection(
        json.loads(get_index_path().read_text(encoding="utf-8"))
    )
    fresh_catalog, note = page_index_projection.read_catalog()

    assert note is None
    assert fresh_catalog is not stale_catalog
    assert page_index_projection._CATALOG_CACHE["catalog"] is None


def test_a_changed_index_json_invalidates_the_cached_catalog(searchable_memory, monkeypatch):
    parses = []
    original = page_index_projection._load_index_json

    def _counted():
        parses.append(1)
        return original()

    monkeypatch.setattr(page_index_projection, "_load_index_json", _counted)
    page_index_projection.reset_catalog_cache()
    _write_index(searchable_memory, _nodes({"Concept_A": "First"}), [])
    page_index_projection.read_catalog()
    _write_index(searchable_memory, _nodes({"Concept_A": "Second", "Concept_B": "Beta"}), [])
    catalog, _ = page_index_projection.read_catalog()

    assert len(parses) == 2
    assert catalog.nodes_by_key(["Concept_A"])["Concept_A"]["title"] == "Second"


def test_assemble_context_parses_index_json_at_most_once(searchable_memory, monkeypatch):
    """One request resolves the catalog once, not once per collaborator."""
    from vector_lake import tool_search

    parses = []
    original = page_index_projection._load_index_json

    def _counted():
        parses.append(1)
        return original()

    monkeypatch.setattr(page_index_projection, "_load_index_json", _counted)
    page_index_projection.reset_catalog_cache()
    _write_index(searchable_memory, _nodes({"Concept_A": "Alpha", "Concept_B": "Beta"}), [])
    assert page_index_projection.projection_is_current() is False

    context = tool_search.assemble_context("梅奥 诊所 人工智能")

    assert len(parses) == 1
    assert context["wiki_page_count"] >= 1
    assert any("fell back to index.json" in note for note in context["retrieval_notes"])
    # Once, not twice.  The note is owned by ``_search_scored_pages``, which is handed the same
    # ``projection_note`` this function resolved; ``assemble_context`` re-appending it made every
    # degraded answer report the same fallback twice.
    assert sum(1 for note in context["retrieval_notes"] if "fell back to index.json" in note) == 1
    assert "Alpha" in context["index_summary"]
