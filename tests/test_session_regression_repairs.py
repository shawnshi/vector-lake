import json

import pytest

from vector_lake import db_store, governance_store, index_snapshot, tool_search


SIGNATURE = ("generation", 1, "digest", "canonical", 1)


def _context(monkeypatch, row, plaintext):
    monkeypatch.setattr(db_store, "get_connection", lambda: object())
    monkeypatch.setattr(db_store, "search_wiki", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(
        db_store,
        "verify_search_projection_integrity",
        lambda _conn: {"status": "ready", "signature": SIGNATURE},
    )
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda _conn: None)
    monkeypatch.setattr(tool_search, "_sqlite_identity_rows", lambda *_args: [])
    monkeypatch.setattr(tool_search, "_is_structural_noise", lambda *_args: False)
    monkeypatch.setattr(tool_search, "_load_current_plaintext_rows", lambda *_args, **_kwargs: plaintext)
    monkeypatch.setattr(tool_search, "_context_purpose", lambda _max: "purpose")
    monkeypatch.setattr(
        tool_search,
        "build_memory_packet",
        lambda *_args, **_kwargs: {
            "packet": "", "memory_count": 0, "warning_count": 0, "omitted_count": 0
        },
    )
    return tool_search._assemble_sqlite_context("needle", 10000)


def test_context_falls_back_to_canonical_search_summary(monkeypatch):
    result = _context(
        monkeypatch,
        {"node_key": "Page_A", "title": "Page A", "summary": "canonical summary", "rank": -1},
        {},
    )
    assert result["wiki_page_count"] == 1
    assert result["_retrieved_page_keys"] == ["Page_A"]
    assert "canonical summary" in result["wiki_context"]
    assert result["wiki_retrieval_degraded"] is False


def test_context_empty_search_summary_remains_degraded(monkeypatch):
    result = _context(
        monkeypatch,
        {"node_key": "Page_A", "summary": "  \n ", "rank": -1},
        {},
    )
    assert result["wiki_page_count"] == 0
    assert result["wiki_retrieval_degraded"] is True


def test_context_hardened_plaintext_wins_and_preserves_truncation(monkeypatch):
    result = _context(
        monkeypatch,
        {"node_key": "Page_A", "summary": "unsafe fallback", "rank": -1},
        {"Page_A": {"status": "available", "snippet": "hardened text", "truncated": True}},
    )
    assert "hardened text" in result["wiki_context"]
    assert "unsafe fallback" not in result["wiki_context"]
    assert result["wiki_text_truncated_count"] == 1


def test_context_bounds_fallback_summary(monkeypatch):
    result = _context(
        monkeypatch,
        {"node_key": "Page_A", "summary": "x" * 1300, "rank": -1},
        {},
    )
    excerpt, truncated = tool_search._bounded_excerpt("x" * 1300, 1200)
    assert excerpt in result["wiki_context"]
    assert truncated is True
    assert result["wiki_text_truncated_count"] == 1


class _ExactConnection:
    def __init__(self, identity_rows):
        self.identity_rows = identity_rows

    def execute(self, query, _params):
        assert "entity_identities" in query
        return self.identity_rows


def _exact_search(
    monkeypatch, *, row=None, statuses=None, signatures=None, identity_rows=None,
    plaintext_loader=None,
):
    statuses = statuses or {}
    signatures = iter(signatures or [SIGNATURE, SIGNATURE])
    if identity_rows is None:
        identity_rows = [{"page_key": "Page_A", "data_json": '{"status":"active"}'}]
    monkeypatch.setattr(db_store, "get_connection", lambda: _ExactConnection(identity_rows))
    monkeypatch.setattr(
        db_store,
        "verify_search_projection_integrity",
        lambda _conn: {"status": "ready", "signature": next(signatures)},
    )
    monkeypatch.setattr(
        db_store,
        "search_wiki",
        lambda *_args, **_kwargs: [row or {"node_key": "Page_A", "title": "Page A", "rank": -1}],
    )
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda _conn: None)
    monkeypatch.setattr(tool_search, "_sqlite_identity_rows", lambda *_args: [])
    if plaintext_loader is None:

        def plaintext_loader(*_args, **_kwargs):
            return statuses
    monkeypatch.setattr(tool_search, "_load_current_plaintext_rows", plaintext_loader)
    return tool_search._exact_fts_page_result("needle", 5, as_xml=False)


def test_exact_search_without_identity_owner_falls_through(monkeypatch):
    called = False

    def load_plaintext(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    assert _exact_search(
        monkeypatch, identity_rows=[], plaintext_loader=load_plaintext,
    ) is None
    assert called is False


def test_exact_search_filters_archived_identity_owner(monkeypatch):
    owners = [{"page_key": "Page_A", "data_json": '{"status":" Archived "}'}]
    assert _exact_search(monkeypatch, identity_rows=owners) is None


def test_exact_search_live_owner_uses_only_canonical_plaintext(monkeypatch):
    owners = [{
        "page_key": "Page_A",
        "data_json": json.dumps({"status": "active", "body": "identity body"}),
    }]
    plaintext = {
        "Page_A": {"status": "available", "snippet": "canonical plaintext"}
    }
    result = _exact_search(monkeypatch, statuses=plaintext, identity_rows=owners)
    assert "canonical plaintext" in result
    assert "identity body" not in result


def test_exact_search_skips_unparsable_identity_owner(monkeypatch):
    owners = [{"page_key": "Page_A", "data_json": "not-json"}]
    assert _exact_search(monkeypatch, identity_rows=owners) is None


def test_plaintext_unavailability_is_not_search_degradation(monkeypatch):
    result = _exact_search(monkeypatch)
    assert result.startswith("- **Page A**")
    assert "snippet: missing" in result
    assert "[Search degraded:" not in result
    assert "plaintext_" not in result


def test_exact_search_falls_back_to_canonical_row_title_and_bounded_summary(monkeypatch):
    result = _exact_search(monkeypatch, row={
        "node_key": "Page_A", "title": "Canonical Title",
        "summary": "  canonical\n" + "x" * 1700, "rank": -1,
    })
    assert result.startswith("- **Canonical Title**")
    assert "canonical " + "x" * 1590 in result
    assert "x" * 1591 not in result
    assert "Page_A**" not in result


def test_exact_search_plaintext_title_and_snippet_win_and_keep_truncated(monkeypatch):
    source = {
        "Page_A": {
            "status": "available", "title": "Plaintext Title",
            "snippet": "plaintext snippet", "truncated": True,
        }
    }
    result = _exact_search(
        monkeypatch,
        row={"node_key": "Page_A", "title": "Row Title", "summary": "row summary", "rank": -1},
        statuses=source,
    )
    assert result.startswith("- **Plaintext Title**")
    assert "plaintext snippet" in result
    assert "Row Title" not in result
    assert "row summary" not in result
    assert source["Page_A"]["truncated"] is True


def test_projection_integrity_change_still_requires_retry(monkeypatch):
    changed = ("generation", 2, "digest", "canonical", 1)
    with pytest.raises(tool_search.SearchIndexError, match="changed during exact search"):
        _exact_search(monkeypatch, signatures=[SIGNATURE, changed])


def test_fts_only_failure_keeps_exact_degradation_header(isolated_memory, monkeypatch):
    node = {"_key": "Page_A", "title": "Page A", "summary": "needle", "type": "concept", "status": "active"}
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "index.json").write_text(json.dumps({
        "nodes": {"Page_A": node}, "weighted_edges": [], "graph_state": {"dirty": False}
    }), encoding="utf-8")
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: {
        "nodes": {"Page_A": node}, "weighted_edges": [], "graph_state": {"dirty": False}
    })
    monkeypatch.setattr(
        tool_search,
        "read_committed_index_snapshot",
        lambda path, **_kwargs: index_snapshot.load_legacy_index_snapshot_for_migration(path),
    )
    index_snapshot.clear_index_snapshot_cache_for_tests()
    monkeypatch.setattr(tool_search, "_exact_fts_page_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_search, "_fts_projection_probe", lambda _index: (SIGNATURE, None))
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(tool_search.SearchBackendError("fts5")),
    )
    monkeypatch.setattr(db_store, "get_connection", lambda: object())
    monkeypatch.setattr(db_store, "verify_search_projection_integrity", lambda _conn: {"status": "ready", "signature": SIGNATURE})
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda _conn: None)
    monkeypatch.setattr(tool_search, "_load_current_plaintext_rows", lambda *_args, **_kwargs: {})
    result = tool_search.search_vector_lake("needle")
    body = result.split("</SemanticReadinessEnvelope>\n", 1)[1]
    assert body.startswith("[Search degraded: fts5]\n")
    assert "plaintext_" not in body


def test_plaintext_retrieval_failure_logs_type_without_degradation_token(
    isolated_memory, monkeypatch, caplog,
):
    node = {
        "_key": "Page_A", "title": "Page A", "summary": "needle",
        "type": "concept", "status": "active",
    }
    index = {
        "nodes": {"Page_A": node}, "weighted_edges": [],
        "graph_state": {"dirty": False},
    }
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "index.json").write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: index)
    monkeypatch.setattr(
        tool_search,
        "read_committed_index_snapshot",
        lambda path, **_kwargs: index_snapshot.load_legacy_index_snapshot_for_migration(path),
    )
    index_snapshot.clear_index_snapshot_cache_for_tests()
    monkeypatch.setattr(tool_search, "_exact_fts_page_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_search, "_fts_projection_probe", lambda _index: (SIGNATURE, None))
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    monkeypatch.setattr(db_store, "get_connection", lambda: object())
    monkeypatch.setattr(
        db_store, "verify_search_projection_integrity",
        lambda _conn: {"status": "ready", "signature": SIGNATURE},
    )
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda _conn: None)

    def fail_plaintext(*_args, **_kwargs):
        raise RuntimeError("sensitive exception details")

    recorded_issues = []
    monkeypatch.setattr(tool_search, "_load_current_plaintext_rows", fail_plaintext)
    monkeypatch.setattr(
        tool_search,
        "_record_search_performance",
        lambda _timings, **kwargs: recorded_issues.extend(kwargs["backend_issues"]),
    )
    caplog.set_level(30, logger=tool_search.log.name)

    result = tool_search.search_vector_lake("needle")

    assert "Plaintext snippet retrieval failed: RuntimeError" in caplog.text
    assert "sensitive exception details" not in caplog.text
    assert all("plaintext_" not in issue for issue in recorded_issues)
    assert "plaintext_" not in result


def test_genuine_projection_failure_keeps_projection_label(isolated_memory, monkeypatch):
    node = {"_key": "Page_A", "title": "Page A", "summary": "needle", "type": "concept", "status": "active"}
    index = {"nodes": {"Page_A": node}, "weighted_edges": [], "graph_state": {"dirty": False}}
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "index.json").write_text(json.dumps(index), encoding="utf-8")
    probes = iter([(SIGNATURE, None), (("generation", 2, "digest", "canonical", 1), "fts_projection_state_changed")])
    monkeypatch.setattr(tool_search, "_load_search_index", lambda _path: index)
    monkeypatch.setattr(
        tool_search,
        "read_committed_index_snapshot",
        lambda path, **_kwargs: index_snapshot.load_legacy_index_snapshot_for_migration(path),
    )
    index_snapshot.clear_index_snapshot_cache_for_tests()
    monkeypatch.setattr(tool_search, "_exact_fts_page_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_search, "_fts_projection_probe", lambda _index: next(probes))
    monkeypatch.setattr(tool_search, "_get_fts_search_results", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    monkeypatch.setattr(db_store, "get_connection", lambda: object())
    monkeypatch.setattr(db_store, "verify_search_projection_integrity", lambda _conn: {"status": "ready", "signature": SIGNATURE})
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda _conn: None)
    monkeypatch.setattr(tool_search, "_load_current_plaintext_rows", lambda *_args, **_kwargs: {})
    result = tool_search.search_vector_lake("needle")
    body = result.split("</SemanticReadinessEnvelope>\n", 1)[1]
    assert body.startswith("[Search degraded: fts_projection_state_changed]\n")
    assert "plaintext_" not in body


class _Connection:
    def __init__(self, rows=()):
        self.rows = rows

    def execute(self, _query, _params):
        return self.rows


def _artifact(source_id, raw_ref=None):
    value = {"artifact_id": "artifact", "source_id": source_id}
    if raw_ref is not None:
        value["raw_ref"] = raw_ref
    return value


# NOTE (pre-existing limitation, pinned here on purpose):
# `source_artifacts` is keyed by artifact_id alone and stores exactly one
# source_id/raw_ref. `artifact_id` is content-hash derived, so two distinct raw
# paths holding identical bytes share one row and the later foundation upsert
# rewrites that row's owner (last-writer-wins). These two tests therefore pin
# ONLY the preflight's permission, which restores the behaviour that existed
# before the over-broad guard. They are NOT evidence that the ownership record
# is preserved, and they are not a safety guarantee. Reviewers raised this as an
# open data-ownership decision (see the receipt); tracking per-path ownership
# needs a separate (artifact_id, raw_ref, source_id) relation or a composite
# upsert key, which is a schema change outside this regression repair.


def test_batch_allows_same_bytes_at_distinct_raw_refs():
    governance_store._preflight_source_artifact_ownership(
        _Connection(), [_artifact("source-a", "a.md"), _artifact("source-b", "b.md")]
    )


@pytest.mark.parametrize("entries", [
    [_artifact("source-a", "same.md"), _artifact("source-b", "same.md")],
    [_artifact("source-b", "same.md"), _artifact("source-a", "same.md")],
    [_artifact("source-a"), _artifact("source-b", "b.md")],
    [_artifact("source-a", "a.md"), _artifact("source-b")],
])
def test_batch_rejects_ambiguous_or_same_raw_ref(entries):
    with pytest.raises(ValueError, match="Conflicting source artifact ownership"):
        governance_store._preflight_source_artifact_ownership(_Connection(), entries)


def _stored_row(raw_ref, *, json_source="stored", physical_source="stored"):
    return {
        "artifact_id": "artifact",
        "source_id": physical_source,
        "data_json": json.dumps({
            "artifact_id": "artifact", "source_id": json_source, "raw_ref": raw_ref
        }),
    }


def test_existing_same_raw_ref_rejects_but_distinct_ref_allows():
    """Distinct raw_refs are permitted here; the stored-owner rewrite is the
    documented pre-existing limitation explained above, not a new guarantee."""
    with pytest.raises(ValueError, match="Conflicting source artifact ownership"):
        governance_store._preflight_source_artifact_ownership(
            _Connection([_stored_row("same.md")]), [_artifact("new", "same.md")]
        )
    governance_store._preflight_source_artifact_ownership(
        _Connection([_stored_row("old.md")]), [_artifact("new", "new.md")]
    )


def test_existing_physical_json_identity_drift_still_rejected():
    with pytest.raises(ValueError, match="Invalid source artifact ownership record"):
        governance_store._preflight_source_artifact_ownership(
            _Connection([_stored_row("same.md", json_source="other")]),
            [_artifact("stored", "same.md")],
        )
