import pytest

from vector_lake import db_store, governance_store, provenance
from vector_lake.source_references import source_id_for_raw_ref


def _trace_change_set(
    *,
    page_key: str,
    claim_id: str,
    claim_text: str,
    source_page: str,
    entity_id: str,
    entity_name: str,
    source_id: str,
    canonical_source_page: str,
) -> dict:
    return {
        "affected_pages": [f"{page_key}.md"],
        "proposed_entities": [
            {
                "entity_id": entity_id,
                "id": entity_id,
                "page_key": page_key,
                "canonical_name": entity_name,
                "title": entity_name,
                "type": "concept",
                "status": "Active",
                "aliases": [],
                "sources": [],
            }
        ],
        "proposed_claims": [
            {
                "claim_id": claim_id,
                "claim_text": claim_text,
                "claim_type": "assertion",
                "claim_scope": "block",
                "status": "Active",
                "confidence": 0.8,
                "subject_entity_ids": [entity_id],
                "evidence_ids": [],
                "source_ids": [source_id],
                "locator": {
                    "page_key": page_key,
                    "heading": "Facts",
                    "block_index": 1,
                },
                "source_page": source_page,
            }
        ],
        "proposed_evidence": [],
        "proposed_source_updates": [
            {
                "source_id": source_id,
                "canonical_source_page": canonical_source_page,
            }
        ],
        "proposed_source_artifacts": [],
        "proposed_extraction_runs": [],
        "proposed_edges": [],
    }


def _seed_trace_records() -> None:
    db_store.init_db()
    governance_store.apply_change_set(
        _trace_change_set(
            page_key="Concept_Boost",
            claim_id="claim_boost",
            claim_text="unrelated statement",
            source_page="PageBoost",
            entity_id="entity_boost",
            entity_name="Boost Entity",
            source_id="source_boost",
            canonical_source_page="Source_Boost.md",
        )
    )
    governance_store.apply_change_set(
        _trace_change_set(
            page_key="Concept_Text",
            claim_id="claim_text",
            claim_text="alpha beta statement",
            source_page="PageText",
            entity_id="entity_text",
            entity_name="Text Entity",
            source_id="source_text",
            canonical_source_page="Source_Text.md",
        )
    )
    db_store.init_db()


def test_public_trace_exact_claim_id_avoids_fts_and_store_bootstrap(isolated_memory, monkeypatch):
    from vector_lake import tool_trace
    _seed_trace_records()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact trace must not use FTS or canonical bootstrap")
    monkeypatch.setattr(db_store, "search_wiki", forbidden)
    monkeypatch.setattr(governance_store, "initialize_meta_store", forbidden)
    monkeypatch.setattr(governance_store, "ensure_canonical_store_populated", forbidden)
    statements = []
    conn = db_store.get_connection()
    before = conn.total_changes
    conn.set_trace_callback(statements.append)
    try:
        result = tool_trace.trace_vector_lake("  claim_text  ")
    finally:
        conn.set_trace_callback(None)
    assert "claim_text" in result and "alpha beta statement" in result
    assert "Accepted Fact: False" in result
    assert conn.total_changes == before
    assert any("FROM claims WHERE claim_id =" in sql for sql in statements)


def test_missing_trace_claim_id_never_falls_back_to_lexical_hits(isolated_memory, monkeypatch):
    _seed_trace_records()
    conn = db_store.get_connection()
    conn.execute("UPDATE claims SET claim_text=?, data_json=json_set(data_json, '$.claim_text', ?) WHERE claim_id='claim_text'", ("mentions claim_missing", "mentions claim_missing"))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("missing exact ID must not fall back to search")
    monkeypatch.setattr(db_store, "search_wiki", forbidden)
    assert provenance.build_trace_for_query("claim_missing")["items"] == []


def test_public_trace_missing_database_does_not_create_store(isolated_memory):
    from vector_lake import tool_trace
    with pytest.raises(RuntimeError, match="database_not_initialized"):
        tool_trace.trace_vector_lake("claim_missing")
    assert not (isolated_memory / "wiki/.meta/vector_lake.db").exists()


@pytest.mark.parametrize("payload", ['[]', '{"claim_id":"claim_wrong"}'])
def test_exact_trace_rejects_corrupt_or_mismatched_identity(isolated_memory, payload):
    _seed_trace_records()
    db_store.get_connection().execute("UPDATE claims SET data_json=? WHERE claim_id='claim_text'", (payload,))
    with pytest.raises(RuntimeError, match="Invalid canonical claim"):
        provenance.build_trace_for_query("claim_text")


def test_exact_trace_malformed_json_reader_error_is_not_no_data(monkeypatch):
    from types import SimpleNamespace
    row = {"claim_id": "claim_text", "data_json": "{"}
    cursor = SimpleNamespace(fetchone=lambda: row)
    conn = SimpleNamespace(execute=lambda *_args: cursor)
    monkeypatch.setattr(governance_store, "require_current_schema_for_read", lambda *_: conn)
    with pytest.raises(RuntimeError, match="Invalid canonical claim metadata"):
        governance_store.select_trace_claim_by_id("claim_text")


def test_trace_uses_bounded_claims_and_referenced_labels(
    isolated_memory,
    monkeypatch,
):
    _seed_trace_records()
    monkeypatch.setattr(
        db_store,
        "search_wiki",
        lambda _query, limit=10: [{"node_key": "Concept_Boost"}],
    )

    def reject_full_load(*_args, **_kwargs):
        raise AssertionError("trace must not load a complete canonical store")

    monkeypatch.setattr(governance_store, "load_claims", reject_full_load)
    monkeypatch.setattr(governance_store, "load_entities", reject_full_load)
    monkeypatch.setattr(governance_store, "load_sources", reject_full_load)
    monkeypatch.setattr(
        governance_store,
        "initialize_meta_store",
        lambda: reject_full_load(),
    )

    trace = provenance.build_trace_for_query("alpha beta", top_k=2)

    assert [item["claim_id"] for item in trace["items"]] == [
        "claim_boost",
        "claim_text",
    ]
    assert trace["items"][0]["subject_entities"] == ["Boost Entity"]
    assert trace["items"][0]["source_pages"] == []
    assert trace["items"][0]["sources"] == [
        {
            "source_id": "source_boost",
            "raw_ref": "",
            "artifact_id": None,
            "page": None,
            "resolution": "unresolved",
            "reason": "missing_or_non_source_page",
            "locator": {"page_key": "Concept_Boost", "heading": "Facts", "block_index": 1},
        }
    ]
    assert trace["items"][1]["subject_entities"] == ["Text Entity"]
    assert trace["items"][1]["source_pages"] == []
    assert trace["items"][0]["evidence_count"] == 0
    assert trace["items"][0]["acceptance_status"] == "not_assessed"
    assert trace["items"][0]["accepted_fact"] is False


def test_resolved_source_and_dangling_evidence_never_promote_acceptance(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setattr(
        governance_store,
        "select_trace_claims",
        lambda *_args, **_kwargs: [
            {
                "claim_id": "claim_dangling",
                "claim_text": "bounded claim",
                "subject_entity_ids": [],
                "source_ids": ["source_owned"],
                "evidence_ids": ["evidence_missing"],
                "status": "Active",
            }
        ],
    )
    monkeypatch.setattr(
        governance_store,
        "load_trace_labels",
        lambda *_args, **_kwargs: (
            {},
            {"source_owned": "Source_Owned.md"},
            {
                "source_owned": {
                    "source_id": "source_owned",
                    "raw_ref": "imports/a/report.pdf",
                    "artifact_id": "artifact_owned",
                    "page": "Source_Owned.md",
                    "resolution": "resolved",
                    "reason": "identity_bound_mapping",
                }
            },
        ),
    )

    item = provenance.build_trace_for_query(
        "bounded", top_k=1, relevant_pages=set()
    )["items"][0]

    assert item["evidence_count"] == 1
    assert item["sources"][0]["resolution"] == "resolved"
    assert item["acceptance_status"] == "not_assessed"
    assert item["accepted_fact"] is False
    assert "active_evidence_count" not in item
    assert "accepted_evidence_count" not in item


@pytest.mark.parametrize("source_kind", ["raw", "official"])
def test_raw_source_mapping_requires_exact_owner_declaration(monkeypatch, source_kind):
    raw_ref = "imports/a/report.pdf"
    source_id = source_id_for_raw_ref(raw_ref)
    if source_kind == "official":
        from vector_lake.tool_claim_provenance import _claim_stable_id, _json_sha256

        source_id = _claim_stable_id("source", "official:" + _json_sha256({"snapshot": "test"}))
        assert source_id != source_id_for_raw_ref(raw_ref)
    source_record = {
        "source_id": source_id,
        "raw_ref": raw_ref,
        "canonical_source_page": "Source_Report.md",
        "artifact_id": "artifact_report",
    }

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class Connection:
        owner_sources = [raw_ref]

        def execute(self, sql, _args):
            if "FROM sources" in sql:
                return Cursor([{"source_id": source_id, "data_json": __import__("json").dumps(source_record)}])
            if "FROM entity_identities" in sql:
                entity = {"type": "source", "sources": self.owner_sources}
                return Cursor([{"page_key": "Source_Report", "data_json": __import__("json").dumps(entity)}])
            if "FROM source_artifacts" in sql:
                return Cursor([{"present": 1}])
            return Cursor([])

    connection = Connection()
    monkeypatch.setattr(
        governance_store, "require_current_schema_for_read", lambda *_args: connection
    )

    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages[source_id] == "Source_Report.md"
    assert details[source_id]["reason"] == "identity_bound_mapping"

    connection.owner_sources = ["imports/b/report.pdf"]
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["reason"] == "source_ownership_mismatch"

    connection.owner_sources = []
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["reason"] == "missing_source_ownership_declaration"

    connection.owner_sources = raw_ref
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}

    connection.owner_sources = [raw_ref]
    assert source_record["source_id"] == source_id
    assert source_record["raw_ref"] == raw_ref
    source_record["source_id"] = "source_foreign_record"
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["reason"] == "source_identity_mismatch"


@pytest.mark.parametrize("wiki_ref", ["Source_Report", "Source_Report.md", "[[Source_Report]]"])
def test_explicit_wiki_reference_resolves_page_not_raw_ownership(monkeypatch, wiki_ref):
    import json

    source_id = source_id_for_raw_ref(wiki_ref)
    record = {
        "source_id": source_id,
        "raw_ref": wiki_ref,
        "canonical_source_page": "Source_Report.md",
        "artifact_id": "artifact_wiki_ref",
    }

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class Connection:
        page_type = "source"
        artifact_present = True

        def execute(self, sql, args):
            if "FROM sources" in sql:
                return Cursor([{"source_id": source_id, "data_json": json.dumps(record)}])
            if "FROM entity_identities" in sql:
                # A real Source declares its raw document, never its own WikiRef.
                entity = {"type": self.page_type, "sources": ["raw/research/original.pdf"]}
                return Cursor([{"page_key": "Source_Report", "data_json": json.dumps(entity)}])
            if "FROM source_artifacts" in sql:
                assert args == ("artifact_wiki_ref", source_id)
                return Cursor([{"present": 1}] if self.artifact_present else [])
            return Cursor([])

    connection = Connection()
    monkeypatch.setattr(governance_store, "require_current_schema_for_read", lambda *_: connection)
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {source_id: "Source_Report.md"}
    assert details[source_id]["resolution"] == "resolved"
    assert details[source_id]["raw_ref"] == wiki_ref
    record["canonical_source_page"] = "Source_Other.md"
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["reason"] == "ambiguous_mapping"
    record["canonical_source_page"] = "Source_Report.md"
    connection.page_type = "concept"
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["reason"] == "missing_or_non_source_page"
    connection.page_type = "source"
    connection.artifact_present = False
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["reason"] == "missing_artifact"


def test_trace_reuses_preselected_pages_without_second_search(
    isolated_memory,
    monkeypatch,
):
    _seed_trace_records()
    monkeypatch.setattr(
        db_store,
        "search_wiki",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preselected trace pages must not repeat FTS search")
        ),
    )

    trace = provenance.build_trace_for_query(
        "alpha beta",
        top_k=2,
        relevant_pages={"Concept_Boost"},
    )

    assert [item["claim_id"] for item in trace["items"]] == [
        "claim_boost",
        "claim_text",
    ]


def test_trace_reports_unknown_and_structural_sources_without_links(isolated_memory):
    db_store.init_db()
    _, pages, details = governance_store.load_trace_labels(set(), {"source_missing"})
    assert pages == {}
    assert details["source_missing"]["resolution"] == "unresolved"
    assert details["source_missing"]["reason"] == "unknown_source"

    raw_ref = "Source_Auto_Fixed"
    source_id = source_id_for_raw_ref(raw_ref)
    governance_store.apply_change_set(
        {
            "affected_pages": [],
            "proposed_entities": [],
            "proposed_claims": [],
            "proposed_evidence": [],
            "proposed_source_updates": [{
                "source_id": source_id,
                "raw_ref": raw_ref,
                "canonical_source_page": "Source_Auto_Fixed.md",
            }],
            "proposed_source_artifacts": [],
            "proposed_extraction_runs": [],
            "proposed_edges": [],
        }
    )
    _, pages, details = governance_store.load_trace_labels(set(), {source_id})
    assert pages == {}
    assert details[source_id]["resolution"] == "structural-placeholder"
    assert details[source_id]["page"] is None


def test_canonical_store_population_check_uses_scalar_counts(
    isolated_memory,
    monkeypatch,
):
    _seed_trace_records()

    def reject_full_load(*_args, **_kwargs):
        raise AssertionError("population check must not hydrate canonical objects")

    monkeypatch.setattr(governance_store, "load_claims", reject_full_load)
    monkeypatch.setattr(governance_store, "load_entities", reject_full_load)
    monkeypatch.setattr(governance_store, "load_sources", reject_full_load)

    result = governance_store.ensure_canonical_store_populated()

    assert result == {
        "bootstrapped": False,
        "entities": 2,
        "claims": 2,
        "sources": 2,
        "pages_scanned": 0,
    }


def test_trace_top_k_contract(isolated_memory, monkeypatch):
    _seed_trace_records()
    monkeypatch.setattr(db_store, "search_wiki", lambda *_args, **_kwargs: [])

    assert provenance.build_trace_for_query("alpha", top_k=0)["items"] == []
    with pytest.raises(ValueError, match="top_k"):
        provenance.build_trace_for_query("alpha", top_k=-1)


def test_trace_preserves_unicode_case_matching(isolated_memory):
    db_store.init_db()
    governance_store.apply_change_set(
        _trace_change_set(
            page_key="Concept_Unicode",
            claim_id="claim_unicode",
            claim_text="CAFÉ clinical workflow",
            source_page="PageUnicode",
            entity_id="entity_unicode",
            entity_name="Unicode Entity",
            source_id="source_unicode",
            canonical_source_page="Source_Unicode.md",
        )
    )

    claims = governance_store.select_trace_claims(["café"], set(), top_k=1)

    assert [claim["claim_id"] for claim in claims] == ["claim_unicode"]


def test_search_wiki_retries_natural_language_question_with_bounded_or(
    monkeypatch,
):
    matches = []

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Connection:
        def execute(self, _sql, args):
            matches.append(args[0])
            if len(matches) == 1:
                return Cursor([])
            return Cursor(
                [
                    {
                        "node_key": "Concept_Agentic-Automation",
                        "title": "Agentic Automation",
                        "summary": "bounded evidence",
                        "rank": -2.0,
                    }
                ]
            )

    monkeypatch.setattr(db_store, "get_connection", lambda: Connection())
    monkeypatch.setattr(
        "vector_lake.tokenizer_runtime.tokenize_for_fts",
        lambda _query: "what evidence supports agentic automation in the current lake",
    )

    rows = db_store.search_wiki(
        "What evidence supports Agentic Automation in the current lake?",
        limit=5,
    )

    assert rows[0]["node_key"] == "Concept_Agentic-Automation"
    assert matches == [
        '"what" "evidence" "supports" "agentic" "automation" "in" "the" "current" "lake"',
        '"agentic" OR "automation"',
    ]


def test_trace_ties_use_stable_claim_id_not_insertion_rowid(isolated_memory):
    db_store.init_db()
    for claim_id in ("claim_z", "claim_a"):
        governance_store.apply_change_set(
            _trace_change_set(
                page_key=f"Concept_{claim_id}",
                claim_id=claim_id,
                claim_text="same café statement",
                source_page=f"Page_{claim_id}",
                entity_id=f"entity_{claim_id}",
                entity_name=claim_id,
                source_id=f"source_{claim_id}",
                canonical_source_page=f"Source_{claim_id}.md",
            )
        )

    ascii_claims = governance_store.select_trace_claims(["same"], set(), top_k=2)
    unicode_claims = governance_store.select_trace_claims(["café"], set(), top_k=2)

    assert [claim["claim_id"] for claim in ascii_claims] == [
        "claim_a",
        "claim_z",
    ]
    assert [claim["claim_id"] for claim in unicode_claims] == [
        "claim_a",
        "claim_z",
    ]
