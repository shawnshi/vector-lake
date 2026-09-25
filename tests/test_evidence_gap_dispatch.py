"""The unsupported-claim debt goes to the queue as cohort batches, once, and without queries.

The queue already carried this debt at claim granularity (684 ``evidence-gap`` items, one
``claim_id`` each).  These tests pin the coarser replacement: a cohort is ``(state, prefix|month)``,
one item is one batch of pages, rerunning is idempotent, and the items carry no ``search_queries``
-- ``tool_research`` spends its five-query window on the first pending items, and provenance is not
something external research can supply.
"""

from vector_lake import db_store, governance_store
from vector_lake.evidence_gap_dispatch import (
    SOURCE,
    claim_evidence_queue,
    plan_evidence_gap_batches,
)


def _seed(rows: list[tuple[str, str, str | None, int]]) -> None:
    """``(page_key, evidence_gap, extractor_name, claim_count)`` -> canonical claims."""
    db_store.init_db()
    items = {}
    for page_key, gap, extractor, count in rows:
        for index in range(count):
            claim_id = f"claim_{page_key}_{gap}_{index}"
            claim = {
                "claim_id": claim_id,
                "claim_text": f"{page_key} block {index}",
                "claim_type": "bullet-claim",
                "status": "Active",
                "locator": {"page_key": page_key, "heading": "1. 编译事实", "block_index": index + 1},
                "evidence_ids": [],
                "evidence_gap": gap,
                "created_at": "2026-06-10",
            }
            if extractor:
                claim["extractor_name"] = extractor
            items[claim_id] = claim
    governance_store.save_claims({"items": items})


def _pending() -> list[dict]:
    return governance_store.pending_governance_items()


def test_cohorts_split_by_state_and_prefix_and_batch_by_page_count(isolated_memory):
    _seed(
        [
            ("Concept_Alpha", "no_source", None, 3),
            ("Concept_Beta", "no_source", None, 2),
            ("Product_Gamma", "no_source", "vector_lake.claim_extractor", 1),
            ("Vendor_Delta", "ambiguous_source", None, 2),
        ]
    )

    plan = plan_evidence_gap_batches(group="prefix", batch_pages=2)

    assert plan["total_claims"] == 8
    assert plan["total_pages"] == 4
    assert plan["state_totals"]["no_source"]["claims"] == 6
    assert plan["state_totals"]["no_source"]["pages"] == 3
    assert plan["state_totals"]["no_source"]["legacy_claims"] == 5
    assert plan["state_totals"]["ambiguous_source"]["claims"] == 2

    # Two Concept pages fill one batch of two; the single Product and Vendor pages get one each.
    shape = sorted((b["cohort"], b["batch_index"], b["batch_count"], b["item"]["cohort"]["page_count"]) for b in plan["batches"])
    assert shape == [("Concept", 1, 1, 2), ("Product", 1, 1, 1), ("Vendor", 1, 1, 1)], shape
    concept = next(b for b in plan["batches"] if b["cohort"] == "Concept")
    assert concept["item"]["cohort"]["claim_count"] == 5
    assert concept["item"]["cohort"]["legacy_claim_count"] == 5


def test_the_month_axis_groups_by_the_ingestion_wave(isolated_memory):
    _seed([("Concept_Alpha", "no_source", None, 2), ("Product_Gamma", "no_source", None, 1)])

    plan = plan_evidence_gap_batches(group="month", batch_pages=10)

    assert [b["cohort"] for b in plan["batches"]] == ["2026-06"], plan["batches"]


def test_a_dry_run_enqueues_nothing(isolated_memory):
    _seed([("Concept_Alpha", "no_source", None, 1)])

    report = claim_evidence_queue(dry_run=True)

    assert "[DRY RUN]" in report
    assert _pending() == []


def test_apply_enqueues_one_item_per_batch_and_rerunning_is_idempotent(isolated_memory):
    _seed([("Concept_Alpha", "no_source", None, 2), ("Concept_Beta", "no_source", None, 1)])

    first = claim_evidence_queue(dry_run=False, batch_pages=10)
    items = _pending()
    assert len(items) == 1, items
    item = items[0]
    assert item["type"] == "evidence-gap"
    assert item["source"] == SOURCE
    assert item["status"] == "pending"
    assert item["owner"] == "vector-lake-governance"
    assert item["reason"] == "page_declares_no_source"
    assert item["cohort"]["key"] == "Concept"
    assert item["cohort"]["page_count"] == 2
    assert item["affected_pages"] == ["Concept_Alpha", "Concept_Beta"]
    assert item["affected_page_count"] == 2
    assert "enqueued 1 item(s)" in first

    second = claim_evidence_queue(dry_run=False, batch_pages=10)

    assert len(_pending()) == 1
    assert "already in the queue (skipped): 1" in second
    assert "enqueued 0 item(s)" in second


def test_items_carry_no_research_queries(isolated_memory):
    """``tool_research`` reads the first five pending items' queries; provenance is not research."""
    _seed([("Concept_Alpha", "no_source", None, 1)])

    claim_evidence_queue(dry_run=False)

    assert _pending()[0]["search_queries"] == []


def test_a_changed_page_set_is_reported_as_stale_not_duplicated(isolated_memory):
    _seed([("Concept_Alpha", "no_source", None, 1)])
    claim_evidence_queue(dry_run=False, batch_pages=10)

    # The same cohort now covers a second page, so the batch is the same batch with different
    # contents: it must be reported, not enqueued again under a new id.
    _seed([("Concept_Beta", "no_source", None, 1)])
    report = claim_evidence_queue(dry_run=False, batch_pages=10)

    assert len(_pending()) == 1
    assert "already in the queue (skipped): 1" in report
    assert "1 cover a changed page set" in report


def test_a_corpus_without_gaps_reports_nothing_to_dispatch(isolated_memory):
    _seed([("Concept_Alpha", "", None, 2)])

    report = claim_evidence_queue(dry_run=True)

    assert "No claim carries an evidence gap" in report
