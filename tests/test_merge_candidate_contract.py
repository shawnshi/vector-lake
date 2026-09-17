"""Regression tests for the merge-candidate -> resolver contract.

Three defects are pinned here:

* ``left_name``/``right_name`` used to carry ``canonical_name``, while
  ``governance_service.resolve_governance_item`` resolves each side as
  ``<prefix><name>.md`` / ``<name>.md``.  On the live graph 266 of 361 candidates
  had at least one side that resolved to no file, so the pair could never be
  merged.  They must carry the on-disk page key.
* Survivor selection used to fall out of candidate-pair set-iteration order, so
  which page survived was arbitrary.  It must be deterministic and independent of
  enumeration order (older entity wins).
* A shared name claimed by three or more distinct entities cannot be resolved by
  merging a pair.  Those candidates stay visible but must not be enqueued by
  default.
"""
from vector_lake import db_store, governance_store
from vector_lake.governance_metrics import find_merge_candidates
from vector_lake.wiki_utils import get_wiki_dir


def _entity(index, canonical_name, *, page_key, created_at, aliases=()):
    entity_id = f"entity_{index:032x}"
    return entity_id, {
        "entity_id": entity_id,
        "page_key": page_key,
        "canonical_name": canonical_name,
        "type": "vendor",
        "domain": "Medical_IT",
        "topic_cluster": "Cloud_Provider",
        "status": "Active",
        "aliases": list(aliases),
        "created_at": created_at,
    }


def _seed(items):
    db_store.init_db()
    governance_store.save_entities(
        {"items": dict(items), "updated_at": "2026-01-01T00:00:00+00:00"}
    )


def _pair(older_first=True):
    """Two entities that share a name but whose titles are not page keys."""
    older = _entity(
        1,
        "Source_intelligence_20260302_briefing",
        page_key="Source_intelligence-20260302-briefing",
        created_at="2026-03-02T00:00:00+00:00",
    )
    newer = _entity(
        2,
        "Intelligence Hub Briefing [2026-03-02]",
        page_key="Source_intelligence-20260302-briefing-2",
        created_at="2026-03-03T00:00:00+00:00",
        aliases=["Source_intelligence_20260302_briefing"],
    )
    return (older, newer) if older_first else (newer, older)


def test_emitted_names_resolve_to_existing_wiki_pages(isolated_memory):
    older, newer = _pair()
    _seed([older, newer])
    for _, entity in (older, newer):
        (get_wiki_dir() / f"{entity['page_key']}.md").write_text(
            f"# {entity['canonical_name']}\n", encoding="utf-8"
        )

    candidates = find_merge_candidates(limit=100)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["left_name"] == "Source_intelligence-20260302-briefing"
    assert candidate["right_name"] == "Source_intelligence-20260302-briefing-2"
    # The title form is what used to be emitted, and it names no file.
    assert candidate["left_canonical_name"] == "Source_intelligence_20260302_briefing"
    for side in ("left", "right"):
        assert (get_wiki_dir() / f"{candidate[f'{side}_name']}.md").exists()
    assert not (get_wiki_dir() / f"{candidate['left_canonical_name']}.md").exists()


def test_survivor_is_the_older_entity_regardless_of_enumeration_order(isolated_memory):
    older_first = _pair(older_first=True)
    _seed([older_first[0], older_first[1]])
    first = find_merge_candidates(limit=100)
    assert len(first) == 1
    assert first[0]["left_entity_id"] == older_first[0][0]
    assert "direction:older-entity-survives" in first[0]["reasons"]

    reversed_order = _pair(older_first=False)
    _seed([reversed_order[0], reversed_order[1]])
    second = find_merge_candidates(limit=100)
    assert len(second) == 1
    assert second[0]["left_entity_id"] == first[0]["left_entity_id"]
    assert second[0]["right_entity_id"] == first[0]["right_entity_id"]


def test_ambiguous_name_is_flagged_and_skipped_on_enqueue(isolated_memory):
    shared = "四层壳模型"
    _seed([
        _entity(1, "Four-Layer Shell Architecture", page_key="Concept_Four-Layer",
                created_at="2026-01-01T00:00:00+00:00", aliases=[shared]),
        _entity(2, shared, page_key="Concept_四层壳模型-A",
                created_at="2026-01-02T00:00:00+00:00"),
        _entity(3, shared, page_key="Concept_四层壳模型-B",
                created_at="2026-01-03T00:00:00+00:00"),
    ])

    candidates = find_merge_candidates(limit=100)
    assert candidates
    assert all(candidate["hazards"] for candidate in candidates)
    assert all("ambiguous-name" in hazard
               for candidate in candidates for hazard in candidate["hazards"])

    preview = governance_store.create_merge_suggestions(limit=100, enqueue=False)
    assert preview["created"] == 0
    assert preview["suggestions"] == []
    assert preview["skipped_hazardous"] == len(candidates)
    assert len(preview["hazardous_pairs"]) == len(candidates)

    forced = governance_store.create_merge_suggestions(
        limit=100, enqueue=False, include_hazardous=True
    )
    assert len(forced["suggestions"]) == len(candidates)
    assert forced["skipped_hazardous"] == 0


def test_unambiguous_pair_is_not_skipped(isolated_memory):
    older, newer = _pair()
    _seed([older, newer])

    preview = governance_store.create_merge_suggestions(limit=100, enqueue=False)

    assert len(preview["suggestions"]) == 1
    assert preview["skipped_hazardous"] == 0
    assert preview["suggestions"][0]["hazards"] == []
    assert preview["hazardous_pairs"] == []
