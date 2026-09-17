"""``tool_lint`` must not keep private copies of the shared vocabularies.

The lint module used to redeclare ``valid_types``, ``valid_status``,
``valid_epistemic``, ``valid_categories`` and ``valid_prefixes``.  Four were
byte-identical to their owners in ``schema_validator`` / ``wiki_utils``; the
fifth had drifted -- ``valid_status`` was missing ``archived`` and ``contested``,
so 16 live pages carrying the legal status ``Contested`` were reported as
invalid while ``schema_validator`` accepted them.

These tests drive the check through its real entry point with pages that use the
*edges* of each vocabulary, so any future copy that drifts away from the shared
source fails here instead of on the live wiki.
"""

from vector_lake.schema_validator import (
    REQUIRED_FIELDS,
    VALID_CATEGORIES,
    VALID_EPISTEMIC_STATUS,
    VALID_STATUS,
    VALID_TYPES,
    missing_required_fields,
)
from vector_lake.wiki_utils import get_wiki_dir
from vector_lake.tool_lint import lint_vector_lake


def _page(name: str, *, type_: str, status: str, epistemic: str, category: str) -> None:
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / name).write_text(
        "---\n"
        f"id: test_{name[:-3].lower()}\n"
        f"title: {name[:-3]}\n"
        f"type: {type_}\n"
        "domain: General\n"
        f"status: {status}\n"
        f"epistemic-status: {epistemic}\n"
        f"categories: [{category}]\n"
        "updated: 2026-09-17\n"
        "sources: []\n"
        "strategic_scope: core\n"
        "evidence_tier: derived\n"
        "---\n\nBody text.\n",
        encoding="utf-8",
    )


def test_the_shared_vocabularies_are_the_edges_the_lint_must_accept(isolated_memory):
    """Drive each check with the value that the drifted copy rejected."""
    assert "Contested" in VALID_STATUS  # the value the live wiki actually uses
    assert "Archived" in VALID_STATUS
    assert "system" in VALID_TYPES
    assert "evergreen" in VALID_EPISTEMIC_STATUS
    assert "Entities_and_Actors" in VALID_CATEGORIES

    _page(
        "Concept_Edge-Values.md",
        type_="system",
        status="Contested",
        epistemic="evergreen",
        category="Entities_and_Actors",
    )

    report = lint_vector_lake(auto_fix=False)

    for label in ("Invalid status", "Invalid type", "Invalid epistemic-status", "Invalid category"):
        assert label not in report, f"{label} reported for a legal vocabulary value:\n{report}"


def test_status_matching_stays_case_insensitive(isolated_memory):
    """The value is lowercased for comparison; the vocabulary is not."""
    _page(
        "Concept_Lowercase-Status.md",
        type_="concept",
        status="active",
        epistemic="seed",
        category="Uncategorized",
    )

    report = lint_vector_lake(auto_fix=False)

    assert "Invalid status" not in report
    assert "Invalid epistemic-status" not in report


def test_missing_required_fields_is_the_single_source():
    """The linter and the write gate must agree on which keys a page needs."""
    system = {
        "id": "x",
        "title": "t",
        "type": "system",
        "status": "Active",
        "categories": ["Uncategorized"],
        "updated": "2026-01-01",
    }
    assert missing_required_fields(dict(system), "System_Community_L0_deadbeef.md") == []
    assert missing_required_fields(dict(system), "Concept_X.md") == [
        "domain",
        "epistemic-status",
        "sources",
    ]
    assert "sources" in REQUIRED_FIELDS


def test_system_artifacts_are_not_flagged_for_exempt_fields(isolated_memory):
    """A ``System_*`` page missing domain/epistemic-status/sources is legal.

    ``validate_schema`` exempts those keys for system artifacts; the linter used
    to ignore the exemption and flagged all 572 community indexes.
    """
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "System_Community_L0_deadbeef.md").write_text(
        "---\n"
        "id: gov_deadbeef\n"
        "title: Community index\n"
        "type: system\n"
        "status: Active\n"
        "categories: [Uncategorized]\n"
        "updated: 2026-09-16\n"
        "---\n\n- [[Concept_Alpha]]\n",
        encoding="utf-8",
    )

    report = lint_vector_lake(auto_fix=False)

    assert "Missing fields" not in report, report
