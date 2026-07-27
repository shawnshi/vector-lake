import json

import pytest

from vector_lake import mcp_server
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

    monkeypatch.setattr("vector_lake.mutation_coordinator.execute_mutation_batch", fake_batch)
    result = mcp_server.batch_replace_links("[[Old]]", "[[New]]", dry_run=False)

    assert "in 2 files" in result
    assert len(captured) == 1
    assert len(captured[0]) == 2
    assert all("[[New]]" in item["content"] for item in captured[0])


def test_schema_tag_collision_is_not_swallowed(tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps({"nodes": {"Concept_Existing": {"title": "Existing", "aliases": []}}}),
        encoding="utf-8",
    )
    frontmatter = {
        "id": "source_test",
        "title": "Test",
        "type": "source",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Source"],
        "updated": "2026-07-14T00:00:00+00:00",
        "sources": [],
        "tags": ["Existing"],
    }

    with pytest.raises(SchemaViolationException, match="Tag Collision"):
        validate_schema(frontmatter, "source body", "Source_Test.md", index_path=index_path)
