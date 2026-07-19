import hashlib
from pathlib import Path

from vector_lake import governance_metrics, governance_service, governance_store
from vector_lake.merge_analysis import preflight_suggestion


def _mock_row_level_queue(monkeypatch, queue):
    def get_item(item_id):
        return next(
            (item for item in queue["items"] if item.get("item_id") == item_id),
            None,
        )

    def update_item(item_id, updates, expected_statuses=None):
        item = get_item(item_id)
        if item is None:
            return None
        if expected_statuses is not None and item.get("status") not in expected_statuses:
            return None
        item.update(updates)
        return item

    monkeypatch.setattr(governance_store, "get_governance_item", get_item)
    monkeypatch.setattr(governance_store, "update_governance_item", update_item)


def _suggestion(**overrides) -> dict:
    suggestion = {
        "pair_key": "entity_a::entity_b",
        "evidence_score": 100,
        "score": 100,
        "decision": "merge",
        "left_entity_id": "entity_a",
        "left_name": "Alpha",
        "left_page_key": "Source_Alpha",
        "left_version": "version-a",
        "right_entity_id": "entity_b",
        "right_name": "Alpha!",
        "right_page_key": "Source_Alpha-Alt",
        "right_version": "version-b",
        "reasons": ["normalized-name-match"],
        "component_id": "merge_test",
        "component_size": 2,
        "preflight_state": "passed",
        "preflight_errors": [],
    }
    suggestion.update(overrides)
    return suggestion


def _source_content(entity_id: str, title: str, body: str) -> str:
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-07-16T00:00:00Z
sources: []
---
{body}
"""


def test_preflight_runs_semantic_merge_and_schema_validation(tmp_path: Path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "Source_Alpha.md").write_text(
        _source_content("source_alpha", "Alpha", "Primary evidence."),
        encoding="utf-8",
    )
    (wiki_dir / "Source_Alpha-Alt.md").write_text(
        _source_content("source_alpha_alt", "Alpha Alt", "Additional evidence."),
        encoding="utf-8",
    )

    checked = preflight_suggestion(_suggestion(), wiki_dir)

    assert checked["preflight_state"] == "passed"
    assert checked["preflight_errors"] == []


def test_candidate_report_preserves_snapshot_versions_and_pool_size(monkeypatch):
    entities = {
        "entity_a": {
            "entity_id": "entity_a",
            "canonical_name": "Alpha",
            "page_key": "Source_Alpha",
            "type": "source",
            "status": "Active",
        },
        "entity_b": {
            "entity_id": "entity_b",
            "canonical_name": "Alpha!",
            "page_key": "Source_Alpha-ab12cd34",
            "type": "source",
            "status": "Active",
        },
    }
    versions = {"Source_Alpha": "version-a", "Source_Alpha-ab12cd34": "version-b"}
    monkeypatch.setattr(
        governance_store,
        "query_entities",
        lambda filters: {"items": entities},
    )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda page_keys: {
            key: value for key, value in versions.items() if key in page_keys
        },
    )
    monkeypatch.setattr(
        governance_metrics,
        "preflight_suggestion",
        lambda suggestion, wiki_dir: {
            **suggestion,
            "preflight_state": "passed",
            "preflight_errors": [],
        },
    )

    report = governance_metrics.find_merge_candidate_report(limit=1)

    assert report["candidate_pool_size"] == 1
    assert report["returned_count"] == 1
    assert report["suggestions"][0]["left_version"] == "version-a"
    assert report["suggestions"][0]["right_version"] == "version-b"
    assert report["suggestions"][0]["snapshot_state"] == "stable"


def test_candidate_report_balances_decisions_in_preview(monkeypatch):
    decisions = ["merge", "alias", "review", "keep_separate"]
    candidates = [
        {
            **_suggestion(
                pair_key=f"{decision}_{index}",
                left_entity_id=f"{decision}_left_{index}",
                right_entity_id=f"{decision}_right_{index}",
                decision=decision,
                preflight_state="not_run",
            ),
            "decision": decision,
        }
        for decision in decisions
        for index in range(5)
    ]
    monkeypatch.setattr(
        governance_store,
        "query_entities",
        lambda filters: {"items": {}},
    )
    monkeypatch.setattr(
        governance_store,
        "canonical_page_versions",
        lambda page_keys: {},
    )
    monkeypatch.setattr(
        governance_metrics,
        "analyze_entities",
        lambda entities, limit, versions: candidates,
    )

    report = governance_metrics.find_merge_candidate_report(
        limit=8,
        run_preflight=False,
    )

    assert report["decision_counts"] == {
        "merge": 5,
        "alias": 5,
        "review": 5,
        "keep_separate": 5,
    }
    assert report["selected_decision_counts"] == {
        "merge": 2,
        "alias": 2,
        "review": 2,
        "keep_separate": 2,
    }
    assert report["actionable_pool_size"] == 15


def test_queue_only_receives_preflight_passed_merges(
    isolated_memory,
    monkeypatch,
):
    eligible = _suggestion(
        left_projection_hash="target-hash",
        right_projection_hash="source-hash",
    )
    alias = _suggestion(
        pair_key="entity_c::entity_d",
        decision="alias",
        preflight_state="not_applicable",
    )
    blocked = _suggestion(
        pair_key="entity_e::entity_f",
        left_entity_id="entity_e",
        right_entity_id="entity_f",
        preflight_state="blocked",
        preflight_errors=["schema failed"],
    )
    monkeypatch.setattr(
        governance_metrics,
        "find_merge_candidate_report",
        lambda limit, run_preflight, decision: {
            "candidate_pool_size": 5,
            "actionable_pool_size": 3,
            "decision_counts": {
                "merge": 2,
                "alias": 1,
                "review": 0,
                "keep_separate": 2,
            },
            "selected_decision_counts": {
                "merge": 2,
                "alias": 1,
            },
            "returned_count": 3,
            "suggestions": [eligible, alias, blocked],
        },
    )

    result = governance_store.create_merge_suggestions(limit=3, enqueue=True)
    queue = governance_store.load_governance_queue()["items"]

    assert result["created"] == 1
    assert result["eligible_count"] == 1
    assert result["skipped_count"] == 2
    assert len(queue) == 1
    assert queue[0]["pair_key"] == eligible["pair_key"]


def test_resolution_passes_candidate_versions_to_atomic_mutation(
    tmp_path: Path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "Source_Alpha.md").write_text("target", encoding="utf-8")
    (wiki_dir / "Source_Alpha-Alt.md").write_text("source", encoding="utf-8")
    queue = {
        "items": [
            {
                "item_id": "gov_test",
                "type": "merge",
                "status": "pending",
                "merge_candidate": _suggestion(
                    left_projection_hash=hashlib.sha256(b"target").hexdigest(),
                    right_projection_hash=hashlib.sha256(b"source").hexdigest(),
                ),
            }
        ]
    }
    captured = {}

    _mock_row_level_queue(monkeypatch, queue)
    monkeypatch.setattr(governance_store, "get_entity", lambda entity_id: {})
    monkeypatch.setattr(governance_store, "upsert_alias", lambda source, target: None)
    monkeypatch.setattr(governance_service, "get_wiki_dir", lambda: wiki_dir)
    monkeypatch.setattr(
        governance_service,
        "merge_markdown_content",
        lambda left, right, source_key: "merged",
    )
    monkeypatch.setattr(
        governance_service,
        "execute_mutation_batch",
        lambda mutations, canonical_callback, validation_mode: captured.update(
            mutations=mutations,
            validation_mode=validation_mode,
        ),
    )
    monkeypatch.setattr(
        "vector_lake.wiki_utils.get_meta_dir",
        lambda: tmp_path,
    )

    governance_service.resolve_governance_item("gov_test", resolution="merge")

    assert captured["validation_mode"] == "schema"
    assert captured["mutations"] == [
        {
            "filename": "Source_Alpha.md",
            "content": "merged",
            "expected_version": "version-a",
        },
        {
            "filename": "Source_Alpha-Alt.md",
            "is_delete": True,
            "expected_version": "version-b",
        },
    ]


def test_resolution_rejects_legacy_candidate_without_new_contract(
    tmp_path: Path,
    monkeypatch,
):
    queue = {
        "items": [
            {
                "item_id": "gov_legacy",
                "type": "merge",
                "status": "pending",
                "merge_candidate": {
                    "left_entity_id": "entity_a",
                    "right_entity_id": "entity_b",
                    "left_name": "Alpha",
                    "right_name": "Alpha!",
                },
            }
        ]
    }
    _mock_row_level_queue(monkeypatch, queue)

    try:
        governance_service.resolve_governance_item(
            "gov_legacy",
            resolution="merge",
        )
    except RuntimeError as exc:
        assert "regenerated" in str(exc)
    else:
        raise AssertionError("Legacy merge candidate was not rejected.")


def test_resolution_rejects_missing_merge_candidate(monkeypatch):
    queue = {
        "items": [
            {
                "item_id": "gov_missing_candidate",
                "type": "merge",
                "status": "pending",
            }
        ]
    }
    _mock_row_level_queue(monkeypatch, queue)

    try:
        governance_service.resolve_governance_item(
            "gov_missing_candidate",
            resolution="merge",
        )
    except RuntimeError as exc:
        assert "regenerated" in str(exc)
    else:
        raise AssertionError("Missing merge candidate was not rejected.")


def test_resolution_rejects_projection_hash_drift(
    tmp_path: Path,
    monkeypatch,
):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "Source_Alpha.md").write_text("changed target", encoding="utf-8")
    (wiki_dir / "Source_Alpha-Alt.md").write_text("source", encoding="utf-8")
    queue = {
        "items": [
            {
                "item_id": "gov_drift",
                "type": "merge",
                "status": "pending",
                "merge_candidate": _suggestion(
                    left_projection_hash="stale-target-hash",
                    right_projection_hash="stale-source-hash",
                ),
            }
        ]
    }
    _mock_row_level_queue(monkeypatch, queue)
    monkeypatch.setattr(governance_store, "get_entity", lambda entity_id: {})
    monkeypatch.setattr(governance_service, "get_wiki_dir", lambda: wiki_dir)
    monkeypatch.setattr(
        governance_service,
        "execute_mutation_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Mutation must not execute after projection drift.")
        ),
    )

    try:
        governance_service.resolve_governance_item(
            "gov_drift",
            resolution="merge",
        )
    except RuntimeError as exc:
        assert "projection changed" in str(exc).lower()
    else:
        raise AssertionError("Projection drift was not rejected.")
