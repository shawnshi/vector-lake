"""One owner for broken-link stubs, and the two defects that having two owners caused.

``tool_lint --auto-fix`` and ``tool_query`` used to create stubs with separate code.  Both
outcomes were wrong, and each is reproduced here against the *caller*, not just against the
shared function:

* ``tool_lint`` had no "a page already covers this name" check, so a link to
  ``[[Epic-Systems]]`` next to ``Vendor_Epic-Systems.md`` produced ``Concept_Epic-Systems.md``
  -- one entity as two nodes, each with its own merge debt;
* ``tool_query`` named the page ``<target>.md``, which is not a valid node filename, so its
  write was refused and the exception swallowed: it created nothing, for typed targets too.

Both callers now go through :mod:`vector_lake.stub_creator`, so the last test here compares
what they produce for the same link.
"""

import re

from tests.test_mutation_coordinator import _write_purpose_contract

from vector_lake import stub_creator, tool_lint, tool_query
from vector_lake.schema_validator import validate_schema
from vector_lake.wiki_utils import (
    get_memory_dir,
    get_wiki_dir,
    read_markdown_file,
    validate_wiki_filename,
)

_SOURCE = (
    "---\nid: Concept_Source\ntitle: Source\ntype: concept\ndomain: General\n"
    "status: Active\nepistemic-status: seed\ncategories: [Uncategorized]\n"
    # the rename path validates the schema, and a rename rewrites the pages that link to it
    "strategic_scope: edge\n"
    "updated: 2026-01-01T00:00:00Z\nsources: []\n---\n\n# Source\n\n"
    "## 1. 编译事实\n*[System Directive]*\n\n见 [[{link}]]。\n\n"
    "### 物理机制 (Mechanism)\n- x\n\n---\n\n## 2. 证据时间线\n- [2026-01-01] [Observation] x\n"
)

_VENDOR_PAGE = (
    "---\nid: 20260101_epic1\ntitle: Epic\ntype: vendor\ndomain: General\nstatus: Active\n"
    "epistemic-status: seed\ncategories: [Uncategorized]\n"
    "updated: 2026-01-01T00:00:00Z\nsources: []\n---\n\n# Epic\n\n"
    "## 1. 编译事实\n*[System Directive]*\n\nx\n\n### 物理机制 (Mechanism)\n- x\n\n"
    "---\n\n## 2. 证据时间线\n- [2026-01-01] [Observation] x\n"
)


def _wiki(link: str, extra: dict[str, str] | None = None):
    """A wiki holding one page that links to ``link``, plus any ``extra`` pages."""
    # ``write_markdown_file`` goes through the mutation coordinator, which is gated on the
    # purpose contract; without it every write is refused and nothing can be created.
    _write_purpose_contract(get_memory_dir())
    wiki = get_wiki_dir()
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Source.md").write_text(_SOURCE.format(link=link), encoding="utf-8")
    for name, text in (extra or {}).items():
        (wiki / f"{name}.md").write_text(text, encoding="utf-8")
    return wiki


def _pages(wiki):
    return sorted(p.name for p in wiki.glob("*.md"))


def _frontmatter(wiki, name):
    frontmatter, body, _ = read_markdown_file(wiki / name)
    return dict(frontmatter or {}), body


# --- the rules the owner encodes --------------------------------------------


def test_an_untyped_target_gets_a_prefix_so_the_page_is_a_node(isolated_memory):
    """A bare ``<target>.md`` is not a valid node name, which is why query created nothing."""
    assert stub_creator.stub_page_name("BrandNew-Thing") == "Concept_BrandNew-Thing.md"
    assert stub_creator.stub_page_name("Vendor_New-Thing") == "Vendor_New-Thing.md"
    assert stub_creator.stub_page_name("") is None


def test_a_generated_type_is_refused(isolated_memory):
    assert stub_creator.stub_page_name("System_Roadmap") is None
    outcome = stub_creator.create_stub(str(get_wiki_dir()), "System_Roadmap")
    assert outcome.stem is None and outcome.reason == "generated"


def test_a_page_under_another_prefix_blocks_the_write(isolated_memory):
    """The fork rule: ``Vendor_Epic-Systems`` covers ``Epic-Systems``."""
    wiki = _wiki("Epic-Systems", {"Vendor_Epic-Systems": _VENDOR_PAGE})

    outcome = stub_creator.create_stub(str(wiki), "Epic-Systems")
    assert outcome.stem is None and outcome.reason == "covered"
    assert _pages(wiki) == ["Concept_Source.md", "Vendor_Epic-Systems.md"]


def test_creating_twice_writes_once(isolated_memory):
    wiki = _wiki("BrandNew-Thing")
    index = stub_creator.existence_index(str(wiki))

    first = stub_creator.create_stub(str(wiki), "BrandNew-Thing", index)
    assert first.stem == "Concept_BrandNew-Thing" and first.reason == "created"
    second = stub_creator.create_stub(str(wiki), "BrandNew-Thing", index)
    assert second.stem is None and second.reason == "covered"
    assert _pages(wiki).count("Concept_BrandNew-Thing.md") == 1


def test_the_created_page_passes_the_schema(isolated_memory):
    wiki = _wiki("BrandNew-Thing")
    written = stub_creator.create_stub(str(wiki), "BrandNew-Thing").stem

    frontmatter, body = _frontmatter(wiki, f"{written}.md")
    validate_schema(frontmatter, body, f"{written}.md")
    assert frontmatter["type"] == "concept"
    assert frontmatter["tags"] == ["auto-stub"]
    assert {
        "id", "title", "type", "domain", "status", "epistemic-status", "categories",
        "updated", "sources",
    } <= set(frontmatter)
    assert "## 1. 编译事实" in body and "## 2. 证据时间线" in body
    # The id is generated, not the page name: 59 of 60 sampled live pages differ from theirs.
    assert frontmatter["id"] != written


def test_a_target_that_is_not_a_valid_filename_is_sanitised(isolated_memory):
    """A space, an underscore or a bracket must not leave the stub unwritable.

    Replacing them with an underscore would: the validator's strict pattern allows no
    underscore beyond the single one after the prefix, so ``Concept_Foo_Bar.md`` is refused.
    """
    for target, expected in (
        ("Foo Bar", "Concept_Foo-Bar.md"),
        ("Foo_Bar", "Concept_Foo-Bar.md"),
        ("Foo(Bar)", "Concept_Foo-Bar.md"),
        ("Vendor_New Thing", "Vendor_New-Thing.md"),
    ):
        name = stub_creator.stub_page_name(target)
        assert name == expected, (target, name)
        validate_wiki_filename(name)


def test_the_title_and_heading_are_the_core_name_not_the_stem(isolated_memory):
    """The prefix is how a page is *named on disk*, not part of its name.

    ``api``'s live pages carry their bare name as the title, and the title is one of the
    routes by which a link resolves, so ``title: Concept_BrandNew-Thing`` would register a
    name nothing ever links to.
    """
    wiki = _wiki("BrandNew-Thing")
    written = stub_creator.create_stub(str(wiki), "BrandNew-Thing").stem
    frontmatter, body = _frontmatter(wiki, f"{written}.md")

    assert frontmatter["title"] == "BrandNew-Thing"
    assert body.lstrip().startswith("# BrandNew-Thing")


def test_a_stub_plants_no_link_that_auto_fix_would_turn_into_a_page(isolated_memory):
    """The date is written bare, as the live wiki writes it.

    ``[[2026-09-18]]`` would be a broken link today and a junk ``Concept_2026-09-18.md`` the
    moment anybody ran lint ``--auto-fix`` -- the live wiki has no date-shaped page at all,
    and the 788 broken links it does have are not to be added to by the fix for broken links.
    """
    wiki = _wiki("BrandNew-Thing")
    written = stub_creator.create_stub(str(wiki), "BrandNew-Thing").stem
    _, body = _frontmatter(wiki, f"{written}.md")

    assert "[[20" not in body, body
    assert "Last Reshaped: 20" in body
    # The one link it plants is its own page, which exists.
    assert [link for link in _links(body)] == [written]


def _links(body: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body)
    ]


def test_the_id_shape_matches_the_live_wikis_convention(isolated_memory):
    wiki = _wiki("BrandNew-Thing")
    written = stub_creator.create_stub(str(wiki), "BrandNew-Thing").stem
    frontmatter, _ = _frontmatter(wiki, f"{written}.md")

    stamp, _, suffix = frontmatter["id"].partition("_")
    assert len(stamp) == 8 and stamp.isdigit()
    assert len(suffix) == 6


# --- the same stub from either caller ---------------------------------------


def test_lint_auto_fix_does_not_fork_an_existing_entity(isolated_memory):
    """The defect this merge removes: lint used to invent ``Concept_Epic-Systems.md``."""
    wiki = _wiki("Epic-Systems", {"Vendor_Epic-Systems": _VENDOR_PAGE})

    tool_lint.lint_vector_lake(auto_fix=True)

    assert "Concept_Epic-Systems.md" not in _pages(wiki), "lint forked one entity into two nodes"


def test_both_callers_ask_the_one_owner_for_the_same_page(isolated_memory, monkeypatch):
    """The merge's point: one link, one page, whichever caller got there first.

    Both runs use the configured wiki directory, in that order, removing the stub between
    them: the mutation coordinator confines writes to that directory ("path traversal
    blocked" for anything else), so a second directory cannot be used to compare them.
    """
    calls: list[str] = []
    real = stub_creator.create_stub

    def spy(wiki_dir, target, index=None, contested=None):
        calls.append(target)
        return real(wiki_dir, target, index, contested=contested)

    # Both callers reach the owner through the module attribute, so one patch covers both.
    monkeypatch.setattr(stub_creator, "create_stub", spy)
    wiki = _wiki("BrandNew-Thing")
    stub_path = wiki / "Concept_BrandNew-Thing.md"

    created_by_query, refused_by_query = tool_query._generate_stubs_for_broken_links(
        str(wiki), {"Concept_Source.md"}
    )
    assert (created_by_query, refused_by_query) == (1, 0), "the query-side creator wrote nothing"
    assert calls == ["BrandNew-Thing"], "query does not ask the shared owner"
    query_fm, query_body = _frontmatter(wiki, stub_path.name)

    # Same link, same wiki, other caller: delete the artifact and let lint find the link.
    stub_path.unlink()
    calls.clear()
    tool_lint.lint_vector_lake(auto_fix=True)

    assert calls == ["BrandNew-Thing"], "lint does not ask the shared owner"
    assert stub_path.exists(), "lint no longer fixes the broken link at all"
    lint_fm, lint_body = _frontmatter(wiki, stub_path.name)

    # The id is derived from the page name, so even that field is the same page: the two callers
    # produce byte-identical output for one link.
    assert query_fm == lint_fm
    assert query_body == lint_body


# --- a closed gate is not "nothing to do" ------------------------------------


def test_a_refused_write_says_so(isolated_memory, monkeypatch):
    """The defect this replaces: a systemic refusal looked exactly like an empty wiki.

    ``write_markdown_file`` is where the gates live -- the purpose contract, the coordinator,
    the path check -- so patching it to raise stands in for all of them being closed at once.
    """
    wiki = _wiki("BrandNew-Thing")

    def refuse(*args, **kwargs):
        raise RuntimeError("Path traversal blocked")

    monkeypatch.setattr(stub_creator, "write_markdown_file", refuse)
    outcome = stub_creator.create_stub(str(wiki), "BrandNew-Thing")

    assert outcome.stem is None
    assert outcome.reason == "refused"
    assert outcome.refused is True, "a closed gate must be distinguishable from no work"
    assert _pages(wiki) == ["Concept_Source.md"]


def test_lint_reports_refused_writes_instead_of_a_bare_zero(isolated_memory, monkeypatch):
    """``Auto-fixed: 0`` alone reads as "nothing needed fixing", which is the wrong story."""
    wiki = _wiki("BrandNew-Thing")

    def refuse(*args, **kwargs):
        raise RuntimeError("purpose contract missing")

    monkeypatch.setattr(stub_creator, "write_markdown_file", refuse)
    report = tool_lint.lint_vector_lake(auto_fix=True)

    assert "Stub writes refused: 1" in report, report.splitlines()[1]
    assert "Auto-fixed: 0" in report
    assert _pages(wiki) == ["Concept_Source.md"]


def test_query_reports_refused_writes_instead_of_silence(isolated_memory, monkeypatch):
    wiki = _wiki("BrandNew-Thing")

    def refuse(*args, **kwargs):
        raise RuntimeError("purpose contract missing")

    monkeypatch.setattr(stub_creator, "write_markdown_file", refuse)
    created, refused = tool_query._generate_stubs_for_broken_links(str(wiki), {"Concept_Source.md"})

    assert (created, refused) == (0, 1), "a refused write was counted as nothing to do"


# --- the stub id --------------------------------------------------------------


def test_the_stub_id_is_derived_from_the_page_name(isolated_memory):
    """Two stubs cannot collide by being created in the same second.

    The id used to be drawn at random, so it consulted nothing: a burst of stubs could collide
    (lint's duplicate-id check would have found it later, on a wiki nobody had said was wrong),
    and re-creating the same stub produced a different id every time.  Deriving it from the page
    name makes it stable and removes the timing dependence.  The residual -- two different names
    hashing to the same six base36 characters -- is stated in ``generate_id`` and is what lint's
    duplicate-id check would still catch.
    """
    wiki = _wiki("BrandNew-Thing")
    first = stub_creator.generate_id("Concept_BrandNew-Thing", "2026-09-18")

    assert stub_creator.generate_id("Concept_BrandNew-Thing", "2026-09-18") == first
    assert stub_creator.generate_id("Concept_Other-Thing", "2026-09-18") != first
    assert stub_creator.generate_id("Concept_BrandNew-Thing", "2026-09-19") != first

    written = stub_creator.create_stub(str(wiki), "BrandNew-Thing").stem
    frontmatter, _ = _frontmatter(wiki, f"{written}.md")
    assert frontmatter["id"] == stub_creator.generate_id(written, frontmatter["created"][:10])
