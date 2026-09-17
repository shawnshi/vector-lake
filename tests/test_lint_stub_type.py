"""The lint auto-fix stub must declare the type its filename claims.

``lint --auto-fix`` (and the ``lint_vector_lake(auto_fix=True)`` tool) used to name a
stub after the target's own prefix while always declaring ``type: concept``.  The
write path validates the pair, so a broken ``[[Vendor_X]]`` built ``Vendor_X.md``
typed ``concept``, ``validate_schema`` refused it with "Filename prefix 'Vendor' does
not match frontmatter type 'concept'", and the exception was caught and logged.
The observable result was not an invalid page: the stub was **never created**, the
broken link stayed broken, and the lint report did not mention it, so the fix looked
like it had been attempted and merely had nothing to do.

These tests drive the real entry point and then run the written page back through
``validate_schema``, which is where the old stub failed before it could be written.
"""

from tests.test_mutation_coordinator import _write_purpose_contract

from vector_lake.schema_validator import validate_schema
from vector_lake.tool_lint import lint_vector_lake
from vector_lake.wiki_utils import get_memory_dir, get_wiki_dir, read_markdown_file


def _memory_root():
    return get_memory_dir()


def _source_page(links: list[str]) -> None:
    # The auto-fix writes through ``execute_mutation_plan``, which requires the
    # purpose contract; without it the write is refused and no stub appears.
    _write_purpose_contract(_memory_root())
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    body = "".join(f"- [[{link}]]\n" for link in links)
    (wiki / "Concept_Source-Page.md").write_text(
        "---\nid: c_sp\ntitle: Source\ntype: concept\ndomain: General\nstatus: Active\n"
        "epistemic-status: seed\ncategories: [Uncategorized]\nupdated: 2026-09-17\n"
        "sources: []\nstrategic_scope: core\nevidence_tier: derived\n---\n\n"
        f"Body text.\n\n{body}",
        encoding="utf-8",
    )


def test_autofix_stub_type_matches_its_filename_prefix(isolated_memory):
    _source_page(["Vendor_Missing-Thing"])

    lint_vector_lake(auto_fix=True)

    stub = get_wiki_dir() / "Vendor_Missing-Thing.md"
    assert stub.exists(), (
        "no stub was written; before the fix the mismatched type made validate_schema "
        "refuse the write, and the refusal was only logged"
    )
    frontmatter, body, _ = read_markdown_file(str(stub))
    assert frontmatter["type"] == "vendor"

    # The pair the old code got wrong.
    validate_schema(frontmatter, body, "Vendor_Missing-Thing.md")


def test_autofix_stub_uses_the_slot_its_type_declares(isolated_memory):
    _source_page(["Vendor_Missing-Thing"])

    lint_vector_lake(auto_fix=True)

    _, body, _ = read_markdown_file(str(get_wiki_dir() / "Vendor_Missing-Thing.md"))
    assert "### 组织架构与商业模式" in body
    assert "### 物理机制" not in body


def test_an_untyped_target_still_gets_a_concept_stub(isolated_memory):
    _source_page(["Bare-Thing"])

    lint_vector_lake(auto_fix=True)

    stub = get_wiki_dir() / "Concept_Bare-Thing.md"
    assert stub.exists()
    frontmatter, body, _ = read_markdown_file(str(stub))
    assert frontmatter["type"] == "concept"
    validate_schema(frontmatter, body, "Concept_Bare-Thing.md")
    assert "### 物理机制" in body


def test_autofix_does_not_materialise_a_generated_artifact(isolated_memory):
    """A page the indexer skips cannot resolve a link, so it must not be written.

    It would satisfy this very check while staying invisible to the graph, hiding the
    gap instead of reporting it.
    """
    _source_page(["System_Missing-Thing"])

    lint_vector_lake(auto_fix=True)

    assert not list(get_wiki_dir().glob("System_*.md"))


def test_a_type_without_declared_slots_still_validates(isolated_memory):
    """``source`` declares no H3 slots, so the stub falls back to the generic line.

    ``schema_validator`` only enforces slots for the types that declare them, so that
    fallback is legal -- worth pinning, because it is the case where the stub body and
    the type do not correspond.
    """
    _source_page(["Source_Missing-Thing"])

    lint_vector_lake(auto_fix=True)

    stub = get_wiki_dir() / "Source_Missing-Thing.md"
    assert stub.exists()
    frontmatter, body, _ = read_markdown_file(str(stub))
    assert frontmatter["type"] == "source"
    validate_schema(frontmatter, body, "Source_Missing-Thing.md")


def test_a_synthesis_target_is_still_not_stubbed(isolated_memory):
    """Recorded limitation, not an endorsement: the generic stub cannot satisfy it.

    ``schema_validator`` requires a Synthesis page to carry ``## 核心合成论点 (Core
    Synthesized Claims)`` and ``## 支撑拓扑 (Supporting Topology)``, which this body does
    not produce, so the write is refused.  That was already true before the type fix --
    the mismatch failed first -- so this pins the current behaviour so that a future
    change cannot start emitting invalid Synthesis pages unnoticed.
    """
    _source_page(["Synthesis_Missing-Thing"])

    lint_vector_lake(auto_fix=True)

    assert not (get_wiki_dir() / "Synthesis_Missing-Thing.md").exists()
