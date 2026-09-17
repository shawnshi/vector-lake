"""The ambiguity guard must live at the resolution entry point, not only in the
candidate generator.

``find_merge_candidates``/``create_merge_suggestions`` filter ambiguous pairs on
the way into the queue, but ``bulk_reconciliation`` builds its own
``merge_candidate`` and never consults the generator, so a queue-only guard can be
bypassed.  These tests pin the entry guard and the end-to-end merge it protects.
"""
import pytest

from vector_lake import db_store, governance_store, governance_service
from vector_lake.schema_validator import validate_schema
from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter

ITEM_ID = "gov_test_ambiguous_merge"
LEFT_ID = "entity_left"
RIGHT_ID = "entity_right"

_PAGE = """---
id: {entity_id}
title: {title}
type: product
domain: Medical_IT
topic_cluster: General
status: Active
epistemic-status: seed
ttl: 1095
categories:
- System_Architecture
tags: []
created: '2026-01-01'
updated: '2026-01-02'
sources: []
aliases: []
strategic_scope: core
evidence_tier: primary
---
## 1. 编译事实 (Compiled Truth - READ MODEL)

{title} 是产品主体。 (Last Reshaped: 2026-01-01)

---

## 2. 证据时间线 (Timeline - EVENT STORE)

- [2026-01-01] [Release] {title} 发布。 (Source: [[Source_S]])
"""


def _entity(entity_id, canonical_name, page_key, aliases=()):
    return entity_id, {
        "entity_id": entity_id,
        "canonical_name": canonical_name,
        "page_key": page_key,
        "type": "product",
        "domain": "Medical_IT",
        "status": "Active",
        "aliases": list(aliases),
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _seed():
    """Two pages whose shared name a third live entity also claims."""
    db_store.init_db()
    (get_wiki_dir().parent / "purpose.md").write_text(
        """---
purpose_version: "12.0"
intent_keywords: [test]
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
  derived: Derived operational evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
""",
        encoding="utf-8",
    )
    governance_store.save_entities({
        "items": dict([
            _entity(LEFT_ID, "Acme HIS", "Product_Alpha"),
            _entity(RIGHT_ID, "Acme-HIS", "Product_Beta", aliases=["Acme HIS"]),
            _entity("entity_third", "Acme HIS", "Product_Gamma"),
        ]),
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    wiki = get_wiki_dir()
    for name in ("Product_Alpha", "Product_Beta", "Product_Gamma"):
        (wiki / f"{name}.md").write_text(
            _PAGE.format(entity_id=name, title=name), encoding="utf-8")
    governance_store.save_governance_queue({"items": [{
        "item_id": ITEM_ID,
        "type": "merge",
        "title": "Merge candidate: Product_Alpha <> Product_Beta",
        "description": "alias-overlap:Acme HIS",
        "created_at": "2026-01-03T00:00:00+00:00",
        "status": "pending",
        "source": "test",
        "pair_key": f"{LEFT_ID}::{RIGHT_ID}",
        "affected_ids": [LEFT_ID, RIGHT_ID],
        "search_queries": ["Acme HIS"],
        "affected_pages": ["Product_Alpha.md", "Product_Beta.md"],
        "merge_candidate": {
            "left_entity_id": LEFT_ID,
            "left_name": "Product_Alpha",
            "right_entity_id": RIGHT_ID,
            "right_name": "Product_Beta",
        },
    }], "updated_at": "2026-01-03T00:00:00+00:00"})
    return wiki


def _queue_item():
    for item in governance_store.load_governance_queue()["items"]:
        if item.get("item_id") == ITEM_ID:
            return item
    return None


def test_hazard_is_computed_for_the_pair(isolated_memory):
    _seed()

    hazards = governance_metrics_hazards()

    assert len(hazards) == 1
    assert "ambiguous-name:Acme HIS claimed by 3 distinct entities" in hazards[0]


def governance_metrics_hazards():
    from vector_lake import governance_metrics
    return governance_metrics.ambiguous_name_hazards(["Product_Alpha", "Product_Beta"])


def test_resolution_refuses_ambiguous_names_without_mutating(isolated_memory):
    wiki = _seed()

    with pytest.raises(RuntimeError, match="ambiguous name claim"):
        governance_service.resolve_governance_item(ITEM_ID, "merge")

    assert (wiki / "Product_Alpha.md").exists()
    assert (wiki / "Product_Beta.md").exists()
    assert _queue_item()["status"] == "pending"


def test_skip_is_not_blocked_by_the_guard(isolated_memory):
    _seed()

    item = governance_service.resolve_governance_item(ITEM_ID, "skip")

    assert item["status"] == "resolved"
    assert item["resolution"] == "skip"


def test_forced_resolution_succeeds_and_merged_page_is_schema_valid(isolated_memory):
    wiki = _seed()

    item = governance_service.resolve_governance_item(
        ITEM_ID, "merge", change_manifest={"allow_ambiguous_names": True})

    assert item["status"] == "resolved"
    assert item["resolution"] == "merge"
    assert not (wiki / "Product_Beta.md").exists()

    frontmatter, body = split_frontmatter((wiki / "Product_Alpha.md").read_text(encoding="utf-8"))
    validate_schema(frontmatter, body, "Product_Alpha.md")
    assert "Product_Alpha 发布" in body
    assert "Product_Beta 发布" in body
    assert governance_store.get_alias(RIGHT_ID) == LEFT_ID


def test_queue_row_is_written_inside_the_mutation_transaction(isolated_memory, monkeypatch):
    """VL-MERGE-RESOLVE-003: item state and canonical state must commit together."""
    _seed()
    observed = {}
    real_save = governance_store.save_governance_queue

    def spy(data):
        observed["in_transaction"] = bool(getattr(db_store._LOCAL, "in_transaction", False))
        return real_save(data)

    monkeypatch.setattr(governance_store, "save_governance_queue", spy)

    governance_service.resolve_governance_item(
        ITEM_ID, "merge", change_manifest={"allow_ambiguous_names": True})

    assert observed.get("in_transaction") is True


def test_failure_inside_the_commit_callback_leaves_no_trace(isolated_memory, monkeypatch):
    wiki = _seed()
    before = (wiki / "Product_Alpha.md").read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected failure inside the commit callback")

    # First statement of the callback, i.e. after the canonical writes are staged
    # but before the transaction commits.
    monkeypatch.setattr(governance_store, "upsert_alias", boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        governance_service.resolve_governance_item(
            ITEM_ID, "merge", change_manifest={"allow_ambiguous_names": True})

    assert (wiki / "Product_Alpha.md").read_text(encoding="utf-8") == before
    assert (wiki / "Product_Beta.md").exists()
    assert _queue_item()["status"] == "pending"
