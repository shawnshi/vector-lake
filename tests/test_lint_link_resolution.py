"""A link that names a page by its core name points at that page.

``[[Epic-Systems]]`` and ``Vendor_Epic-Systems.md`` are the same node: the prefix is how a
page is filed, not part of what it is called.  The stub creator has always decided
"does a page already cover this target?" that way (``covering_page``), but lint's link
resolution matched only filenames, titles and aliases -- so a link by core name was reported
broken while a page already answered it, and the two halves of the same question disagreed.

Measured on the live wiki before the change: 24 distinct targets, 67 link occurrences, of
which ``[[Concept_CoMET]]`` (only ``Product_CoMET`` exists), ``[[Concept_Art]]``
(``Product_Art``), ``[[Concept_DICOM]]`` (``Standard_DICOM``) and ``[[刘宁]]`` (``Person_刘宁``)
are representative.  ``--auto-fix`` used to "fix" such a link by creating a second page beside
the real one; the fork guard added with the stub creator now refuses that write, which left
the link reported broken on every run with nothing that could ever fix it -- the state these
tests pin shut.

An *ambiguous* core name is deliberately not resolved: two pages sharing a core is a real
defect, and picking the alphabetically first would hide it.
"""

from tests.test_stub_creator import _VENDOR_PAGE, _pages, _wiki

from vector_lake import tool_lint


def _broken_lines(report: str) -> list[str]:
    return [line for line in report.splitlines() if "->" in line and "[[" in line]


def _report() -> str:
    return tool_lint.lint_vector_lake(auto_fix=False)


def test_a_link_by_core_name_is_not_reported_broken(isolated_memory):
    """The false positive: a page answers the link, under a different prefix."""
    wiki = _wiki("Epic-Systems", {"Vendor_Epic-Systems": _VENDOR_PAGE})

    report = _report()

    assert "[[Epic-Systems]]" not in report, _broken_lines(report)
    assert "7. Broken Links: [PASS]" in report


def test_such_a_link_needs_no_stub_at_all(isolated_memory):
    """An invariant guard, not a regression test for this change: it passes either way.

    The fork guard (``stub_creator.covering_page``) already refuses the write without core
    name resolution, which is why this passed before the change too.  It is kept because the
    two together are what the report means: not reported, and not "fixed" by writing a page.
    """
    wiki = _wiki("Epic-Systems", {"Vendor_Epic-Systems": _VENDOR_PAGE})

    tool_lint.lint_vector_lake(auto_fix=True)

    assert _pages(wiki) == ["Concept_Source.md", "Vendor_Epic-Systems.md"]


def test_an_ambiguous_core_name_stays_broken(isolated_memory):
    """Discriminates the implementation I rejected, not the pre-change behaviour.

    Resolving ambiguous cores to the alphabetically first page makes exactly this test fail
    (verified by mutating the map build to disregard the uniqueness check); leaving it broken
    keeps a duplication defect visible instead of hiding it behind a plausible answer.
    """
    duplicate = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic2").replace(
        "type: vendor", "type: event"
    )
    wiki = _wiki(
        "Epic-Systems",
        {"Vendor_Epic-Systems": _VENDOR_PAGE, "Event_Epic-Systems": duplicate},
    )

    report = _report()
    tool_lint.lint_vector_lake(auto_fix=True)

    assert "[[Epic-Systems]]" in report, "an ambiguous name was resolved to one of its pages"
    assert _pages(wiki) == [
        "Concept_Source.md",
        "Event_Epic-Systems.md",
        "Vendor_Epic-Systems.md",
    ], "a third page was created for an already-ambiguous name"


def test_a_title_cannot_resolve_an_ambiguous_core_name(isolated_memory):
    """The uniqueness rule must survive the title route, not just the core-key map.

    Resolving by stripping the target's prefix and looking it up in the general map (titles,
    aliases, filenames together) makes exactly this case resolve: ``Concept_Epic-Systems``
    strips to ``Epic-Systems``, one of the two contested pages declares that as its title, and
    the lookup silently picks it.  Measured on the live wiki that mistake resolved 42 links.
    """
    contested = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic2").replace(
        "type: vendor", "type: event"
    ).replace("title: Epic", "title: Epic-Systems")
    wiki = _wiki(
        "Concept_Epic-Systems",
        {"Vendor_Epic-Systems": _VENDOR_PAGE, "Event_Epic-Systems": contested},
    )

    report = _report()

    assert "[[Concept_Epic-Systems]]" in report, "a title resolved an ambiguous core name"
    assert "7. Broken Links: [PASS]" not in report
    assert len(_pages(wiki)) == 3


def test_a_target_that_needed_sanitising_still_resolves(isolated_memory):
    """The closure has to hold for targets the creator had to rewrite, or it is not a closure.

    ``[[Foo Bar]]`` produced ``Concept_Foo-Bar.md``.  Compared literally, the raw target
    matched neither the filename nor the core name, so the link stayed reported broken on
    every run while the stub that would fix it made the creator skip its own write silently:
    reported broken forever with nothing that could fix it.  Both sides are normalised now.
    """
    wiki = _wiki("Foo Bar")

    first = tool_lint.lint_vector_lake(auto_fix=True)
    assert "[[Foo Bar]]" in first
    assert "Concept_Foo-Bar.md" in _pages(wiki)

    second = tool_lint.lint_vector_lake(auto_fix=False)

    assert "[[Foo Bar]]" not in second, _broken_lines(second)
    assert "7. Broken Links: [PASS]" in second


def test_underscore_and_hyphen_are_one_name_for_the_uniqueness_guard(isolated_memory):
    """Otherwise the guard reads two spellings as two clean cores and resolves both.

    ``Concept_Foo-Bar.md`` and ``Product_Foo_Bar.md`` are the same name written two ways, which
    the rest of the wiki already treats as one (``normalize_entity_name``).  Compared raw, each
    would look unique and a link of either spelling would resolve to a different page -- the
    hidden-duplication outcome the guard exists to prevent.
    """
    twin = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic3").replace(
        "type: vendor", "type: product"
    ).replace("title: Epic", "title: Foo Bar")
    wiki = _wiki(
        "Concept_Foo-Bar",
        {"Vendor_Foo-Bar": _VENDOR_PAGE.replace("title: Epic", "title: Foo-Bar"),
         "Product_Foo_Bar": twin},
    )

    report = _report()

    assert "[[Concept_Foo-Bar]]" in report, "two spellings of one name were resolved separately"
    assert len(_pages(wiki)) == 3


def test_a_link_to_a_generated_page_is_not_resolved_by_core_name(isolated_memory):
    """A System_ page is not a node: resolving to it would hide the gap, not close it.

    The stub creator refuses to *create* one for exactly this reason (the indexer deletes such
    pages from the graph), so the resolver must not quietly accept one either.  Writing the
    exact spelling still finds it, as it always did.
    """
    system = _VENDOR_PAGE.replace("Vendor_", "").replace("id: 20260101_epic1", "id: 20260101_sys1")
    wiki = _wiki("Magic", {"System_Magic": system})

    report = _report()

    assert "[[Magic]]" in report, "a link resolved to a page the graph does not contain"


def test_a_contested_core_name_names_its_pages(isolated_memory):
    """"target does not exist" is false when the name is contested, and it diagnoses nothing."""
    contested = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic4").replace(
        "type: vendor", "type: event"
    )
    wiki = _wiki(
        "Epic-Systems",
        {"Vendor_Epic-Systems": _VENDOR_PAGE, "Event_Epic-Systems": contested},
    )

    report = _report()
    line = [l for l in report.splitlines() if "[[Epic-Systems]]" in l]

    assert line, report
    assert "2 pages share that name" in line[0]
    assert "Vendor_Epic-Systems" in line[0] and "Event_Epic-Systems" in line[0]

def test_creating_a_stub_actually_fixes_the_link(isolated_memory):
    """The closure: the second run must be clean, or the fix never was one.

    Two independent routes make this hold, and the test fails only when *both* are removed
    (verified by mutating each and then both): the stub's ``title`` is the core name, which
    lint accepts as a link target, and the core-name rule accepts the page name itself.  Both
    routes exist because of the two batches either side of this one -- the title fix and this
    resolution -- so the assertion is deliberately about the outcome rather than about which
    of them did it.
    """
    wiki = _wiki("BrandNew-Thing")

    first = tool_lint.lint_vector_lake(auto_fix=True)
    assert "Concept_BrandNew-Thing.md" in _pages(wiki)
    assert "[[BrandNew-Thing]]" in first

    second = tool_lint.lint_vector_lake(auto_fix=False)

    assert "[[BrandNew-Thing]]" not in second, _broken_lines(second)
    assert "7. Broken Links: [PASS]" in second


def test_a_genuinely_missing_target_is_still_reported_and_still_fixed(isolated_memory):
    """Coverage guard: the rule must not turn the broken-link check off (passes either way)."""
    wiki = _wiki("NoSuchPageAnywhere")

    report = _report()

    assert "[[NoSuchPageAnywhere]]" in report
    tool_lint.lint_vector_lake(auto_fix=True)
    assert "Concept_NoSuchPageAnywhere.md" in _pages(wiki)
