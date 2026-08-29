import hashlib

import pytest

from vector_lake import db_store, indexer, mcp_server
from vector_lake.projection_format_v2 import (
    build_projection_roots,
    publish_prepared_projection,
)
from vector_lake.schema_validator import SchemaViolationException, validate_schema
from vector_lake.tool_rename import rename_vector_lake_entity
from vector_lake.claim_extractor import _stable_id
from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter


def test_rename_builds_one_atomic_mutation_batch(isolated_memory, monkeypatch):
    wiki_dir = get_wiki_dir()
    (wiki_dir / "Concept_Old.MD").write_text(
        "---\ntitle: Old\naliases: []\n---\nOld body [[Concept_Old]].\n",
        encoding="utf-8",
    )
    (wiki_dir / "Concept_Ref.MD").write_text("ref [[Concept_Old]]", encoding="utf-8")
    captured = []

    def fake_batch(mutations):
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

    def fake_batch(mutations):
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
