from vector_lake.merge_analysis import (
    analyze_entities,
    filename_candidate_pairs,
    normalize_name,
    source_identity_candidate_pairs,
)


def _entity(entity_id: str, name: str, **overrides) -> dict:
    entity = {
        "entity_id": entity_id,
        "canonical_name": name,
        "page_key": f"Concept_{name}",
        "type": "concept",
        "status": "Active",
        "aliases": [],
        "domain": "General",
        "topic_cluster": "General",
        "sources": [],
    }
    entity.update(overrides)
    return entity


def _pair_for(results: list[dict], left_id: str, right_id: str) -> dict:
    pair_ids = {left_id, right_id}
    return next(
        item
        for item in results
        if {item["left_entity_id"], item["right_entity_id"]} == pair_ids
    )


def test_normalize_name_preserves_unicode_and_distinguishes_chinese_names():
    assert normalize_name("Concept_5G通信") == "5g通信"
    assert normalize_name("地坛医院") == "地坛医院"
    assert normalize_name("天坛医院") == "天坛医院"
    assert normalize_name("地坛医院") != normalize_name("天坛医院")


def test_normalize_name_only_strips_markdown_suffix_not_dotted_identity():
    assert normalize_name("2604.14228v1") == "260414228v1"
    assert normalize_name("2604.08224v1") == "260408224v1"
    assert normalize_name("Software 3.0 A") == "software30a"
    assert normalize_name("Software 3.0 B") == "software30b"
    assert normalize_name("Concept_File.md") == "file"


def test_filename_candidates_recall_punctuation_variants_without_raw_sort_window():
    keys = ["Concept_A_B", *(f"Concept_AA{i:03d}" for i in range(80)), "Concept_AB"]

    pairs = filename_candidate_pairs(keys)

    assert any({left, right} == {"Concept_A_B", "Concept_AB"} for left, right, _ in pairs)


def test_filename_candidates_recall_hash_revision_siblings_regardless_of_name_length():
    keys = {
        "Source_2605-14892v2",
        "Source_2605-14892v2-0dadf455",
        "Source_AI-Agent-为什么是AIGC最后的杀手锏",
        "Source_AI-Agent-为什么是AIGC最后的杀手锏-9b4f8b4c",
    }

    pairs = filename_candidate_pairs(keys)

    assert any(
        {left, right}
        == {"Source_2605-14892v2", "Source_2605-14892v2-0dadf455"}
        for left, right, _ in pairs
    )
    assert any(
        {left, right}
        == {
            "Source_AI-Agent-为什么是AIGC最后的杀手锏",
            "Source_AI-Agent-为什么是AIGC最后的杀手锏-9b4f8b4c",
        }
        for left, right, _ in pairs
    )


def test_source_identity_candidates_recall_same_raw_path_with_different_names():
    pairs = source_identity_candidate_pairs(
        {
            "Source_Curated": ["raw/team/report.md"],
            "Source_Historical-abcdef12": ["MEMORY/raw/team/report.md"],
            "Source_Unrelated": ["raw/team/other.md"],
        }
    )

    assert pairs == [
        (
            "Source_Curated",
            "Source_Historical-abcdef12",
            "raw/team/report.md",
        )
    ]


def test_overview_singular_plural_is_not_hard_excluded():
    entities = [
        _entity(
            "overview_models",
            "Overview-AI-Models",
            sources=["Source_Stanford_AI_Report"],
            raw_text="A survey of current foundation models and their capabilities.",
        ),
        _entity(
            "overview_model",
            "Overview-AI-Model",
            sources=["Source_Stanford_AI_Report"],
            raw_text="A survey of current foundation model capabilities.",
        ),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "overview_models", "overview_model")

    assert pair["decision"] in {"merge", "alias"}
    assert "overview-distinct" not in pair["reasons"]


def test_numeric_suffix_conflict_stays_separate():
    entities = [
        _entity("alphafold", "AlphaFold"),
        _entity("alphafold2", "AlphaFold2"),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "alphafold", "alphafold2")

    assert pair["decision"] == "keep_separate"
    assert "numeric-identity-conflict" in pair["reasons"]


def test_dotted_product_versions_stay_separate():
    entities = [
        _entity("glm_45", "GLM-4.5V", type="product", page_key="Product_GLM-4-5V"),
        _entity(
            "glm_41",
            "GLM-4.1V-Thinking",
            type="product",
            page_key="Product_GLM-4-1V-Thinking",
        ),
    ]

    results = analyze_entities(entities, limit=None)

    assert not any(
        {item["left_entity_id"], item["right_entity_id"]} == {"glm_45", "glm_41"}
        and item["decision"] == "merge"
        for item in results
    )


def test_exact_name_across_entity_types_is_not_an_automatic_merge():
    entities = [
        _entity("vendor_acme", "Acme", type="vendor", page_key="Vendor_Acme"),
        _entity("concept_acme", "Acme", type="concept", page_key="Concept_Acme"),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "vendor_acme", "concept_acme")

    assert pair["decision"] == "review"
    assert "entity-type-conflict" in pair["reasons"]


def test_source_and_entity_with_same_name_stay_separate():
    entities = [
        _entity("source_acme", "Acme", type="source", page_key="Source_Acme"),
        _entity("vendor_acme", "Acme", type="vendor", page_key="Vendor_Acme"),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "source_acme", "vendor_acme")

    assert pair["decision"] == "keep_separate"
    assert "entity-type-conflict" in pair["reasons"]


def test_generic_source_does_not_turn_similar_names_into_merge():
    entities = [
        _entity(
            "human_in",
            "Human-in-the-Loop",
            aliases=["HITL"],
            sources=["Source_Auto_Fixed"],
        ),
        _entity(
            "human_on",
            "Human-on-the-Loop",
            aliases=["HITL"],
            sources=["Source_Auto_Fixed"],
        ),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "human_in", "human_on")

    assert pair["decision"] in {"review", "keep_separate"}
    assert "stable-source-overlap" not in pair["reasons"]


def test_source_exact_name_without_identity_evidence_requires_review():
    entities = [
        _entity("source_a", "Quarterly Report", type="source", page_key="Source_Report-A"),
        _entity("source_b", "Quarterly Report", type="source", page_key="Source_Report-B"),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "source_a", "source_b")

    assert pair["decision"] == "review"
    assert "source-raw-identity-missing" in pair["reasons"]


def test_source_revision_hash_is_strong_identity_evidence():
    entities = [
        _entity(
            "source_a",
            "Quarterly Report",
            type="source",
            page_key="Source_Report",
            sources=["raw/reports/quarterly.md"],
        ),
        _entity(
            "source_b",
            "Quarterly Report",
            type="source",
            page_key="Source_Report-ab12cd34",
            sources=["raw/reports/quarterly.md"],
        ),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "source_a", "source_b")

    assert pair["decision"] == "merge"
    assert "revision-suffix-match" in pair["reasons"]


def test_source_revision_hash_overrides_low_surface_name_similarity():
    entities = [
        _entity(
            "source_a",
            "Source 2601.06002v2",
            type="source",
            page_key="Source_2601-06002v2",
            sources=["raw/Huggingface-Daily-Papers/2601.06002v2.md"],
        ),
        _entity(
            "source_b",
            "2601.06002v2",
            type="source",
            page_key="Source_2601-06002v2-e68c5390",
            sources=["raw/Huggingface-Daily-Papers/2601.06002v2.md"],
        ),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "source_a", "source_b")

    assert pair["decision"] == "merge"
    assert "revision-suffix-match" in pair["reasons"]


def test_source_revision_merge_keeps_curated_unsuffixed_target_when_backlog_body_is_longer():
    entities = [
        _entity(
            "source_curated",
            "paper-river-Multi-Agent-LIFE-Progression",
            type="source",
            page_key="Source_2605-14892v2",
            topic_cluster="Multi-Agent",
            sources=["raw/Huggingface-Daily-Papers/2605.14892v2.md"],
            raw_text="Curated analysis.",
        ),
        _entity(
            "source_backlog",
            "2605.14892v2",
            type="source",
            page_key="Source_2605-14892v2-0dadf455",
            topic_cluster="Raw_Ingest_Backlog",
            categories=["Raw_Ingest_Backlog"],
            sources=["raw/Huggingface-Daily-Papers/2605.14892v2.md"],
            raw_text="Raw preview. " * 1000,
        ),
    ]

    pair = _pair_for(
        analyze_entities(entities, limit=None),
        "source_curated",
        "source_backlog",
    )

    assert pair["decision"] == "merge"
    assert pair["left_entity_id"] == "source_curated"
    assert pair["left_page_key"] == "Source_2605-14892v2"


def test_source_revision_suffix_without_exact_raw_identity_requires_review():
    entities = [
        _entity(
            "source_left",
            "Quarterly Report",
            type="source",
            page_key="Source_Report",
            sources=["raw/reports/quarterly-a.md"],
        ),
        _entity(
            "source_right",
            "Quarterly Report",
            type="source",
            page_key="Source_Report-ab12cd34",
            sources=["raw/reports/quarterly-b.md"],
        ),
    ]

    pair = _pair_for(
        analyze_entities(entities, limit=None),
        "source_left",
        "source_right",
    )

    assert pair["decision"] == "review"
    assert "source-raw-identity-mismatch" in pair["reasons"]


def test_duplicate_component_larger_than_two_requires_review():
    entities = [
        _entity("oracle_a", "Oracle"),
        _entity("oracle_b", "Oracle!"),
        _entity("oracle_c", "Oracle_"),
    ]

    results = analyze_entities(
        entities,
        limit=None,
        versions={
            "Concept_Oracle": "version-a",
            "Concept_Oracle!": "version-b",
            "Concept_Oracle_": "version-c",
        },
    )
    reviews = [item for item in results if item["decision"] == "review"]

    assert len(reviews) == 2
    assert len({item["left_entity_id"] for item in reviews}) == 1
    assert len({item["component_id"] for item in reviews}) == 1
    assert {item["right_version"] for item in reviews} == {"version-b", "version-c"}
    assert all("large-component-review" in item["reasons"] for item in reviews)


def test_candidate_uses_evidence_score_not_confidence():
    results = analyze_entities(
        [_entity("same_a", "Same Name"), _entity("same_b", "Same-Name")],
        limit=None,
    )

    assert results
    assert "evidence_score" in results[0]
    assert "confidence" not in results[0]
