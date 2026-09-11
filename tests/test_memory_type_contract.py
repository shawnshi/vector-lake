import pytest

from vector_lake.governance_store import _memory_object_from_claim, infer_memory_type
from vector_lake.operational_memory_contract import effective_routing_type
from vector_lake.claim_extractor import extract_page_objects, _stable_id


def _claim(**overrides):
    claim = {
        "claim_id": "opaque-claim-id",
        "claim_text": "Neutral knowledge.",
        "source_page": "Source_Article.md",
        "source_ids": ["opaque-source-id"],
        "evidence_ids": ["opaque-evidence-id"],
    }
    claim.update(overrides)
    return claim


def test_trigger_words_do_not_promote_articles():
    for text in (
        "方案采用状态：不要上线。",
        "A decision records a preference, task, pending status and approval.",
    ):
        assert infer_memory_type(_claim(claim_text=text)) == "fact"


def test_explicit_type_or_authoring_marker_on_article_is_insufficient():
    claim = _claim(
        memory_type="preference",
        authoring_origin="governed_operational_memory",
        operational_memory_provenance=True,
    )
    assert infer_memory_type(claim) == "fact"


def test_legacy_governed_contract_requires_matching_page_type_and_source_marker():
    genuine = _claim(
        source_page="Concept_SystemDecisions.md",
        memory_type="decision",
        operational_memory_provenance=True,
    )
    assert infer_memory_type(genuine) == "decision"
    assert infer_memory_type({**genuine, "source_page": "Concept_UserPreferences.md"}) == "fact"
    assert infer_memory_type({**genuine, "memory_type": "preference"}) == "fact"
    assert infer_memory_type({**genuine, "source_page": "raw/Concept_SystemDecisions.md"}) == "fact"
    assert infer_memory_type({**genuine, "operational_memory_provenance": False}) == "fact"

    legacy = {
        **genuine,
        "inline_sources": ["Source_Operational-Memory"],
    }
    legacy.pop("operational_memory_provenance")
    assert infer_memory_type(legacy) == "decision"
    assert infer_memory_type({**legacy, "inline_sources": ["Source_Operational.md-Memory"]}) == "fact"


def _operational_page(memory_type, inline_source):
    page_by_type = {
        "preference": "Concept_UserPreferences.md",
        "decision": "Concept_SystemDecisions.md",
        "task_state": "Concept_AgentTaskState.md",
        "fact": "Concept_OperationalFacts.md",
    }
    page = page_by_type[memory_type]
    frontmatter = {
        "id": f"operational-memory-{memory_type.replace('_', '-')}",
        "title": "Operational Memory Contract Test",
        "type": "concept",
        "memory_type": memory_type,
        "domain": "system",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["System_Architecture"],
        "sources": [],
        "strategic_scope": "core",
        "evidence_tier": "derived",
        "topic_cluster": "Operational_Memory",
        "authoring_origin": "governed_operational_memory",
        "updated": "2026-09-09T00:00:00+00:00",
    }
    body = (
        "# Operational Memory Contract Test\n\n"
        "## 1. 编译事实 (Compiled Truth - READ MODEL)\n"
        f"A unique operational statement. (Source: [[{inline_source}]])\n\n"
        "## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)\n"
    )
    return page, frontmatter, body


@pytest.mark.parametrize(
    ("inline_source", "expected_provenance", "expected_type"),
    [
        ("Source_Operational-Memory", True, "decision"),
        ("Source_Operational-Memory.MD", True, "decision"),
        ("Source_Operational.md-Memory", False, "fact"),
    ],
)
def test_extractor_classifies_original_inline_wikiref_without_changing_identity(
    inline_source, expected_provenance, expected_type
):
    page, frontmatter, body = _operational_page("decision", inline_source)
    objects = extract_page_objects(page, frontmatter, body)
    claim = next(
        item for item in objects["claims"]
        if "A unique operational statement" in item["claim_text"]
    )

    assert claim["operational_memory_provenance"] is expected_provenance
    assert infer_memory_type(claim) == expected_type
    if inline_source == "Source_Operational.md-Memory":
        legacy_ref = "Source_Operational-Memory"
        assert claim["inline_sources"] == [legacy_ref]
        assert claim["source_ids"] == [_stable_id("source", legacy_ref)]
        assert claim["evidence_ids"] == [
            _stable_id(
                "evidence",
                f"Concept_SystemDecisions:{legacy_ref}:{claim['claim_text']}",
            )
        ]
        memory = _memory_object_from_claim(claim)
        assert effective_routing_type(memory, claim) == "fact"


def test_extractor_uses_original_inline_ref_for_provenance_but_preserves_legacy_identity():
    page, frontmatter, body = _operational_page("decision", "Source_Operational.md-Memory")
    objects = extract_page_objects(page, frontmatter, body)
    claim = next(item for item in objects["claims"] if "unique operational statement" in item["claim_text"].lower())

    assert claim["inline_sources"] == ["Source_Operational-Memory"]
    assert claim["operational_memory_provenance"] is False
    assert infer_memory_type(claim) == "fact"
    memory = _memory_object_from_claim(claim)
    assert effective_routing_type(memory, claim) == "fact"
    assert claim["source_ids"] == [objects["sources"][0]["source_id"]]
    assert claim["evidence_ids"] == [objects["evidence"][0]["evidence_id"]]


def test_extractor_accepts_only_strict_operational_markers_on_fixed_pages():
    for marker in ("Source_Operational-Memory", "Source_Operational-Memory.MD"):
        page, frontmatter, body = _operational_page("preference", marker)
        claim = next(
            item for item in extract_page_objects(page, frontmatter, body)["claims"]
            if "unique operational statement" in item["claim_text"].lower()
        )
        assert claim["operational_memory_provenance"] is True
        assert infer_memory_type(claim) == "preference"

    page, frontmatter, body = _operational_page("preference", "Source_Operational-Memory")
    frontmatter["type"] = "source"
    claim = next(
        item for item in extract_page_objects("Source_Ordinary.md", frontmatter, body)["claims"]
        if "unique operational statement" in item["claim_text"].lower()
    )
    assert claim["operational_memory_provenance"] is True
    assert infer_memory_type(claim) == "fact"


def test_derived_memory_preserves_identity_and_classification_provenance():
    claim = _claim(
        source_page="Concept_AgentTaskState.md",
        memory_type="task_state",
        operational_memory_provenance=True,
    )
    memory = _memory_object_from_claim(claim)
    assert memory["memory_type"] == "task_state"
    assert memory["source_claim_id"] == claim["claim_id"]
    assert memory["source_ids"] == claim["source_ids"]
    assert memory["evidence_ids"] == claim["evidence_ids"]
    assert memory["operational_memory_provenance"] is True


def test_operative_routing_requires_matching_canonical_claim():
    claim = _claim(
        source_page="Concept_UserPreferences.md",
        memory_type="preference",
        operational_memory_provenance=True,
    )
    memory = _memory_object_from_claim(claim)
    before = dict(memory)
    assert effective_routing_type(memory, claim) == "preference"
    assert effective_routing_type(memory, None) == "fact"
    assert effective_routing_type(memory, {**claim, "status": "Archived"}) == "fact"
    assert effective_routing_type(memory, {**claim, "validity_state": "superseded"}) == "fact"
    assert effective_routing_type(memory, {**claim, "claim_id": "foreign"}) == "fact"
    assert effective_routing_type({**memory, "text": "Borrowed instruction"}, claim) == "fact"
    assert effective_routing_type({**memory, "source_page": "Source_Article.md"}, claim) == "fact"
    assert memory == before


def test_routing_does_not_hide_invalid_canonical_json(monkeypatch):
    import pytest
    from vector_lake import governance_store

    class Cursor:
        def fetchall(self):
            return [("claim_bad", "{broken")]

    class Connection:
        def execute(self, sql, params):
            assert "FROM claims" in sql
            assert params == ["claim_bad"]
            return Cursor()

    monkeypatch.setattr(governance_store, "require_current_schema_for_read", lambda *_: Connection())
    with pytest.raises(RuntimeError, match="Invalid canonical claim metadata"):
        governance_store.apply_effective_memory_routing([{"source_claim_id": "claim_bad"}])


def test_polluted_stored_type_routes_as_fact_without_mutating_identity():
    memory = {
        "memory_id": "stored-id",
        "memory_type": "preference",
        "source_claim_id": "opaque-claim-id",
        "source_page": "Source_Article.md",
    }
    before = dict(memory)
    assert effective_routing_type(memory, _claim(memory_type="preference")) == "fact"
    assert memory == before
