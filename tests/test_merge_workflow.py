import hashlib
import json
from pathlib import Path

import pytest

from vector_lake import (
    db_store,
    governance_metrics,
    governance_service,
    governance_store,
    mutation_coordinator,
)
from vector_lake.merge_analysis import preflight_suggestion
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.wiki_utils import split_frontmatter
from vector_lake.watchdog_app import process_mutation_outbox_batch
from tests.test_mutation_coordinator import _write_purpose_contract


def test_find_md_file_uses_safe_page_key_when_title_contains_a_separator(tmp_path):
    expected = tmp_path / "Source_Safe-Key.MD"
    expected.write_text("# Safe\n", encoding="utf-8")

    assert (
        governance_service._find_md_file(
            tmp_path.resolve(),
            "Source_Safe-Key",
            "Unsafe / Display Title",
        )
        == expected.resolve()
    )


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
        if (
            expected_statuses is not None
            and item.get("status") not in expected_statuses
        ):
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


def _source_content(
    entity_id: str,
    title: str,
    body: str,
    sources: str = "[]",
) -> str:
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-07-16T00:00:00Z
sources: {sources}
strategic_scope: core
evidence_tier: primary
---
{body}
"""


def _system_content(entity_id: str, title: str, body: str) -> str:
    return f"""---
id: {entity_id}
title: {title}
type: system
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-08-09T00:00:00Z
sources: []
strategic_scope: core
---
{body}
"""


def test_preflight_runs_semantic_merge_and_schema_validation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_path = tmp_path / "raw" / "alpha.md"
    raw_path.parent.mkdir()
    raw_path.write_text("physical source", encoding="utf-8")
    (wiki_dir / "Source_Alpha.md").write_text(
        _source_content(
            "source_alpha",
            "Alpha",
            "Primary evidence.",
            "[raw/alpha.md]",
        ),
        encoding="utf-8",
    )
    (wiki_dir / "Source_Alpha-Alt.MD").write_text(
        _source_content(
            "source_alpha_alt",
            "Alpha Alt",
            "Additional evidence.",
            "[raw/alpha.md]",
        ),
        encoding="utf-8",
    )

    checked = preflight_suggestion(_suggestion(), wiki_dir)

    assert checked["preflight_state"] == "passed"
    assert checked["preflight_errors"] == []
    assert checked["source_identity"] == "raw/alpha.md"
    assert len(checked["source_artifact_hash"]) == 64
    assert checked["backlink_manifest"] == []


def test_source_preflight_blocks_different_raw_identities(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "alpha.md").write_text("alpha", encoding="utf-8")
    (raw_dir / "beta.md").write_text("beta", encoding="utf-8")
    (wiki_dir / "Source_Alpha.md").write_text(
        _source_content("source_alpha", "Alpha", "Primary.", "[raw/alpha.md]"),
        encoding="utf-8",
    )
    (wiki_dir / "Source_Alpha-Alt.md").write_text(
        _source_content("source_alpha_alt", "Alpha", "Other.", "[raw/beta.md]"),
        encoding="utf-8",
    )

    checked = preflight_suggestion(_suggestion(), wiki_dir)

    assert checked["preflight_state"] == "blocked"
    assert (
        "exactly one approved canonical raw identity" in checked["preflight_errors"][0]
    )


def test_source_metadata_conflict_policy_is_explicit_in_merge_preflight(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "alpha.md").write_text("alpha", encoding="utf-8")
    target = _source_content(
        "source_alpha",
        "Alpha",
        "Primary.",
        "[raw/alpha.md]",
    ).replace(
        "domain: General\n",
        "domain: Healthcare\ntopic_cluster: Hospital_IT\ntags: [target-one, target-two]\n",
    )
    source = _source_content(
        "source_alpha_alt",
        "Alpha Alt",
        "Other.",
        "[raw/alpha.md]",
    ).replace(
        "domain: General\n",
        (
            "domain: Artificial_Intelligence\n"
            "topic_cluster: Agentic_AI\n"
            "tags: [source-one, source-two, source-three]\n"
        ),
    )
    (wiki_dir / "Source_Alpha.md").write_text(target, encoding="utf-8")
    (wiki_dir / "Source_Alpha-Alt.md").write_text(source, encoding="utf-8")

    blocked = preflight_suggestion(_suggestion(), wiki_dir)
    checked = preflight_suggestion(
        _suggestion(source_metadata_conflict_policy="preserve_target"),
        wiki_dir,
    )

    assert blocked["preflight_state"] == "blocked"
    assert "Conflicting Source scalar field" in blocked["preflight_errors"][0]
    assert checked["preflight_state"] == "passed", checked["preflight_errors"]
    assert checked["source_metadata_conflict_policy"] == "preserve_target"


def test_source_preflight_approval_cannot_mask_disjoint_raw_identities(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "alpha.md").write_text("alpha", encoding="utf-8")
    (raw_dir / "beta.md").write_text("beta", encoding="utf-8")
    (wiki_dir / "Source_Alpha.md").write_text(
        _source_content("source_alpha", "Alpha", "Primary.", "[raw/alpha.md]"),
        encoding="utf-8",
    )
    (wiki_dir / "Source_Alpha-Alt.md").write_text(
        _source_content("source_alpha_alt", "Alpha", "Other.", "[raw/beta.md]"),
        encoding="utf-8",
    )

    checked = preflight_suggestion(
        _suggestion(approved_source_identity="raw/alpha.md"),
        wiki_dir,
    )

    assert checked["preflight_state"] == "blocked"
    assert "one canonical raw identity" in checked["preflight_errors"][0]


def test_source_preflight_blocks_an_extra_raw_identity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "alpha.md").write_text("alpha", encoding="utf-8")
    (raw_dir / "beta.md").write_text("beta", encoding="utf-8")
    (wiki_dir / "Source_Alpha.md").write_text(
        _source_content(
            "source_alpha",
            "Alpha",
            "Primary.",
            "[raw/alpha.md, raw/beta.md]",
        ),
        encoding="utf-8",
    )
    (wiki_dir / "Source_Alpha-Alt.md").write_text(
        _source_content("source_alpha_alt", "Alpha", "Other.", "[raw/alpha.md]"),
        encoding="utf-8",
    )

    checked = preflight_suggestion(_suggestion(), wiki_dir)

    assert checked["preflight_state"] == "blocked"
    assert "one canonical raw identity" in checked["preflight_errors"][0]


def test_source_preflight_blocks_direct_backlinks_to_duplicate(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_path = tmp_path / "raw" / "alpha.md"
    raw_path.parent.mkdir()
    raw_path.write_text("physical source", encoding="utf-8")
    for filename, entity_id, title in (
        ("Source_Alpha.md", "source_alpha", "Alpha"),
        ("Source_Alpha-Alt.md", "source_alpha_alt", "Alpha Alt"),
    ):
        (wiki_dir / filename).write_text(
            _source_content(entity_id, title, "Evidence.", "[raw/alpha.md]"),
            encoding="utf-8",
        )
    (wiki_dir / "Concept_Backlink.md").write_text(
        "---\nid: concept_backlink\ntitle: Backlink\ntype: concept\ndomain: General\n"
        "status: Active\nepistemic-status: seed\ncategories: [Uncategorized]\n"
        "sources: [raw/alpha.md]\n---\n[[Source_Alpha-Alt]]\n",
        encoding="utf-8",
    )

    checked = preflight_suggestion(_suggestion(), wiki_dir)

    assert checked["preflight_state"] == "blocked"
    assert checked["backlink_manifest"][0]["page_key"] == "Concept_Backlink"
    assert "direct backlink" in checked["preflight_errors"][0]


def test_source_preflight_blocks_case_and_unicode_equivalent_backlink(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_path = tmp_path / "raw" / "cafe.md"
    raw_path.parent.mkdir()
    raw_path.write_text("physical source", encoding="utf-8")
    for filename, entity_id, title in (
        ("Source_Café.md", "source_cafe", "Café"),
        ("Source_Café-Alt.md", "source_cafe_alt", "Café Alt"),
    ):
        (wiki_dir / filename).write_text(
            _source_content(entity_id, title, "Evidence.", "[raw/cafe.md]"),
            encoding="utf-8",
        )
    (wiki_dir / "Concept_Backlink.md").write_text(
        "---\nid: concept_backlink\ntitle: Backlink\ntype: concept\ndomain: General\n"
        "status: Active\nepistemic-status: seed\ncategories: [Uncategorized]\n"
        "sources: [raw/cafe.md]\n---\n[[source_cafe\u0301-alt.MD]]\n",
        encoding="utf-8",
    )

    checked = preflight_suggestion(
        _suggestion(
            left_page_key="Source_Café",
            right_page_key="Source_Café-Alt",
        ),
        wiki_dir,
    )

    assert checked["preflight_state"] == "blocked"
    assert checked["backlink_manifest"][0]["page_key"] == "Concept_Backlink"
    assert "direct backlink" in checked["preflight_errors"][0]


def test_source_metadata_merge_preserves_legacy_hash_during_verified_upgrade():
    merged = governance_service._merge_durable_source_record(
        {
            "source_id": "source_alpha",
            "raw_ref": "raw/alpha.md",
            "content_hash": "a" * 64,
            "integrity_status": "verified",
        },
        {
            "source_id": "source_alpha",
            "raw_ref": "raw/alpha.md",
            "content_hash": "legacy-hash",
            "integrity_status": "unverified",
        },
        "test source",
    )

    assert merged["content_hash"] == "a" * 64
    assert merged["legacy_content_hash"] == "legacy-hash"
    assert merged["integrity_status"] == "verified"


def test_source_metadata_merge_rejects_conflicting_verified_hashes():
    with pytest.raises(RuntimeError, match="Conflicting verified content_hash"):
        governance_service._merge_durable_source_record(
            {
                "source_id": "source_alpha",
                "raw_ref": "raw/alpha.md",
                "content_hash": "a" * 64,
                "integrity_status": "verified",
            },
            {
                "source_id": "source_alpha",
                "raw_ref": "raw/alpha.md",
                "content_hash": "b" * 64,
                "integrity_status": "verified",
            },
            "test source",
        )


def test_source_merge_converges_provenance_projection_journal_and_replay(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "alpha.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("physical source", encoding="utf-8")
    target_content = _source_content(
        "source_alpha",
        "Curated Alpha",
        "Curated evidence.",
        "[raw/alpha.md]",
    ).replace("domain: General\n", "domain: General\ntopic_cluster: Research\n")
    duplicate_content = _source_content(
        "source_alpha_backlog",
        "Alpha",
        "historical awaiting_subagent backlog\n\nRaw Preview MUST NOT SURVIVE.",
        "[raw/alpha.md]",
    ).replace(
        "domain: General\n",
        "domain: General\ntopic_cluster: Raw_Ingest_Backlog\n",
    )
    execute_mutation_plan("Source_Alpha.md", content=target_content)
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1
    execute_mutation_plan("Source_Alpha-ab12cd34.md", content=duplicate_content)
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1

    conn = db_store.get_connection()
    source_row = conn.execute(
        "SELECT source_id, data_json FROM sources "
        "WHERE json_extract(data_json, '$.raw_ref') = 'raw/alpha.md'"
    ).fetchone()
    source_before = json.loads(source_row["data_json"])
    source_before.update(
        {
            "legacy_content_hash": "legacy-reviewed-hash",
            "retention_policy": "retain-7-years",
            "legal_hold": True,
            "reviewed_provenance": {
                "reviewer": "human-review-board",
                "decision": "accepted",
            },
        }
    )
    artifact_row = conn.execute(
        "SELECT artifact_id, data_json FROM source_artifacts WHERE source_id = ?",
        (source_row["source_id"],),
    ).fetchone()
    artifact_before = json.loads(artifact_row["data_json"])
    artifact_before.update(
        {
            "retention_policy": "retain-7-years",
            "legal_hold": True,
            "reviewed_provenance": {
                "reviewer": "human-review-board",
                "decision": "accepted",
            },
        }
    )
    with db_store.transaction():
        conn.execute(
            "UPDATE sources SET data_json = ? WHERE source_id = ?",
            (json.dumps(source_before, ensure_ascii=False), source_row["source_id"]),
        )
        conn.execute(
            "UPDATE source_artifacts SET data_json = ? WHERE artifact_id = ?",
            (
                json.dumps(artifact_before, ensure_ascii=False),
                artifact_row["artifact_id"],
            ),
        )
        conn.execute(
            "INSERT INTO sources (source_id, data_json, updated_at) VALUES (?, ?, ?)",
            (
                "source_legacy_alpha",
                json.dumps(
                    {
                        "source_id": "source_legacy_alpha",
                        "raw_ref": "raw/alpha.md",
                        "canonical_source_page": "Source_Alpha-ab12cd34.md",
                        "content_hash": "legacy-row-hash",
                        "integrity_status": "unverified",
                        "retention_policy": "legacy-row-retention",
                        "reviewed_provenance": {"reviewer": "legacy-reviewer"},
                    },
                    ensure_ascii=False,
                ),
                "2026-01-01T00:00:00+00:00",
            ),
        )

    entities = governance_store.query_entities({"type": "source"})["items"].values()
    by_page = {item["page_key"]: item for item in entities}
    versions = governance_store.canonical_page_versions(
        {"Source_Alpha", "Source_Alpha-ab12cd34"}
    )
    candidate = preflight_suggestion(
        {
            "pair_key": "::".join(
                sorted(
                    (
                        by_page["Source_Alpha"]["entity_id"],
                        by_page["Source_Alpha-ab12cd34"]["entity_id"],
                    )
                )
            ),
            "evidence_score": 100,
            "score": 100,
            "decision": "merge",
            "left_entity_id": by_page["Source_Alpha"]["entity_id"],
            "right_entity_id": by_page["Source_Alpha-ab12cd34"]["entity_id"],
            "left_name": by_page["Source_Alpha"]["title"],
            "right_name": by_page["Source_Alpha-ab12cd34"]["title"],
            "left_page_key": "Source_Alpha",
            "right_page_key": "Source_Alpha-ab12cd34",
            "left_version": versions["Source_Alpha"],
            "right_version": versions["Source_Alpha-ab12cd34"],
            "preflight_state": "not_run",
            "preflight_errors": [],
        },
        isolated_memory / "wiki",
    )
    assert candidate["preflight_state"] == "passed"
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_source_canary",
            "type": "merge",
            "status": "pending",
            "merge_candidate": candidate,
        }
    )

    original_materialize = mutation_coordinator.materialize_markdown_projection
    interrupted = False

    def interrupt_after_commit(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected post-commit interruption")
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        interrupt_after_commit,
    )
    with pytest.raises(KeyboardInterrupt, match="post-commit interruption"):
        governance_service.resolve_governance_item(
            "gov_source_canary",
            resolution="merge",
        )

    pending = governance_store.get_governance_item("gov_source_canary")
    assert pending["status"] == "projection_pending"
    pending_journal = db_store.get_merge_journal(pending["merge_journal_id"])
    assert pending_journal["status"] == "projection_pending"
    assert pending_journal["outbox_ids"] == pending["merge_outbox_ids"]

    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        original_materialize,
    )
    resolved = governance_service.resolve_governance_item(
        "gov_source_canary",
        resolution="merge",
    )

    assert resolved["status"] == "resolved", json.dumps(
        resolved,
        ensure_ascii=False,
        indent=2,
    )
    target_path = isolated_memory / "wiki" / "Source_Alpha.md"
    duplicate_path = isolated_memory / "wiki" / "Source_Alpha-ab12cd34.md"
    assert target_path.is_file()
    assert not duplicate_path.exists()
    frontmatter, body = split_frontmatter(target_path.read_text(encoding="utf-8"))
    assert frontmatter["id"] == "source_alpha"
    assert frontmatter["sources"] == ["raw/alpha.md"]
    assert "Source_Alpha-ab12cd34" in frontmatter["aliases"]
    assert "Raw Preview MUST NOT SURVIVE" not in body
    source_rows = (
        db_store.get_connection()
        .execute(
            "SELECT source_id, data_json FROM sources "
            "WHERE json_extract(data_json, '$.raw_ref') = 'raw/alpha.md'"
        )
        .fetchall()
    )
    assert source_rows
    assert {
        json.loads(row["data_json"])["canonical_source_page"] for row in source_rows
    } == {"Source_Alpha.md"}
    preserved_by_id = {
        row["source_id"]: json.loads(row["data_json"]) for row in source_rows
    }
    preserved_source = preserved_by_id[source_row["source_id"]]
    assert preserved_source["legacy_content_hash"] == "legacy-reviewed-hash"
    assert preserved_source["retention_policy"] == "retain-7-years"
    assert preserved_source["legal_hold"] is True
    assert preserved_source["reviewed_provenance"] == {
        "reviewer": "human-review-board",
        "decision": "accepted",
    }
    assert preserved_by_id["source_legacy_alpha"]["content_hash"] == "legacy-row-hash"
    assert (
        preserved_by_id["source_legacy_alpha"]["retention_policy"]
        == "legacy-row-retention"
    )
    assert preserved_by_id["source_legacy_alpha"]["reviewed_provenance"] == {
        "reviewer": "legacy-reviewer"
    }
    preserved_artifact = json.loads(
        db_store.get_connection()
        .execute(
            "SELECT data_json FROM source_artifacts WHERE artifact_id = ?",
            (artifact_row["artifact_id"],),
        )
        .fetchone()["data_json"]
    )
    assert preserved_artifact["retention_policy"] == "retain-7-years"
    assert preserved_artifact["legal_hold"] is True
    assert preserved_artifact["reviewed_provenance"] == {
        "reviewer": "human-review-board",
        "decision": "accepted",
    }
    journal = db_store.get_merge_journal(resolved["merge_journal_id"])
    assert journal["status"] == "completed"
    assert all(
        status == "completed"
        for status in db_store.mutation_outbox_statuses(
            resolved["merge_outbox_ids"]
        ).values()
    )

    before_replay = target_path.read_bytes()
    replayed = governance_service.resolve_governance_item(
        "gov_source_canary",
        resolution="merge",
    )
    assert replayed["status"] == "resolved"
    assert target_path.read_bytes() == before_replay


def test_approved_system_merge_resolves_with_intentional_index_exclusion(
    isolated_memory,
):
    from vector_lake import indexer
    from vector_lake.wiki_utils import get_index_path

    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    indexer.generate_index()
    target_key = "System_Time-Series-Foundation-Models"
    source_key = "System_Time_Series_Foundation_Models"
    target_path = isolated_memory / "wiki" / f"{target_key}.md"
    source_path = isolated_memory / "wiki" / f"{source_key}.md"
    execute_mutation_plan(
        target_path.name,
        content=_system_content(
            "system_time_series_target",
            "Time-Series Foundation Models",
            "Curated system projection.",
        ),
    )
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1
    execute_mutation_plan(
        source_path.name,
        content=_system_content(
            "system_time_series_source",
            "Time Series Foundation Models",
            "Historical system projection.",
        ),
    )
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1

    entities = governance_store.query_entities({"type": "system"})["items"].values()
    by_page = {item["page_key"]: item for item in entities}
    versions = governance_store.canonical_page_versions({target_key, source_key})
    left_id = by_page[target_key]["entity_id"]
    right_id = by_page[source_key]["entity_id"]
    candidate = preflight_suggestion(
        {
            "pair_key": "::".join(sorted((left_id, right_id))),
            "evidence_score": 100,
            "score": 100,
            "decision": "merge",
            "left_entity_id": left_id,
            "right_entity_id": right_id,
            "left_name": by_page[target_key]["title"],
            "right_name": by_page[source_key]["title"],
            "left_page_key": target_key,
            "right_page_key": source_key,
            "left_version": versions[target_key],
            "right_version": versions[source_key],
            "component_id": "approved_system_merge_test",
            "component_size": 2,
            "preflight_state": "not_run",
            "preflight_errors": [],
        },
        isolated_memory / "wiki",
    )
    assert candidate["preflight_state"] == "passed", candidate["preflight_errors"]
    item_id = "gov_system_time_series_models"
    governance_store.upsert_governance_item(
        {
            "item_id": item_id,
            "type": "merge",
            "status": "pending",
            "merge_candidate": candidate,
        }
    )

    resolved = governance_service.resolve_governance_item(item_id, resolution="merge")

    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "merge"
    assert resolved["postcondition_errors"] == []
    assert target_path.is_file()
    assert not source_path.exists()
    assert governance_store.get_entity(left_id)["page_key"] == target_key
    assert governance_store.get_entity(right_id) is None
    assert governance_store.get_alias(right_id) == left_id
    frontmatter, _body = split_frontmatter(target_path.read_text(encoding="utf-8"))
    assert source_key in frontmatter["aliases"]
    assert all(
        status == "completed"
        for status in db_store.mutation_outbox_statuses(
            resolved["merge_outbox_ids"]
        ).values()
    )
    assert db_store.get_merge_journal(resolved["merge_journal_id"])["status"] == (
        "completed"
    )
    index_data = json.loads(get_index_path().read_text(encoding="utf-8"))
    selected = {target_key, source_key}
    assert selected.isdisjoint(index_data["nodes"])
    assert all(
        key not in selected and value not in selected
        for key, value in index_data["aliases"].items()
    )
    assert all(
        edge.get(endpoint) not in selected
        for edge in index_data["weighted_edges"]
        for endpoint in ("source", "target")
    )
    connection = db_store.get_connection()
    assert connection.execute(
        "SELECT COUNT(*) FROM wiki_search_index WHERE node_key IN (?, ?)",
        (target_key, source_key),
    ).fetchone()[0] == 0
    vector_connection = db_store.get_vector_connection()
    assert vector_connection.execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id IN (?, ?)",
        (target_key, source_key),
    ).fetchone()[0] == 0
    claim_parity = indexer.claim_graph_projection_parity()
    assert claim_parity["canonical_nodes"] == claim_parity["projection_nodes"]
    assert claim_parity["canonical_edges"] == claim_parity["projection_edges"]
    assert claim_parity["missing_nodes"] == 0
    assert claim_parity["extra_nodes"] == 0
    assert claim_parity["missing_edges"] == 0
    assert claim_parity["extra_edges"] == 0
    assert indexer.projection_pair_matches_current_generation() is True

    before_replay = target_path.read_bytes()
    replayed = governance_service.resolve_governance_item(item_id, resolution="merge")
    assert replayed["status"] == "resolved"
    assert target_path.read_bytes() == before_replay


def test_system_outbox_fails_closed_when_excluded_search_projection_is_stale(
    isolated_memory,
):
    from vector_lake import indexer

    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    indexer.generate_index()
    filename = "System_Contaminated.md"
    content = _system_content(
        "system_contaminated",
        "Contaminated System",
        "System projection exclusion test.",
    )
    execute_mutation_plan(filename, content=content)
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1
    assert indexer.projection_pair_matches_current_generation() is True

    db_store.upsert_search_index(
        "System_Contaminated",
        "contaminated",
        "contaminated",
        "contaminated",
    )
    assert indexer.index_projection_matches_canonical([filename]) is False
    outbox_id = db_store.enqueue_mutation(
        filename,
        "update",
        content,
        idempotency_key="system-contaminated-recheck",
        validation_mode="schema",
    )

    result = process_mutation_outbox_batch(
        limit=1,
        max_attempts=1,
        backoff_base=0,
        outbox_ids=[outbox_id],
    )

    assert result["completed"] == 0
    assert result["failed"] == 1
    assert db_store.mutation_outbox_statuses([outbox_id]) == {outbox_id: "failed"}


def test_source_metadata_restore_cas_preserves_concurrent_human_update(
    isolated_memory,
):
    db_store.init_db()
    source_id = "source_metadata_cas"
    expected = {
        "source_id": source_id,
        "raw_ref": "raw/source.md",
        "canonical_source_page": "Source_Metadata-CAS.md",
        "ingested_at": "2026-01-01T00:00:00+00:00",
        "legacy_content_hash": "legacy-reviewed-hash",
    }
    observed = {
        key: value for key, value in expected.items() if key != "legacy_content_hash"
    }
    observed["ingested_at"] = "2026-07-23T00:00:00+00:00"
    observed_json = json.dumps(observed, ensure_ascii=False)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO sources (source_id, data_json, updated_at) VALUES (?, ?, ?)",
            (source_id, observed_json, "2026-07-23T00:00:00+00:00"),
        )
    journal_id = "journal_source_metadata_cas"
    item_id = "gov_source_metadata_cas"
    db_store.record_merge_journal(
        journal_id,
        item_id,
        {"outbox_ids": [101]},
        status="projection_pending",
    )
    governance_store.upsert_governance_item(
        {
            "item_id": item_id,
            "type": "merge",
            "status": "projection_pending",
            "merge_candidate": {},
            "merge_journal_id": journal_id,
            "merge_outbox_ids": [101],
        }
    )

    human_update = {**observed, "reviewed_provenance": {"reviewer": "human-board"}}
    with db_store.transaction():
        conn.execute(
            "UPDATE sources SET data_json = ? WHERE source_id = ?",
            (json.dumps(human_update, ensure_ascii=False), source_id),
        )

    with pytest.raises(RuntimeError, match="changed before metadata recovery"):
        with db_store.transaction():
            governance_service.restore_preserved_source_rows_compare_and_swap(
                [{"source_id": source_id, "data": expected}],
                {source_id: observed_json},
            )
            db_store.update_merge_journal(
                journal_id,
                {"metadata_recovery_completed_at": governance_service._utc_now()},
                status="completed",
            )
            governance_store.update_governance_item(
                item_id,
                {"status": "resolved"},
                expected_statuses={"projection_pending"},
            )

    current = json.loads(
        conn.execute(
            "SELECT data_json FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()["data_json"]
    )
    assert current == human_update
    assert db_store.get_merge_journal(journal_id)["status"] == "projection_pending"
    assert (
        governance_store.get_governance_item(item_id)["status"] == "projection_pending"
    )


def test_source_merge_rejects_raw_drift_between_preflight_and_commit(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    raw_path = isolated_memory / "raw" / "drift.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("original raw bytes", encoding="utf-8")
    target_content = _source_content(
        "source_drift",
        "Curated Drift",
        "Curated evidence.",
        "[raw/drift.md]",
    )
    duplicate_content = _source_content(
        "source_drift_backlog",
        "Drift",
        "historical awaiting_subagent backlog",
        "[raw/drift.md]",
    ).replace(
        "domain: General\n",
        "domain: General\ntopic_cluster: Raw_Ingest_Backlog\n",
    )
    execute_mutation_plan("Source_Drift.md", content=target_content)
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1
    execute_mutation_plan("Source_Drift-ab12cd34.md", content=duplicate_content)
    assert process_mutation_outbox_batch(limit=10)["completed"] == 1

    entities = governance_store.query_entities({"type": "source"})["items"].values()
    by_page = {item["page_key"]: item for item in entities}
    versions = governance_store.canonical_page_versions(
        {"Source_Drift", "Source_Drift-ab12cd34"}
    )
    candidate = preflight_suggestion(
        {
            "pair_key": "::".join(
                sorted(
                    (
                        by_page["Source_Drift"]["entity_id"],
                        by_page["Source_Drift-ab12cd34"]["entity_id"],
                    )
                )
            ),
            "decision": "merge",
            "left_entity_id": by_page["Source_Drift"]["entity_id"],
            "right_entity_id": by_page["Source_Drift-ab12cd34"]["entity_id"],
            "left_name": by_page["Source_Drift"]["title"],
            "right_name": by_page["Source_Drift-ab12cd34"]["title"],
            "left_page_key": "Source_Drift",
            "right_page_key": "Source_Drift-ab12cd34",
            "left_version": versions["Source_Drift"],
            "right_version": versions["Source_Drift-ab12cd34"],
            "preflight_state": "not_run",
            "preflight_errors": [],
        },
        isolated_memory / "wiki",
    )
    assert candidate["preflight_state"] == "passed"
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_source_raw_drift",
            "type": "merge",
            "status": "pending",
            "merge_candidate": candidate,
        }
    )

    real_preflight = governance_service.preflight_suggestion

    def mutate_raw_after_preflight(candidate_data, wiki_dir):
        checked = real_preflight(candidate_data, wiki_dir)
        raw_path.write_text("changed after preflight", encoding="utf-8")
        return checked

    monkeypatch.setattr(
        governance_service,
        "preflight_suggestion",
        mutate_raw_after_preflight,
    )
    with pytest.raises(RuntimeError, match="raw artifact changed before canonical"):
        governance_service.resolve_governance_item(
            "gov_source_raw_drift",
            resolution="merge",
        )

    assert (
        governance_store.get_governance_item("gov_source_raw_drift")["status"]
        == "pending"
    )
    assert (isolated_memory / "wiki" / "Source_Drift.md").is_file()
    assert (isolated_memory / "wiki" / "Source_Drift-ab12cd34.md").is_file()
    assert (
        db_store.get_connection()
        .execute(
            "SELECT COUNT(*) FROM merge_journal WHERE item_id = 'gov_source_raw_drift'"
        )
        .fetchone()[0]
        == 0
    )


def test_candidate_report_preserves_snapshot_versions_and_pool_size(monkeypatch):
    entities = {
        "entity_a": {
            "entity_id": "entity_a",
            "canonical_name": "Alpha",
            "page_key": "Source_Alpha",
            "type": "source",
            "status": "Active",
            "sources": ["raw/alpha.md"],
        },
        "entity_b": {
            "entity_id": "entity_b",
            "canonical_name": "Alpha!",
            "page_key": "Source_Alpha-ab12cd34",
            "type": "source",
            "status": "Active",
            "sources": ["raw/alpha.md"],
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
        lambda suggestion, wiki_dir, backlink_index=None: {
            **suggestion,
            "preflight_state": "passed",
            "preflight_errors": [],
        },
    )
    monkeypatch.setattr(
        governance_metrics,
        "build_wiki_backlink_index",
        lambda wiki_dir: {},
    )

    report = governance_metrics.find_merge_candidate_report(limit=1)

    assert report["candidate_pool_size"] == 1
    assert report["returned_count"] == 1
    assert report["suggestions"][0]["left_version"] == "version-a"
    assert report["suggestions"][0]["right_version"] == "version-b"
    assert report["suggestions"][0]["snapshot_state"] == "stable"


def test_candidate_report_reuses_one_backlink_index_for_large_preflight_batch(
    monkeypatch,
):
    candidates = [
        _suggestion(
            pair_key=f"entity_{index}_a::entity_{index}_b",
            left_entity_id=f"entity_{index}_a",
            right_entity_id=f"entity_{index}_b",
            left_page_key=f"Source_{index}_A",
            right_page_key=f"Source_{index}_B",
            left_version="",
            right_version="",
        )
        for index in range(250)
    ]
    sentinel_index = object()
    build_calls = []
    observed_indexes = []
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
    monkeypatch.setattr(
        governance_metrics,
        "build_wiki_backlink_index",
        lambda wiki_dir: build_calls.append(wiki_dir) or sentinel_index,
    )

    def fake_preflight(suggestion, wiki_dir, backlink_index=None):
        observed_indexes.append(backlink_index)
        return {
            **suggestion,
            "preflight_state": "passed",
            "preflight_errors": [],
        }

    monkeypatch.setattr(
        governance_metrics,
        "preflight_suggestion",
        fake_preflight,
    )

    report = governance_metrics.find_merge_candidate_report(limit=None)

    assert report["returned_count"] == 250
    assert len(build_calls) == 1
    assert observed_indexes == [sentinel_index] * 250


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
        "preflight_suggestion",
        lambda candidate, _wiki_dir: candidate,
    )
    monkeypatch.setattr(
        governance_service,
        "merge_markdown_content",
        lambda left, right, source_key, source_metadata_conflict_policy=None: "merged",
    )
    monkeypatch.setattr(
        governance_service,
        "execute_mutation_batch",
        lambda mutations, validation_mode, origin, return_details, transaction_callback, precondition_callback: (
            captured.update(
                mutations=mutations,
                validation_mode=validation_mode,
                origin=origin,
                return_details=return_details,
                transaction_callback=transaction_callback,
                precondition_callback=precondition_callback,
            )
            or transaction_callback([11, 12])
            or {"ok": True, "outbox_ids": [11, 12], "deferred": []}
        ),
    )
    monkeypatch.setattr(db_store, "record_merge_journal", lambda *args, **kwargs: {})
    monkeypatch.setattr(db_store, "update_merge_journal", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        governance_service,
        "_reconcile_projection_pending",
        lambda item: item,
    )
    monkeypatch.setattr(
        "vector_lake.wiki_utils.get_meta_dir",
        lambda: tmp_path,
    )

    governance_service.resolve_governance_item("gov_test", resolution="merge")

    assert captured["validation_mode"] == "schema"
    assert captured["origin"] == "governance-merge"
    assert captured["return_details"] is True
    assert callable(captured["transaction_callback"])
    assert callable(captured["precondition_callback"])
    assert captured["mutations"] == [
        {
            "filename": "Source_Alpha.md",
            "content": "merged",
            "expected_version": "version-a",
            "expected_projection_hash": hashlib.sha256(b"target").hexdigest(),
        },
        {
            "filename": "Source_Alpha-Alt.md",
            "is_delete": True,
            "expected_version": "version-b",
            "expected_projection_hash": hashlib.sha256(b"source").hexdigest(),
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
