"""The authoring gate for classification, and the lint repair that stopped inventing it.

Two halves of one defect.  ``SCHEMA_CATEGORIES.md`` bans ``Uncategorized`` for a new node and
the ingest prompt promises that "the validator rejects" it, but the write gate checked nothing:
by 2026-09-23 the category vocabulary was enforced by ``tool_lint`` alone.  The linter's repair
then wrote the banned value into 3 000 pages, and appended a second copy of it to 28 of them.

What is asserted here is the boundary between the two modes.  ``full`` is authoring and answers
to the contract; ``schema`` is bounded legacy maintenance -- projections, restores, renames --
and must keep working for pages that have not been migrated.
"""

import datetime

import pytest

from vector_lake import schema_validator, tool_lint
from vector_lake.defense_hook import DefenseHookException
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.node_vocabulary import STUB_MARKER_TAG
from vector_lake.schema_validator import SchemaViolationException
from vector_lake.wiki_utils import (
    get_wiki_dir,
    read_markdown_file,
    split_frontmatter,
    write_markdown_file,
)

from tests.test_ingest_contract import _concept_content
from tests.test_mutation_coordinator import _write_purpose_contract

_BASE_FRONTMATTER, _BASE_BODY = split_frontmatter(_concept_content())


def _frontmatter(**overrides) -> dict:
    frontmatter = dict(_BASE_FRONTMATTER)
    frontmatter.update(overrides)
    return frontmatter


def _write(name: str, **overrides):
    """Author a page through the gate, in ``full`` mode, as the ingest path does."""
    path = get_wiki_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown_file(path, _frontmatter(**overrides), _BASE_BODY)
    return path


def test_a_new_node_cannot_be_uncategorized(isolated_memory):
    """The ontology's own wording: ``Uncategorized`` is for imported legacy nodes only."""
    _write_purpose_contract(isolated_memory)

    with pytest.raises(DefenseHookException, match="imported legacy nodes only"):
        _write("Concept_New.md", categories=["Uncategorized"])


def test_a_stub_page_may_be_uncategorized(isolated_memory):
    """A placeholder is honest about not being classified; that is what the marker says."""
    _write_purpose_contract(isolated_memory)

    path = _write(
        "Concept_Target.md", categories=["Uncategorized"], tags=[STUB_MARKER_TAG]
    )

    assert path.exists()


@pytest.mark.parametrize("category", ["Source", "Concept", "System"])
def test_the_category_is_a_domain_not_the_node_type(isolated_memory, category):
    """``Source``/``Concept`` were written into pages by the projection's fallback."""
    _write_purpose_contract(isolated_memory)

    with pytest.raises(DefenseHookException, match="Invalid category"):
        _write("Concept_New.md", categories=[category])


def test_categories_must_hold_exactly_one_element(isolated_memory):
    _write_purpose_contract(isolated_memory)

    with pytest.raises(DefenseHookException, match="a list with exactly one domain"):
        _write("Concept_New.md", categories=["Healthcare_IT", "System_Architecture"])


def test_a_bare_string_category_is_refused(isolated_memory):
    _write_purpose_contract(isolated_memory)

    with pytest.raises(DefenseHookException, match="a list with exactly one domain"):
        _write("Concept_New.md", categories="Healthcare_IT")


def test_a_new_node_cannot_use_an_unregistered_domain(isolated_memory):
    """The facet has two tiers: a macro domain, or a vertical registered in the schema.

    192 domain flavours accumulated while the field had no vocabulary at all; the answer was not
    to force every subject into one of nine macro values -- the pages left outside them were
    verticals whose tags carried the subject in 1 case of 118.
    """
    _write_purpose_contract(isolated_memory)

    with pytest.raises(DefenseHookException, match="neither a macro domain nor a registered"):
        _write("Concept_New.md", domain="AI_Industry")


def test_a_registered_vertical_is_accepted_on_a_new_node(isolated_memory):
    """Positive control: a registered subject passes, and so does a macro domain."""
    _write_purpose_contract(isolated_memory)

    assert _write("Concept_Media.md", domain="Media").exists()
    assert _write("Concept_Macro.md", domain="Medical_IT").exists()


def test_a_macro_domain_alias_is_accepted_on_a_new_node(isolated_memory):
    """``Healthcare_IT`` names ``Medical_IT``: the two axes spell one subject two ways.

    The live failure this closes: the Source page for
    ``raw/research/刘海一先生的历史定位、生平贡献与思想体系深度解析20260926.md`` finalized with
    ``domain: Healthcare_IT`` and was refused as unregistered, while its two siblings in the same
    batch happened to emit ``Medical_IT``.  Both spellings name the subject the other axis calls
    ``Healthcare_IT``, so the refusal was vocabulary drift between ``categories`` and ``domain``,
    not an invented subject.
    """
    _write_purpose_contract(isolated_memory)

    assert _write("Concept_Healthcare-IT.md", domain="Healthcare_IT").exists()


def test_an_alias_is_not_a_vertical():
    """The distinction the registration turns on, asserted where it decides behaviour.

    A vertical is a subject no macro domain names, so it stays its own value.  An alias is a
    second spelling of a macro domain, so it must resolve to one and must not enlarge the facet:
    ``tool_search._passes_filters`` compares ``domain`` by equality, and a second value for one
    subject is what splits a search.
    """
    assert "Healthcare_IT" not in schema_validator.DOMAIN_VERTICALS
    assert "Healthcare_IT" not in schema_validator.VALID_DOMAINS
    assert schema_validator.canonical_domain("Healthcare_IT") == "Medical_IT"
    assert schema_validator.canonical_domain("Medical_IT") == "Medical_IT"
    assert schema_validator.canonical_domain("Media") == "Media", "a vertical is not remapped"
    assert schema_validator.canonical_domain("AI_Industry") == "AI_Industry"


def test_search_matches_an_alias_spelling_on_either_side():
    """A reader cannot be expected to know which spelling a page was stored under."""
    from vector_lake import tool_search

    node = {"domain": "Healthcare_IT"}
    assert tool_search._passes_filters(node, "Medical_IT", None, False, None)
    node = {"domain": "Medical_IT"}
    assert tool_search._passes_filters(node, "Healthcare_IT", None, False, None)
    node = {"domain": "Media"}
    assert not tool_search._passes_filters(node, "Medical_IT", None, False, None)


def test_a_new_node_cannot_enter_the_generated_namespace(isolated_memory):
    """``System_`` holds pages the wiki generates about itself; 235 knowledge pages sit there."""
    _write_purpose_contract(isolated_memory)

    with pytest.raises(DefenseHookException, match="generated artifacts"):
        _write("System_Topic.md", type="system", categories=["Healthcare_IT"])


def test_a_generated_artifact_is_still_allowed_in_its_own_namespace(isolated_memory):
    """Positive control: the rule is about the family, and the artifact family must pass."""
    _write_purpose_contract(isolated_memory)

    path = _write(
        "System_Community_L0_deadbeef.md",
        type="system",
        categories=["System"],
    )

    assert path.exists()


def test_a_renamed_generated_artifact_is_still_an_artifact(isolated_memory):
    """235 community indexes were renamed to their titles (``System_<标题>.md``).

    A name-only rule read them as knowledge pages: it counted them as a defect, and it would
    have refused to write them under rules that a generated index does not answer to.
    """
    _write_purpose_contract(isolated_memory)

    path = _write(
        "System_集团化医院信息架构与溯源.md",
        type="system",
        categories=["System"],
        community_id=405,
        level="L0",
    )

    assert path.exists()


def test_the_shape_rule_applies_to_an_update_too(isolated_memory):
    """The gap the two copies left between them.

    With the rule only in the purpose gate (which does not run in ``schema`` mode) and in the
    new-node branch (which does not run for an existing page), a bare string could be written
    straight over an existing page's category.  It cannot now.
    """
    _write_purpose_contract(isolated_memory)
    path = get_wiki_dir() / "Concept_Legacy.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _concept_content().replace(
            "categories: [System_Architecture]", 'categories: "Healthcare_IT"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(SchemaViolationException, match="must be a list with exactly one domain"):
        execute_mutation_batch(
            [{"filename": path.name, "content": path.read_text(encoding="utf-8")}],
            validation_mode="schema",
        )


_METRIC_LINE = (
    "- [[Concept_Target]] 的 {Metric: Market_Share} 为 12% "
    "(Source: [[Source_Original]])\n"
)


def _metric_body() -> str:
    """The metric line belongs in section 1 -- anything after the timeline heading is read as a
    timeline entry by that section's (unbounded) validator."""
    head, sep, tail = _BASE_BODY.partition("## 2. 证据时间线")
    return f"{head}{_METRIC_LINE}\n{sep}{tail}"


def test_a_new_page_asserting_a_metric_must_declare_its_evidence(isolated_memory):
    """``evidence_tier`` is asked for exactly where it decides something.

    Required of every page it was filled on 10% of the corpus, because nothing read it.  A number
    in the compiled truth is the case where "the vendor said so" against "independently verified"
    changes whether the page may be quoted.
    """
    _write_purpose_contract(isolated_memory)
    path = get_wiki_dir() / "Concept_Metric.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(DefenseHookException, match="without an evidence_tier"):
        # The shared fixture already declares one; this is the page that does not.
        write_markdown_file(path, _frontmatter(evidence_tier=""), _metric_body())


def test_a_metric_assertion_with_its_evidence_is_accepted(isolated_memory):
    _write_purpose_contract(isolated_memory)
    path = get_wiki_dir() / "Concept_Metric.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    write_markdown_file(
        path, _frontmatter(evidence_tier="primary"), _metric_body()
    )

    assert path.exists()


def test_the_evidence_requirement_is_authoring_only(isolated_memory):
    """A page that already asserts a metric keeps whatever support it declared -- even none."""
    _write_purpose_contract(isolated_memory)
    path = get_wiki_dir() / "Concept_Legacy-Metric.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _concept_content().replace(
            "## 2. 证据时间线", f"{_METRIC_LINE}\n## 2. 证据时间线"
        ),
        encoding="utf-8",
    )

    frontmatter, body, _ = read_markdown_file(path)
    write_markdown_file(path, frontmatter, body)

    assert path.exists()


def test_an_existing_legacy_page_stays_writable(isolated_memory):
    """Growth-only, as with the placeholder source: refusing a legacy value here would freeze
    the 3 000 pages that carry one, and the migration is a separate pass."""
    _write_purpose_contract(isolated_memory)
    path = get_wiki_dir() / "Concept_Legacy.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _concept_content().replace(
            "categories: [System_Architecture]", "categories: [Uncategorized]"
        ),
        encoding="utf-8",
    )

    frontmatter, body, _ = read_markdown_file(path)
    write_markdown_file(path, frontmatter, body)

    assert path.exists()


def test_legacy_maintenance_mode_skips_the_authoring_rules(isolated_memory):
    """A restore or a projection re-materializes a node that already exists."""
    _write_purpose_contract(isolated_memory)
    content = _concept_content().replace(
        "categories: [System_Architecture]", "categories: [Uncategorized]"
    )

    execute_mutation_batch(
        [{"filename": "Concept_Restored.md", "content": content}], validation_mode="schema"
    )

    assert (get_wiki_dir() / "Concept_Restored.md").exists()


def test_the_rules_have_one_owner_each():
    """Shape and vocabulary answer to one function, the new-node rules to another."""
    from vector_lake.schema_validator import category_shape_violation, classification_violations

    assert "Invalid category" in category_shape_violation(
        {"categories": ["Source"], "tags": []}, "Concept_X.md"
    )
    assert "exactly one domain" in category_shape_violation(
        {"categories": ["A", "B"], "tags": []}, "Concept_X.md"
    )
    assert category_shape_violation(
        {"categories": ["Healthcare_IT"], "tags": []}, "Concept_X.md"
    ) is None

    # The new-node owner says nothing about shape or vocabulary: that is not its job.
    for shape in ("Source", ["A", "B"], "Healthcare_IT"):
        assert classification_violations(
            {"categories": shape, "domain": "Medical_IT", "tags": []}, "Concept_X.md"
        ) == []

    found = classification_violations(
        {"categories": ["Healthcare_IT"], "domain": "AI_Industry", "tags": []}, "System_Topic.md"
    )
    assert len(found) == 2, found
    assert any("neither a macro domain nor a registered" in item for item in found)
    assert any("generated artifacts" in item for item in found)


# ---------------------------------------------------------------------------------------------
# The lint repair that used to fabricate the banned value.
# ---------------------------------------------------------------------------------------------


def _lintable_page(name: str, **replacements):
    """A page lint will not garbage-collect: it has to look recently updated."""
    path = get_wiki_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _concept_content().replace(
        "updated: 2026-07-13T00:00:00+00:00",
        f"updated: {datetime.date.today().isoformat()}T00:00:00+00:00",
    )
    for old, new in replacements.items():
        content = content.replace(old, new)
    path.write_text(content, encoding="utf-8")
    return path


def test_lint_reports_a_missing_classification_instead_of_fabricating_one(isolated_memory):
    """This branch is where the corpus's 3 000 unclassified pages came from."""
    _write_purpose_contract(isolated_memory)
    path = _lintable_page(
        "Concept_Bad-Category.md", **{"categories: [System_Architecture]": "categories: [Source]"}
    )

    report = tool_lint.lint_vector_lake(auto_fix=True)

    assert "Invalid category 'Source'" in report, report
    frontmatter, _, _ = read_markdown_file(path)
    assert frontmatter["categories"] == ["Source"], "the repair rewrote the value it reported"


def test_lint_does_not_append_a_second_uncategorized(isolated_memory):
    """The duplicate that reached 28 pages: invalid element first, ``Uncategorized`` second."""
    _write_purpose_contract(isolated_memory)
    path = _lintable_page(
        "Concept_Duplicate-Category.md",
        **{"categories: [System_Architecture]": "categories: [Source, Uncategorized]"},
    )

    tool_lint.lint_vector_lake(auto_fix=True)

    frontmatter, _, _ = read_markdown_file(path)
    assert frontmatter["categories"] == ["Source", "Uncategorized"]


def test_lint_reports_an_unregistered_domain_and_excuses_a_stub(isolated_memory):
    """The report names only what needs registering, and a placeholder has no subject yet."""
    _write_purpose_contract(isolated_memory)
    _lintable_page("Concept_Odd-Domain.md", **{"domain: General": "domain: AI_Industry"})
    stub = _lintable_page("Concept_Placeholder.md")
    stub.write_text(
        stub.read_text(encoding="utf-8")
        .replace("domain: General", "domain: Uncategorized")
        # The shared fixture has no tags key at all, so the marker is added rather than replaced.
        .replace("epistemic-status: seed", f"epistemic-status: seed\ntags: [{STUB_MARKER_TAG}]"),
        encoding="utf-8",
    )

    report = tool_lint.lint_vector_lake(auto_fix=False)
    lines = report.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("15. "))
    end = next(i for i, line in enumerate(lines) if line.startswith("16. "))
    domain_section = "\n".join(lines[start:end])

    assert "neither a macro domain nor a registered vertical" in domain_section, domain_section
    assert "Concept_Odd-Domain.md" in domain_section, domain_section
    assert "Concept_Placeholder.md" not in domain_section, (
        "a placeholder has no subject yet, so it is not reported for one: " + domain_section
    )
