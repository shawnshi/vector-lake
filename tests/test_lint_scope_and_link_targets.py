"""Linting scope and link resolution are different questions.

``lint_vector_lake`` filtered one list and then used it for both: which files to *lint*,
and which link targets *exist*.  The two halves failed in opposite directions.

* A file the wiki writes about itself was linted as a node unless its name happened to
  be one of the three in the filter.  ``orphan_pages.md`` therefore had no valid prefix
  and no frontmatter, so it was reported as "Does not start with valid prefix" and,
  under ``auto_fix``, **renamed to ``Concept_orphan_pages.md``** -- a destructive fix
  applied to a generated report, and one that also hides it from whatever reads it.
* ``index.md`` / ``log.md`` / ``overview.md`` were the three filtered out, so a link to
  them could not resolve: reported broken, and ``auto_fix`` built a stub beside the real
  page.

The filter now answers only the first question, and link resolution reads every ``.md``
file on disk.
"""

from tests.test_mutation_coordinator import _write_purpose_contract

from vector_lake.tool_lint import lint_vector_lake
from vector_lake.wiki_utils import get_memory_dir, get_wiki_dir

_FRONTMATTER = (
    "---\nid: {id}\ntitle: {title}\ntype: concept\ndomain: General\nstatus: Active\n"
    "epistemic-status: seed\ncategories: [Uncategorized]\nupdated: 2026-09-17\n"
    "sources: []\nstrategic_scope: core\nevidence_tier: derived\n---\n\n"
)


def _wiki():
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    return wiki


def _node_page(name: str, links: list[str]) -> None:
    body = "".join(f"- [[{link}]]\n" for link in links)
    stem = name[:-3]
    (_wiki() / name).write_text(
        _FRONTMATTER.format(id=f"c_{stem.lower()}", title=stem.replace("-", " "))
        + f"Body text.\n\n{body}",
        encoding="utf-8",
    )


def _report_page(name: str) -> None:
    """A generated artifact: no frontmatter, no node prefix, and it is not a node."""
    (_wiki() / name).write_text("# Generated report\n\n- 12 orphans\n", encoding="utf-8")


def test_a_generated_report_is_not_linted_as_a_node():
    _report_page("orphan_pages.md")
    _node_page("Concept_Alpha.md", [])

    report = lint_vector_lake()

    assert "2. Naming Compliance: [PASS]" in report
    assert "orphan_pages.md" not in report
    assert "Scanned: 1 files" in report


def test_a_link_to_a_generated_report_resolves():
    _report_page("index.md")
    _node_page("Concept_Alpha.md", ["index"])

    report = lint_vector_lake()

    assert "[[index]]" not in report
    assert "7. Broken Links: [PASS]" in report


def test_auto_fix_neither_renames_nor_stubs_around_a_generated_report():
    _write_purpose_contract(get_memory_dir())
    _report_page("orphan_pages.md")
    _report_page("index.md")
    _node_page("Concept_Alpha.md", ["orphan_pages", "index"])

    lint_vector_lake(auto_fix=True)

    wiki = _wiki()
    assert (wiki / "orphan_pages.md").exists(), "a generated report was renamed as a node"
    assert not (wiki / "Concept_orphan_pages.md").exists()
    assert not (wiki / "Concept_index.md").exists(), "a stub was invented beside a real page"


def test_a_link_to_a_missing_page_is_still_reported():
    """The checks above must not have been bought by disarming broken-link detection."""
    _node_page("Concept_Alpha.md", ["Totally_Missing"])

    report = lint_vector_lake()

    assert "[[Totally_Missing]]" in report
    assert "7. Broken Links: [FAIL" in report
