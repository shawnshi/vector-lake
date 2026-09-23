"""Tags and entity names are two namespaces; an alias may not be written in the tag one.

A ``tags:`` entry is a label and an ``aliases:`` entry is a name for the entity itself.  They were
checked in one direction only -- a *tag* colliding with an existing entity was refused ("Tag
Collision") -- so ``aliases: ["#某个标签"]`` was accepted at write time.  That is the reverse leak:
the entry carries tag syntax into the entity namespace, where every consumer of ``aliases`` (link
resolution, the stub guard, the merge candidate finder, ``declared`` in the linter) treats it as a
name, so the tag becomes a link target.

No live page has such an alias (measured: zero), so this is a write-time gate rather than a repair.
The direction that already existed is pinned here too, because it is the same rule: the two
namespaces stay disjoint.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import link_resolution
from vector_lake.schema_validator import SchemaViolationException, validate_schema

# Enough body for the structural rules: the tests below are about ``aliases`` and must fail (or
# pass) for that reason and no other.
BODY = "\n".join(["## 1. 编译事实", "", "Facts.", "", "## 2. 证据时间线", "", "Timeline.", ""])


def _frontmatter(**overrides):
    frontmatter = {
        "id": "concept_test",
        "title": "Test",
        "type": "concept",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["System_Architecture"],
        "updated": "2026-09-19T00:00:00+00:00",
        "sources": [],
        "tags": [],
    }
    frontmatter.update(overrides)
    return frontmatter


@pytest.mark.parametrize("alias", ["#标签", "# Tag", "  #padded"])
def test_an_alias_written_with_tag_syntax_is_refused(alias):
    with pytest.raises(SchemaViolationException, match="tag syntax"):
        validate_schema(_frontmatter(aliases=[alias]), BODY, "Concept_Test.md")


def test_a_plain_alias_is_still_accepted():
    """The gate refuses tag syntax, not aliases: a normal one must pass unchanged."""
    validate_schema(_frontmatter(aliases=["医疗资源规划", "HRP"]), BODY, "Concept_Test.md")


def test_a_hash_inside_or_after_an_alias_is_not_tag_syntax():
    """Only a leading '#' is syntax; 'C#' is a name, and so is a '#' that is not first."""
    validate_schema(_frontmatter(aliases=["C#", "Issue #2"]), BODY, "Concept_Test.md")


def test_tags_keep_their_own_syntax():
    """The same string is legal in ``tags:``: that namespace is where '#' belongs."""
    validate_schema(_frontmatter(tags=["#医疗信息化"]), BODY, "Concept_Test.md")


def test_a_tag_that_is_an_entity_name_is_still_refused(isolated_memory):
    """The pre-existing direction of the same rule, kept pinned beside the new one."""
    wiki_dir = isolated_memory / "wiki"
    index_path = wiki_dir / ".meta" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"nodes": {"Concept_Existing": {"title": "Existing", "aliases": ["别名"]}}}),
        encoding="utf-8",
    )

    for tag in ["Existing", "别名"]:
        with pytest.raises(SchemaViolationException, match="Tag Collision"):
            validate_schema(
                _frontmatter(tags=[tag]), BODY, "Concept_Test.md", index_path=index_path
            )


def test_a_tag_never_becomes_a_link_target():
    """Namespace isolation in the resolver: only titles and aliases declare a name.

    A tag is not read here at all, which is why the write-time gate is the only place that can
    keep the two apart.
    """
    nodes = {"Concept_甲": {"title": "甲", "aliases": [], "tags": ["共享标签"]}}
    declared = link_resolution.declared_names_from_nodes(nodes)

    assert "共享标签" not in declared
    assert declared == {"甲": ["Concept_甲"]}
    _core_pages, unique_cores = link_resolution.core_name_maps(nodes.keys(), declared)
    assert link_resolution.resolve_link_target("共享标签", {}, unique_cores) is None
