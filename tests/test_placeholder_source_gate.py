"""A placeholder source may not be *added*, but pages that already carry it keep working.

The rule is deliberately growth-only.  A census on 2026-09-19 found 2 267 live pages citing
``Source_Auto_Fixed`` (28.6% of the wiki) -- the anchor page exists precisely because those
pages had no provenance to record.  A flat ban would make more than a quarter of the store
unwritable and would freeze the pages the anchor was created for, so what is enforced is
that the marker cannot spread, and that a repair which removes it is never blocked.

The two halves therefore need separate coverage: the pure rule, and the fact that the write
path supplies the page's *current* sources so "already carried it" is decidable.
"""
import pytest

from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.schema_validator import (
    PLACEHOLDER_SOURCES,
    SchemaViolationException,
    check_placeholder_sources,
    source_key,
)
from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter

PLACEHOLDER = "[[Source_Auto_Fixed]]"


@pytest.fixture(autouse=True)
def purpose_contract(isolated_memory):
    """The write path runs the purpose gate too, so the isolated tree needs a contract."""
    (isolated_memory / "purpose.md").write_text(
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
    return isolated_memory


def _page(entity_id="test_page", *, sources, title="Concept_Test"):
    # Quoted entries, matching how the real pages are written: an unquoted
    # `- [[Source_X]]` is YAML flow-sequence syntax and parses as a nested list.
    rendered = "\n".join(f"- '{entry}'" for entry in sources) if sources else "[]"
    return f"""---
id: {entity_id}
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories:
- Uncategorized
updated: 2026-09-19T00:00:00+00:00
tags: []
sources:
{rendered}
strategic_scope: core
evidence_tier: primary
---
# {title}

## 1. 编译事实 (Compiled Truth - READ MODEL)

*[System Directive: This section represents the LATEST consensus. NO historical narrative here. NO marketing fluff.]*

{title} 是本页对象。 (Last Reshaped: 2026-09-19)

### 物理机制 (Mechanism)

- {title} 的机制。 (Source: [[Source_Real]])

---

## 2. 证据时间线 (Timeline - EVENT STORE)

*[System Directive: This is the immutable event ledger. All facts in Section 1 MUST trace back to entries here.]*

- [2026-09-19] [Observation] {title} 记录。 (Source: [[Source_Real]])
"""


# --- The rule itself ---------------------------------------------------------------


def test_placeholder_key_is_the_documented_one():
    # The constant is the contract the write gate reads; a silent rename here would
    # disable the gate without failing anything else.
    assert "source_auto_fixed" in PLACEHOLDER_SOURCES


@pytest.mark.parametrize(
    "entry",
    [
        "[[Source_Auto_Fixed]]",
        "[[Source_Auto_Fixed|Auto Fixed]]",
        "[[Source_Auto_Fixed.md]]",
        "Source_Auto_Fixed",
        "source_auto_fixed",
        # An unquoted `- [[Source_Auto_Fixed]]` is YAML flow-sequence syntax, so the
        # parser hands this shape over as a nested list. Missing it would leave the gate
        # open to the form a hand-written page is most likely to use.
        [["Source_Auto_Fixed"]],
    ],
)
def test_every_written_form_of_the_placeholder_is_recognised(entry):
    assert source_key(entry) in PLACEHOLDER_SOURCES


def test_the_nested_list_form_is_refused_as_well():
    # The whole point of normalising the list shape: it must not be a way through.
    with pytest.raises(SchemaViolationException):
        check_placeholder_sources([["Source_Auto_Fixed"]], [])


def test_a_new_page_may_not_cite_the_placeholder():
    with pytest.raises(SchemaViolationException):
        check_placeholder_sources([PLACEHOLDER], [])


def test_adding_the_placeholder_to_a_page_that_lacked_it_is_refused():
    with pytest.raises(SchemaViolationException):
        check_placeholder_sources(["raw/real.pdf", PLACEHOLDER], ["raw/real.pdf"])


def test_a_page_that_already_carried_it_may_keep_it():
    # The grandfather case: 2 267 pages depend on this being allowed.
    check_placeholder_sources([PLACEHOLDER], [PLACEHOLDER])


def test_a_repair_write_that_removes_it_is_allowed():
    check_placeholder_sources([], [PLACEHOLDER])

    check_placeholder_sources(["raw/real.pdf"], [PLACEHOLDER, "raw/real.pdf"])


def test_a_real_source_is_never_affected():
    check_placeholder_sources(["raw/a.pdf", "[[Source_Real_Page]]"], [])


def test_empty_sources_is_allowed():
    # An empty list is honest; that is the whole point of the message.
    check_placeholder_sources([], [])
    check_placeholder_sources(None, [])


# --- The write path supplies the previous state ------------------------------------


def test_write_path_rejects_a_new_page_citing_the_placeholder(isolated_memory):
    with pytest.raises(SchemaViolationException):
        execute_mutation_plan("Concept_Test.md", content=_page(sources=[PLACEHOLDER]))

    assert not (get_wiki_dir() / "Concept_Test.md").exists()


def test_write_path_accepts_a_new_page_without_the_placeholder(isolated_memory):
    execute_mutation_plan("Concept_Test.md", content=_page(sources=["raw/real.pdf"]))

    frontmatter, _ = split_frontmatter(
        (get_wiki_dir() / "Concept_Test.md").read_text(encoding="utf-8")
    )
    assert frontmatter["sources"] == ["raw/real.pdf"]


def test_write_path_rejects_adding_the_placeholder_to_an_existing_page(isolated_memory):
    execute_mutation_plan("Concept_Test.md", content=_page(sources=["raw/real.pdf"]))

    with pytest.raises(SchemaViolationException):
        execute_mutation_plan(
            "Concept_Test.md", content=_page(sources=["raw/real.pdf", PLACEHOLDER])
        )


def test_write_path_allows_rewriting_a_grandfathered_page(isolated_memory):
    """The regression this rule could cause: the 2 267 pages must stay repairable."""
    path = get_wiki_dir() / "Concept_Test.md"
    path.write_text(_page(sources=[PLACEHOLDER]), encoding="utf-8")

    execute_mutation_plan(
        "Concept_Test.md", content=_page(sources=[PLACEHOLDER], title="Concept_Test_Renamed")
    )

    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "Concept_Test_Renamed"
    assert frontmatter["sources"] == [PLACEHOLDER]


def test_write_path_allows_the_repair_that_removes_it(isolated_memory):
    path = get_wiki_dir() / "Concept_Test.md"
    path.write_text(_page(sources=[PLACEHOLDER]), encoding="utf-8")

    execute_mutation_plan("Concept_Test.md", content=_page(sources=["raw/real.pdf"]))

    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter["sources"] == ["raw/real.pdf"]


def test_unreadable_previous_page_is_not_reported_as_no_sources(isolated_memory, monkeypatch):
    """A read fault must not be laundered into a provenance verdict.

    ``_previous_sources`` returning [] on error would make every existing page look new,
    turning a transient I/O problem into "the placeholder was added" and rejecting writes
    that should pass.
    """
    path = get_wiki_dir() / "Concept_Grandfathered.md"
    path.write_text(_page(sources=[PLACEHOLDER]), encoding="utf-8")

    import vector_lake.mutation_coordinator as coordinator

    real_read_text = coordinator.Path.read_text

    def explode(self, *args, **kwargs):
        if self.name == "Concept_Grandfathered.md":
            raise OSError("simulated read fault")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(coordinator.Path, "read_text", explode)

    with pytest.raises(OSError):
        execute_mutation_plan("Concept_Grandfathered.md", content=_page(sources=[PLACEHOLDER]))
