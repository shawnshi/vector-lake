import hashlib
import json

import pytest

from vector_lake import db_store, indexer, mcp_server
from vector_lake import governance_store, mutation_coordinator
from vector_lake.projection_format_v2 import (
    build_projection_roots,
    publish_prepared_projection,
)
from vector_lake.schema_validator import SchemaViolationException, validate_schema
from vector_lake.tool_rename import rename_vector_lake_entity
from vector_lake.claim_extractor import _stable_id
from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter


def _identity_page(name: str, title: str) -> str:
    """A structurally valid page that deliberately lacks a legal evidence_tier."""
    return (
        "---\n"
        f"id: {name}\n"
        f"title: {title}\n"
        "type: concept\n"
        "domain: Medical_IT\n"
        "status: Active\n"
        "epistemic-status: seed\n"
        "categories:\n- Uncategorized\n"
        "updated: '2026-09-12'\n"
        "sources: []\n"
        "strategic_scope: core\n"
        "---\n"
        f"# {title}\n\n"
        "## 1. 编译事实\n"
        "*[System Directive: This section represents the LATEST consensus.]*\n\n"
        "陈述。\n\n"
        "### 物理机制 (Mechanism)\n"
        "- 机制说明。\n\n"
        "---\n\n"
        "## 2. 证据时间线 (Timeline - EVENT STORE)\n"
        "*[System Directive: This is the immutable event ledger.]*\n\n"
        "- [2026-09-12] [Observation] 观察记录。\n"
    )


def test_entity_id_cannot_migrate_to_a_still_live_page(isolated_memory):
    """The identity-transfer allowance must not let a live page's id be stolen.

    An ``entity_id`` may move to a new page key only when the page that reserved
    it is deleted by the same batch.  A surviving page keeps its id exclusively.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    now = "2026-09-13T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO entities "
            "(entity_id, canonical_name, data_json, updated_at) VALUES (?,?,?,?)",
            (
                "entity_live",
                "Concept_Live",
                json.dumps({"page_key": "Concept_Live"}),
                now,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO entity_identities "
            "(entity_id, page_key, canonical_name, identity_origin, data_json, "
            "recorded_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                "entity_live",
                "Concept_Live",
                "Concept_Live",
                "canonical_write",
                "{}",
                now,
                now,
            ),
        )
    proposed = [{"entity_id": "entity_live", "page_key": "Concept_Thief"}]

    with pytest.raises(governance_store.CanonicalIdOwnershipError):
        governance_store._validate_canonical_id_ownership(
            conn,
            proposed_entities=proposed,
            proposed_claims=[],
            proposed_evidence=[],
            affected_page_keys={"Concept_Thief"},
        )

    # Retiring the page that reserved the id in the same batch makes it legal.
    governance_store._validate_canonical_id_ownership(
        conn,
        proposed_entities=proposed,
        proposed_claims=[],
        proposed_evidence=[],
        affected_page_keys={"Concept_Thief"},
        retired_page_keys={"Concept_Live"},
    )


def test_identity_only_rename_skips_evidence_contract_but_keeps_structure(
    isolated_memory,
):
    """A migration-era page without a legal evidence_tier can still be renamed.

    Regression: `rename_vector_lake_entity` re-validated the destination page
    against the full evidence contract, so pages created before that contract
    could never be renamed even though their content was already reviewed.  The
    destination is now validated structurally; content that breaks the structure
    is still refused.
    """
    wiki_dir = get_wiki_dir()
    legacy = wiki_dir / "Concept_Atrium Health.md"
    legacy.write_text(_identity_page("atrium", "Atrium Health"), encoding="utf-8")

    result = rename_vector_lake_entity(
        "Concept_Atrium Health", "Concept_Atrium-Health", dry_run=False
    )
    assert result.startswith("Successfully renamed"), result
    assert not legacy.exists()
    assert (wiki_dir / "Concept_Atrium-Health.md").is_file()


def test_identity_only_rejects_delete_and_existing_target(isolated_memory):
    """The identity-only allowance must never overwrite or remove a live page."""
    wiki_dir = get_wiki_dir()
    legacy = wiki_dir / "Concept_Atrium Health.md"
    legacy.write_text(_identity_page("atrium", "Atrium Health"), encoding="utf-8")
    existing = wiki_dir / "Concept_Atrium-Health.md"
    existing.write_text(_identity_page("atrium2", "Atrium Health 2"), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be deletes"):
        mutation_coordinator.validate_mutation_batch_metadata(
            [
                {
                    "filename": "Concept_Atrium-Health.md",
                    "is_delete": True,
                    "expected_projection_hash": "",
                }
            ],
            validation_mode="full",
            identity_only_filenames=["Concept_Atrium-Health.md"],
        )

    with pytest.raises(ValueError, match="must not already exist"):
        mutation_coordinator.validate_mutation_batch_metadata(
            [
                {
                    "filename": "Concept_Atrium-Health.md",
                    "content": _identity_page("atrium3", "Atrium Health 3"),
                    "expected_projection_hash": "",
                }
            ],
            validation_mode="full",
            identity_only_filenames=["Concept_Atrium-Health.md"],
        )


def test_rename_retires_legacy_named_source_in_full_validation(isolated_memory):
    """A migration-era name must be retirable without the schema-maintenance path.

    Regression: `Concept_Atrium Health.md` (space) and `Concept_Epic_Systems.md`
    (underscore in the core name) could neither be deleted nor renamed, because
    the delete leg of the rename was validated against the current filename
    contract while the schema-maintenance path forbids deletes.  Deletes now
    carry the legacy-name allowance; updates keep the strict contract.
    """
    wiki_dir = get_wiki_dir()
    legacy = wiki_dir / "Concept_Atrium Health.md"
    legacy.write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")

    metadata = mutation_coordinator.validate_mutation_batch_metadata(
        [
            {
                "filename": "Concept_Atrium Health.md",
                "is_delete": True,
                "expected_projection_hash": hashlib.sha256(
                    legacy.read_bytes()
                ).hexdigest(),
            }
        ],
        validation_mode="full",
    )
    assert metadata[0]["validation_mode"] == "full"
    assert metadata[0]["filepath"].name == "Concept_Atrium Health.md"

    with pytest.raises(ValueError, match="characters"):
        mutation_coordinator.validate_mutation_batch_metadata(
            [
                {
                    "filename": "Concept_Atrium Health.md",
                    "content": "---\ntitle: x\n---\nbody\n",
                    "expected_projection_hash": "",
                }
            ],
            validation_mode="full",
        )


def test_rename_builds_one_atomic_mutation_batch(isolated_memory, monkeypatch):
    wiki_dir = get_wiki_dir()
    (wiki_dir / "Concept_Old.MD").write_text(
        "---\ntitle: Old\naliases: []\n---\nOld body [[Concept_Old]].\n",
        encoding="utf-8",
    )
    (wiki_dir / "Concept_Ref.MD").write_text("ref [[Concept_Old]]", encoding="utf-8")
    captured = []

    def fake_batch(mutations, **kwargs):
        captured.append(mutations)
        return True, "ok"

    monkeypatch.setattr("vector_lake.tool_rename.execute_mutation_batch", fake_batch)
    result = rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False)

    assert result.startswith("Successfully renamed")
    assert len(captured) == 1
    assert [item["filename"] for item in captured[0]] == [
        "Concept_Old.MD",
        "Concept_New.md",
        "Concept_Ref.MD",
    ]
    assert captured[0][0]["is_delete"] is True
    assert (
        captured[0][0]["expected_projection_hash"]
        == hashlib.sha256((wiki_dir / "Concept_Old.MD").read_bytes()).hexdigest()
    )
    assert captured[0][1]["expected_projection_hash"] == ""
    assert (
        captured[0][2]["expected_projection_hash"]
        == hashlib.sha256((wiki_dir / "Concept_Ref.MD").read_bytes()).hexdigest()
    )
    assert "[[Concept_New|Old]]" in captured[0][2]["content"]
    renamed_frontmatter, _ = split_frontmatter(captured[0][1]["content"])
    assert renamed_frontmatter["entity_id"] == _stable_id("entity", "Concept_Old")


def test_batch_replace_links_commits_once(isolated_memory, monkeypatch):
    wiki_dir = get_wiki_dir()
    (wiki_dir / "Concept_A.md").write_text("[[Old]]", encoding="utf-8")
    (wiki_dir / "Concept_B.MD").write_text("x [[Old]] y", encoding="utf-8")
    captured = []

    def fake_batch(mutations, **kwargs):
        captured.append(mutations)
        return True, "ok"

    monkeypatch.setattr(
        "vector_lake.mutation_coordinator.execute_mutation_batch", fake_batch
    )
    result = mcp_server.batch_replace_links("[[Old]]", "[[New]]", dry_run=False)

    assert "in 2 files" in result
    assert len(captured) == 1
    assert len(captured[0]) == 2
    assert all("[[New]]" in item["content"] for item in captured[0])

    assert {
        item["filename"]: item["expected_projection_hash"] for item in captured[0]
    } == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in wiki_dir.iterdir()
        if path.is_file() and path.suffix.casefold() == ".md"
    }


def test_schema_tag_collision_is_not_swallowed(tmp_path):
    db_store.init_db()
    prepared = build_projection_roots(
        tmp_path,
        {"nodes": {"Concept_Existing": {"title": "Existing", "aliases": []}}},
        {"nodes": [], "edges": []},
        canonical_generation=indexer.canonical_runtime_generation_snapshot(),
    )
    publish_prepared_projection(tmp_path, prepared)
    index_path = tmp_path / "index.json"
    frontmatter = {
        "id": "source_test",
        "title": "Test",
        "type": "source",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Uncategorized"],
        "updated": "2026-07-14T00:00:00+00:00",
        "sources": [],
        "tags": ["Existing"],
    }

    with pytest.raises(SchemaViolationException, match="Tag Collision"):
        validate_schema(
            frontmatter, "source body", "Source_Test.md", index_path=index_path
        )
