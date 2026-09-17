"""A malformed page name must not be able to block its own removal.

``resolve_wiki_mutation_path`` validated the basename for every mutation, including
deletes.  ``rename_vector_lake_entity`` starts by deleting the old name, so a page
whose filename violated the naming pattern could never be renamed:

    Error during atomic rename: Strict Naming Violation: 'Concept_-FS.md' must
    match pattern [Type]_[MainName]-[SubName].md

Two live pages were stuck that way -- ``Concept_-FS.md`` (title πFS, the π is not
in the allowed character set) and a Source page whose sub-name held a full-width
comma.  Deleting removes an existing name and cannot introduce a malformed one, so
the exemption now applies to deletes while writes stay validated.
"""

import pytest

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.wiki_utils import get_wiki_dir

_PAGE = """---
id: legacy_1
title: Legacy
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: '2026-09-17'
sources: []
strategic_scope: core
---

# Legacy

## 1. 编译事实
*[System Directive: This section represents the LATEST consensus.]*

A legacy page.
"""


def test_a_legacy_named_page_can_still_be_deleted(isolated_memory):
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    legacy = wiki / "Concept_-FS.md"
    legacy.write_text(_PAGE, encoding="utf-8")
    assert legacy.exists()

    ok, message = execute_mutation_batch([{"filename": "Concept_-FS.md", "is_delete": True}])

    assert ok is True, message
    assert not legacy.exists()


def test_a_malformed_name_is_still_rejected_for_a_write(isolated_memory):
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)

    with pytest.raises(Exception, match="Naming Violation"):
        execute_mutation_batch([{"filename": "Concept_-FS.md", "content": _PAGE}])
