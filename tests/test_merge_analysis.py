from vector_lake.merge_analysis import (
    FilenameCandidateStats,
    analyze_entities,
    filename_candidate_pairs,
    iter_filename_candidates,
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


def test_filename_candidates_exclude_system_community_pages():
    keys = {f"System_Community-L1-{index:08x}" for index in range(469)}

    assert filename_candidate_pairs(keys) == []


def test_system_prefix_parser_rejects_hyphen_and_nfkc_separator_variants():
    keys = {
        "System-Community",
        "System-_Community",
        "System＿Community",
        "Concept_Community",
    }

    assert filename_candidate_pairs(keys) == []


def test_filename_candidates_do_not_treat_calendar_dates_as_revision_hashes():
    keys = {
        "Source_DHWB-20260302",
        "Source_DHWB-20260308",
        "Source_DHWB-20260315",
    }
    stats = FilenameCandidateStats()

    events = list(iter_filename_candidates(keys, stats=stats))

    assert not [event for event in events if event.eligible]
    assert stats.temporal_series == 1


def test_calendar_shaped_revision_keeps_one_bounded_review_edge():
    stats = FilenameCandidateStats()

    events = list(
        iter_filename_candidates(
            {"Source_Report", "Source_Report-20260302"},
            stats=stats,
        )
    )

    assert len(events) == 1
    assert events[0].category == "ambiguous_revision"
    assert events[0].eligible is True
    assert stats.ambiguous_revision == 1


def test_filename_candidates_keep_valid_source_hash_revision():
    pairs = filename_candidate_pairs(
        {
            "Source_Report-2026-05-09",
            "Source_Report-2026-05-09-a56748c3",
        }
    )

    assert pairs == [
        (
            "Source_Report-2026-05-09",
            "Source_Report-2026-05-09-a56748c3",
            1.0,
        )
    ]


def test_source_revision_canonical_lookup_is_case_insensitive():
    stats = FilenameCandidateStats()

    events = list(
        iter_filename_candidates(
            {"Source_Report", "source_report-ab12cd34"},
            page_types={
                "Source_Report": "source",
                "source_report-ab12cd34": "source",
            },
            stats=stats,
        )
    )

    assert [(event.category, event.eligible) for event in events] == [
        ("hash_revision", True)
    ]
    assert stats.hash_revision == 1


def test_source_hash_revision_accepts_controlled_hyphen_type_separator():
    events = list(
        iter_filename_candidates(
            {
                "Source-Report",
                "Source-Report-ab12cd34",
            },
            page_types={
                "Source-Report": "source",
                "Source-Report-ab12cd34": "source",
            },
        )
    )

    assert [(event.category, event.eligible) for event in events] == [
        ("hash_revision", True)
    ]


def test_source_hash_siblings_remain_recalled_without_canonical_page():
    stats = FilenameCandidateStats()

    events = list(
        iter_filename_candidates(
            {
                "Source_Report-ab12cd34",
                "Source_Report-deadbeef",
            },
            stats=stats,
        )
    )

    assert [(event.category, event.eligible) for event in events] == [
        ("hash_revision", True)
    ]
    assert stats.hash_revision == 1


def test_large_revision_bucket_uses_deterministic_star_edges():
    keys = [
        "Source_Report",
        *(f"Source_Report-a{index:07x}" for index in range(100)),
    ]

    forward = filename_candidate_pairs(keys)
    reverse = filename_candidate_pairs(list(reversed(keys)))

    assert forward == reverse
    assert len(forward) == len(keys) - 1
    assert all("Source_Report" in pair[:2] for pair in forward)


def test_large_exact_bucket_uses_connected_star_edges():
    keys = [f"Concept_{'-' * index}Foo" for index in range(1, 101)]

    pairs = filename_candidate_pairs(keys)
    adjacency = {key: set() for key in keys}
    for left, right, _ratio in pairs:
        adjacency[left].add(right)
        adjacency[right].add(left)
    seen = set()
    stack = [keys[0]]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency[current] - seen)

    assert len(pairs) == len(keys) - 1
    assert seen == set(keys)


def test_temporal_series_is_reported_but_not_actionable():
    stats = FilenameCandidateStats()

    events = list(
        iter_filename_candidates(
            {
                "Source_intelligence-20260301-briefing",
                "Source_intelligence-20260302-briefing",
            },
            stats=stats,
        )
    )

    assert len(events) == 1
    assert events[0].category == "temporal_series"
    assert events[0].eligible is False
    assert stats.temporal_series == 1


def test_numeric_group_boundaries_are_not_collapsed_by_normalization():
    stats = FilenameCandidateStats()

    events = list(
        iter_filename_candidates(
            {
                "Product_Model-X-1.2",
                "Product_Model-X-12",
            },
            stats=stats,
        )
    )

    assert len(events) == 1
    assert events[0].category == "numeric_identity_conflict"
    assert events[0].eligible is False
    assert stats.numeric_identity_conflict == 1


def test_equivalent_compact_and_separated_dates_share_identity_signature():
    pairs = filename_candidate_pairs(
        {
            "Source_DHWB-2026-03-02",
            "Source_DHWB-20260302",
        }
    )

    assert len(pairs) == 1
    assert pairs[0][2] == 1.0


def test_same_raw_identity_overrides_temporal_name_gate():
    left = "Source_Brief-20260301"
    right = "Source_Brief-20260302"
    stats = FilenameCandidateStats()

    events = list(
        iter_filename_candidates(
            {left, right},
            page_sources={
                left: ["raw/brief.md"],
                right: ["MEMORY/raw/brief.md"],
            },
            stats=stats,
        )
    )

    assert len(events) == 1
    assert events[0].category == "raw_source"
    assert events[0].eligible is True
    assert stats.raw_source == 1
    assert stats.temporal_series == 0


def test_large_raw_identity_bucket_uses_deterministic_star_edges():
    page_sources = {
        "Source_Report": ["raw/report.md"],
        **{
            f"Source_Report-a{index:07x}": ["MEMORY/raw/report.md"]
            for index in range(1_000)
        },
    }

    pairs = source_identity_candidate_pairs(page_sources)

    assert len(pairs) == len(page_sources) - 1
    assert all("Source_Report" in pair[:2] for pair in pairs)
    assert pairs == source_identity_candidate_pairs(dict(reversed(page_sources.items())))


def test_large_raw_identity_component_never_builds_a_large_clique(monkeypatch):
    import vector_lake.merge_analysis as merge_analysis

    real_combinations = merge_analysis.combinations
    observed_bucket_sizes = []

    def guarded_combinations(values, size):
        materialized = tuple(values)
        observed_bucket_sizes.append(len(materialized))
        assert len(materialized) <= 8
        return real_combinations(materialized, size)

    monkeypatch.setattr(merge_analysis, "combinations", guarded_combinations)
    entities = [
        _entity(
            f"source_{index}",
            f"Raw report {index}",
            type="source",
            page_key=(
                "Source_Raw-Report"
                if index == 0
                else f"Source_Raw-Report-a{index:07x}"
            ),
            sources=["raw/report.md"],
        )
        for index in range(1_000)
    ]

    results = analyze_entities(entities, limit=None)

    assert len(results) == len(entities) - 1
    assert all(item["component_size"] == len(entities) for item in results)
    assert max(observed_bucket_sizes, default=0) <= 8


def test_non_source_hex_suffix_never_becomes_revision_evidence():
    results = analyze_entities(
        [
            _entity(
                "concept_base",
                "Canonical Foo",
                aliases=["bridge"],
                page_key="Concept_Canonical-Foo",
            ),
            _entity(
                "concept_hash",
                "Canonical Foo deadbeef",
                aliases=["bridge"],
                page_key="Concept_Canonical-Foo-deadbeef",
            ),
        ],
        limit=None,
    )

    pair = _pair_for(results, "concept_base", "concept_hash")
    assert pair["decision"] != "merge"
    assert "revision-suffix-match" not in pair["reasons"]


def test_non_source_hash_suffix_cannot_bypass_exact_canonical_name_gate():
    results = analyze_entities(
        [
            _entity(
                "concept_base",
                "Canonical Foo",
                page_key="Concept_Canonical-Foo",
            ),
            _entity(
                "concept_hash",
                "Canonical Foo",
                page_key="Concept_Canonical-Foo-deadbeef",
            ),
        ],
        limit=None,
    )

    pair = _pair_for(results, "concept_base", "concept_hash")
    assert pair["decision"] == "keep_separate"
    assert "unverified-non-source-hash-suffix" in pair["reasons"]


def test_explicit_type_conflict_does_not_grant_source_revision_signal():
    events = list(
        iter_filename_candidates(
            {"Source_Report", "Source_Report-ab12cd34"},
            page_types={
                "Source_Report": "concept",
                "Source_Report-ab12cd34": "concept",
            },
        )
    )

    assert not any(event.category == "hash_revision" for event in events)


def test_formal_merge_keeps_prefix_and_declared_type_conflict_separate():
    results = analyze_entities(
        [
            _entity(
                "source_prefix_drift",
                "Shared Report",
                type="concept",
                page_key="Source_Shared-Report",
            ),
            _entity(
                "concept_report",
                "Shared Report",
                type="concept",
                page_key="Concept_Shared-Report",
            ),
        ],
        limit=None,
    )

    pair = _pair_for(
        results,
        "source_prefix_drift",
        "concept_report",
    )
    assert pair["decision"] == "keep_separate"
    assert "entity-type-conflict" in pair["reasons"]


def test_legacy_filename_candidate_api_remains_a_deterministic_list():
    keys = ["Concept_A_B", "Concept_AB"]

    pairs = filename_candidate_pairs(keys)

    assert isinstance(pairs, list)
    assert pairs == [("Concept_AB", "Concept_A_B", 1.0)]


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


def test_numeric_suffix_conflict_is_filtered_before_candidate_evaluation():
    entities = [
        _entity("alphafold", "AlphaFold"),
        _entity("alphafold2", "AlphaFold2"),
    ]

    results = analyze_entities(entities, limit=None)

    assert not any(
        {item["left_entity_id"], item["right_entity_id"]}
        == {"alphafold", "alphafold2"}
        for item in results
    )


def test_exact_canonical_name_does_not_merge_different_dated_page_keys():
    entities = [
        _entity(
            "brief_day_1",
            "Daily Brief",
            type="event",
            page_key="Event_Brief-20260301",
        ),
        _entity(
            "brief_day_2",
            "Daily Brief",
            type="event",
            page_key="Event_Brief-20260302",
        ),
    ]

    assert analyze_entities(entities, limit=None) == []


def test_exact_canonical_name_does_not_merge_different_product_versions():
    entities = [
        _entity(
            "product_v1",
            "AlphaFold Enterprise",
            type="product",
            page_key="Product_AlphaFold-Enterprise-1",
        ),
        _entity(
            "product_v2",
            "AlphaFold Enterprise",
            type="product",
            page_key="Product_AlphaFold-Enterprise-2",
        ),
    ]

    assert analyze_entities(entities, limit=None) == []


def test_exact_canonical_name_does_not_merge_decimal_with_compact_version():
    entities = [
        _entity(
            "product_decimal",
            "Model X",
            type="product",
            page_key="Product_Model-X-1.2",
        ),
        _entity(
            "product_compact",
            "Model X",
            type="product",
            page_key="Product_Model-X-12",
        ),
    ]

    assert analyze_entities(entities, limit=None) == []


def test_system_page_key_is_excluded_even_when_declared_as_concept():
    results = analyze_entities(
        [
            _entity(
                "system_drift",
                "Shared Community",
                type="concept",
                page_key="System_Community-L1-deadbeef",
            ),
            _entity(
                "concept_peer",
                "Shared Community",
                type="concept",
                page_key="Concept_Shared-Community",
            ),
        ],
        limit=None,
    )

    assert results == []


def test_hyphenated_system_page_key_is_excluded_from_formal_merge():
    results = analyze_entities(
        [
            _entity(
                "system_drift",
                "Shared Community",
                type="concept",
                page_key="System-Community",
            ),
            _entity(
                "concept_peer",
                "Shared Community",
                type="concept",
                page_key="Concept_Community",
            ),
        ],
        limit=None,
    )

    assert results == []


def test_large_mixed_type_exact_bucket_preserves_same_type_merge_component():
    vendor_ids = {f"vendor_{index}" for index in range(10)}
    entities = [
        *(
            _entity(
                entity_id,
                "Shared Name",
                type="vendor",
                page_key=f"Vendor_{'-' * (index + 1)}Shared-Name",
            )
            for index, entity_id in enumerate(sorted(vendor_ids))
        ),
        _entity(
            "concept_shared",
            "Shared Name",
            type="concept",
            page_key="Concept_Shared-Name",
        ),
    ]

    results = analyze_entities(entities, limit=None)
    vendor_component_edges = [
        item
        for item in results
        if item.get("component_size") == len(vendor_ids)
        and item["left_entity_id"] in vendor_ids
        and item["right_entity_id"] in vendor_ids
    ]

    assert len(vendor_component_edges) == len(vendor_ids) - 1
    assert len({item["component_id"] for item in vendor_component_edges}) == 1
    cross_type_reviews = [
        item
        for item in results
        if "concept_shared" in {item["left_entity_id"], item["right_entity_id"]}
        and item["decision"] == "review"
    ]
    assert len(cross_type_reviews) == 1
    assert min(vendor_ids) in {
        cross_type_reviews[0]["left_entity_id"],
        cross_type_reviews[0]["right_entity_id"],
    }


def test_large_exact_bucket_preserves_each_numeric_identity_subgroup():
    entities = []
    expected_pairs = set()
    for version in range(20):
        left_id = f"a{version:02d}"
        right_id = f"b{version:02d}"
        expected_pairs.add(frozenset((left_id, right_id)))
        entities.extend(
            [
                _entity(
                    left_id,
                    "Shared Product",
                    type="product",
                    page_key=f"Product_Shared-v{version}-a",
                ),
                _entity(
                    right_id,
                    "Shared Product",
                    type="product",
                    page_key=f"Product_Shared-v{version}-b",
                ),
            ]
        )

    results = analyze_entities(entities, limit=None)
    merge_pairs = {
        frozenset((item["left_entity_id"], item["right_entity_id"]))
        for item in results
        if item["decision"] == "merge"
    }

    assert merge_pairs == expected_pairs
    assert all(item["component_size"] == 2 for item in results)


def test_cross_type_review_is_remapped_to_component_target():
    entities = [
        _entity(
            "vendor_a",
            "Model",
            type="vendor",
            page_key="Vendor_Model",
            sources=["Source_Model_Catalog"],
            raw_text="A" * 120,
        ),
        _entity(
            "vendor_b",
            "Models",
            type="vendor",
            page_key="Vendor_Models",
            sources=["Source_Model_Catalog"],
            raw_text="Short body used only to preserve plural evidence.",
        ),
        _entity(
            "concept_d",
            "Models",
            type="concept",
            page_key="Concept_Models",
        ),
    ]

    results = analyze_entities(entities, limit=None)
    review = _pair_for(results, "vendor_a", "concept_d")

    assert review["decision"] == "review"
    assert "component-endpoint-remap" in review["reasons"]
    assert not any(
        item["decision"] != "merge"
        and "concept_d" in {item["left_entity_id"], item["right_entity_id"]}
        and "vendor_b" in {item["left_entity_id"], item["right_entity_id"]}
        for item in results
    )


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


def test_source_hash_siblings_without_canonical_page_require_review_without_raw():
    entities = [
        _entity(
            "source_a",
            "Report A",
            type="source",
            page_key="Source_Report-ab12cd34",
        ),
        _entity(
            "source_b",
            "Report B",
            type="source",
            page_key="Source_Report-deadbeef",
        ),
    ]

    pair = _pair_for(analyze_entities(entities, limit=None), "source_a", "source_b")

    assert pair["decision"] == "review"
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
