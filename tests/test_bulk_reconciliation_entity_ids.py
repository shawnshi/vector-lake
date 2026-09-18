"""The merge candidate must name entities that exist.

``bulk_reconcile`` used to write ``f"entity_{page_key}"`` into
``merge_candidate.left_entity_id`` / ``right_entity_id``.  No row carries such an id,
so ``resolve_governance_item`` registered an alias between two invented strings --
the merge's own bookkeeping then pointed at nothing, and the cycle guard that walks
``get_alias`` could not see the real chain.

The entry guard covers the queue *shape*, so the regression shows up as what lands in
the queue, not as an exception: a candidate with an unresolvable id is accepted and
only fails much later, silently.
"""
from vector_lake import governance_store
from vector_lake.tool_bulk_reconciliation import bulk_reconcile
from vector_lake.wiki_utils import get_wiki_dir


def _write_page(page_key: str) -> None:
    path = get_wiki_dir() / f"{page_key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: test_{page_key}\n---\n# {page_key}\n", encoding="utf-8")


def _register(rows: dict) -> None:
    """Persist entity rows for ``{page_key: entity_id}`` in one call.

    ``save_entities`` replaces the whole table, so a per-page call would delete the
    row written by the call before it.
    """
    governance_store.initialize_meta_store()
    governance_store.save_entities({
        "items": {
            entity_id: {
                "entity_id": entity_id,
                "page_key": page_key,
                "canonical_name": page_key,
                "title": page_key,
                "type": "concept",
                "status": "Active",
                "ttl": 1825,
                "aliases": [],
            }
            for page_key, entity_id in rows.items()
        }
    })


def _queued_merges() -> list[dict]:
    queue = governance_store.load_governance_queue()
    return [item for item in queue.get("items", []) if item.get("type") == "merge"]


def test_merge_candidate_carries_the_registered_entity_ids(isolated_memory):
    _write_page("Concept_Alpha")
    _write_page("Concept_Beta")
    _register({"Concept_Alpha": "entity_real_alpha", "Concept_Beta": "entity_real_beta"})

    result = bulk_reconcile(
        [{"source_entity": "Concept_Alpha", "target_entity": "Concept_Beta"}],
        dry_run=False,
    )
    assert "Enqueued 1" in result, result

    candidate = _queued_merges()[0]["merge_candidate"]
    # The target survives as the left side; the source is the consumed right side.
    assert candidate["left_entity_id"] == "entity_real_beta"
    assert candidate["right_entity_id"] == "entity_real_alpha"


def test_merge_is_refused_when_a_page_has_no_entity_row(isolated_memory):
    _write_page("Concept_Alpha")
    _write_page("Concept_Ghost")
    _register({"Concept_Alpha": "entity_real_alpha"})

    result = bulk_reconcile(
        [{"source_entity": "Concept_Alpha", "target_entity": "Concept_Ghost"}],
        dry_run=False,
    )

    assert "No entity row" in result, result
    assert "Concept_Ghost" in result
    # Nothing is queued: a candidate whose ids cannot be resolved is worse than no
    # candidate, because it resolves "successfully" into a broken alias row.
    assert _queued_merges() == []


def test_dry_run_also_refuses_an_unregistered_page(isolated_memory):
    _write_page("Concept_Alpha")
    _write_page("Concept_Ghost")
    _register({"Concept_Alpha": "entity_real_alpha"})

    result = bulk_reconcile(
        [{"source_entity": "Concept_Alpha", "target_entity": "Concept_Ghost"}],
        dry_run=True,
    )

    assert "No entity row" in result, result
    assert _queued_merges() == []


def test_dry_run_reports_how_many_rows_it_resolved(isolated_memory):
    _write_page("Concept_Alpha")
    _write_page("Concept_Beta")
    _register({"Concept_Alpha": "entity_real_alpha", "Concept_Beta": "entity_real_beta"})

    result = bulk_reconcile(
        [{"source_entity": "Concept_Alpha", "target_entity": "Concept_Beta"}]
    )

    assert "[DRY RUN]" in result
    assert "2 registered entity row(s)" in result, result
