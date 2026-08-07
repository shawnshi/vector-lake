import pytest

from vector_lake import db_store, governance_store, provenance


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


def test_trace_uses_bounded_claims_and_referenced_labels(
    isolated_memory,
    monkeypatch,
):
    _seed_trace_records()
    monkeypatch.setattr(
        db_store,
        "search_wiki",
        lambda _query, limit=10: [{"node_key": "PageBoost"}],
    )

    def reject_full_load(*_args, **_kwargs):
        raise AssertionError("trace must not load a complete canonical store")

    monkeypatch.setattr(governance_store, "load_claims", reject_full_load)
    monkeypatch.setattr(governance_store, "load_entities", reject_full_load)
    monkeypatch.setattr(governance_store, "load_sources", reject_full_load)

    trace = provenance.build_trace_for_query("alpha beta", top_k=2)

    assert [item["claim_id"] for item in trace["items"]] == [
        "claim_boost",
        "claim_text",
    ]
    assert trace["items"][0]["subject_entities"] == ["Boost Entity"]
    assert trace["items"][0]["source_pages"] == ["Source_Boost.md"]
    assert trace["items"][1]["subject_entities"] == ["Text Entity"]
    assert trace["items"][1]["source_pages"] == ["Source_Text.md"]


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

    ascii_claims = governance_store.select_trace_claims(
        ["same"], set(), top_k=2
    )
    unicode_claims = governance_store.select_trace_claims(
        ["café"], set(), top_k=2
    )

    assert [claim["claim_id"] for claim in ascii_claims] == [
        "claim_a",
        "claim_z",
    ]
    assert [claim["claim_id"] for claim in unicode_claims] == [
        "claim_a",
        "claim_z",
    ]
