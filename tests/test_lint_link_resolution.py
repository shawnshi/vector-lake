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

from tests.test_stub_creator import _SOURCE, _VENDOR_PAGE, _pages, _wiki

from vector_lake import stub_creator, tool_lint, tool_query


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


# --- declarations, and the maps a rename invalidates --------------------------


def test_a_name_two_pages_declare_does_not_resolve(isolated_memory):
    """A declaration is usable only when exactly one page makes it.

    Titles and aliases used plain assignment into the link map, so when two pages claimed the
    same name the link resolved to whichever one ``os.listdir`` read last -- a coin flip that
    the report then presented as a fact.  Contested declarations now stay unresolved and the
    report says why.
    """
    twin = _VENDOR_PAGE.replace("id: 20260101_epic9", "id: 20260101_epic9").replace(
        "title: Epic", "title: Shared-Name"
    )
    other = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic8").replace(
        "type: vendor", "type: product"
    ).replace("title: Epic", "title: Shared-Name")
    wiki = _wiki(
        "Shared-Name",
        {"Vendor_Claim-A": twin, "Product_Claim-B": other},
    )

    report = _report()
    line = [l for l in report.splitlines() if "[[Shared-Name]]" in l]

    assert line, report
    assert "2 pages declare that name" in line[0]
    assert "Vendor_Claim-A" in line[0] and "Product_Claim-B" in line[0]


def test_a_declaration_does_not_displace_a_filename(isolated_memory):
    """The declaration used to overwrite the other page's own stem in the link map.

    With ``Concept_Target.md`` on disk and another page declaring ``title: Concept_Target``, a
    link ``[[Concept_Target]]`` resolved to the *declaring* page -- a link to a real file
    answered by a different one.
    """
    target = _VENDOR_PAGE.replace("id: 20260101_epic7", "id: 20260101_epic7").replace(
        "type: vendor", "type: concept"
    )
    claimer = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic6").replace(
        "title: Epic", "title: Concept_Target"
    )
    wiki = _wiki("Concept_Target", {"Concept_Target": target, "Vendor_Claimer": claimer})

    report = _report()

    assert "[[Concept_Target]]" not in report, _broken_lines(report)
    assert "7. Broken Links: [PASS]" in report
    # The discriminating part: credit must land on the page whose filename the link names.  Under
    # plain assignment the declaration won, so Concept_Target.md had zero inbound links and was
    # reported as an orphan.
    assert "Concept_Target.md: No inbound links (orphan)" not in report, [
        l for l in report.splitlines() if "orphan" in l.lower()
    ]


def test_a_rename_leaves_links_that_named_the_old_core_resolvable(isolated_memory):
    """An invariant guard, not a regression test: it passed before this batch too.

    I set out to fix a stale map here -- ``core_pages``/``unique_cores`` are built once and the
    naming auto-fix renames files mid-pass -- and could not construct a case where it matters,
    so the rebuild was dropped rather than kept as unobservable defence.  This is the property
    that makes it unnecessary: the rename rewrites ``[[Old]]`` and ``[[Old|alias]]`` to the new
    key, and adds the old core to the renamed page's aliases, so every remaining spelling of the
    old name still resolves -- through the alias route, which the *fresh* map reads from disk
    anyway.
    """
    wiki = _wiki("BadName")
    (wiki / "BadName.md").write_text(
        "---\nid: 20260101_bad1\ntitle: Bad Name\ntype: concept\ndomain: General\n"
        "status: Active\nepistemic-status: seed\ncategories: [Uncategorized]\n"
        "strategic_scope: edge\n"
        "updated: 2026-01-01T00:00:00Z\nsources: []\n---\n\n# Bad Name\n\n"
        "## 1. 编译事实\n*[System Directive]*\n\nx\n\n"
        "### 物理机制 (Mechanism)\n- x\n\n---\n\n"
        "## 2. 证据时间线\n- [2026-01-01] [Observation] x\n",
        encoding="utf-8",
    )

    tool_lint.lint_vector_lake(auto_fix=True)
    pages = _pages(wiki)

    assert "Concept_BadName.md" in pages, "the rename did not happen"
    report = tool_lint.lint_vector_lake(auto_fix=False)
    # The wiki holds a link spelling the *old* name, so this can fail; before the fix it resolved
    # against a stem that no longer existed and the report claimed a page nobody can open.
    assert "[[BadName]]" not in report, _broken_lines(report)
    assert "BadName.md" not in report, _broken_lines(report)




def test_a_contested_declaration_is_not_auto_fixed_into_a_third_page(isolated_memory):
    """Reporting it is the point; inventing a page for it would be the fork again.

    Neither claimant's *core* name matches the contested name, so the stub creator's
    covering-page guard cannot refuse -- measured on the live wiki, 31 names are in exactly
    that shape.  Lint therefore declines to attempt the write at all.
    """
    first = _VENDOR_PAGE.replace("id: 20260101_epic1", "id: 20260101_epic1").replace(
        "title: Epic", "title: Atrium Health"
    )
    second = _VENDOR_PAGE.replace("id: 20260101_epic2", "id: 20260101_epic2").replace(
        "type: vendor", "type: product"
    ).replace("title: Epic", "title: Atrium Health")
    wiki = _wiki("Atrium Health", {"Vendor_First": first, "Product_Second": second})

    report = tool_lint.lint_vector_lake(auto_fix=True)

    assert "[[Atrium Health]]" in report
    assert "2 pages declare that name" in report
    assert "Concept_Atrium-Health.md" not in _pages(wiki), "a third page was invented"
    assert _pages(wiki) == ["Concept_Source.md", "Product_Second.md", "Vendor_First.md"]


def test_a_contested_name_is_refused_whatever_spelling_the_link_uses(isolated_memory):
    """P1-1: the guard and the message looked the raw spelling up, so a hyphen bypassed both.

    ``title: Atrium Health`` and ``[[Atrium-Health]]`` are one name (``_``/``-``/space), so a
    literal lookup found no claimants: the link was mislabelled "target does not exist" and
    ``--auto-fix`` wrote ``Concept_Atrium-Health.md`` -- a third page for a contested name.
    """
    first = _VENDOR_PAGE.replace("title: Epic", "title: Atrium Health")
    second = _VENDOR_PAGE.replace("id: 20260101_epic2", "id: 20260101_epic2").replace(
        "type: vendor", "type: product"
    ).replace("title: Epic", "title: Atrium Health")
    wiki = _wiki("Atrium-Health", {"Vendor_First": first, "Product_Second": second})

    report = tool_lint.lint_vector_lake(auto_fix=True)
    line = [l for l in report.splitlines() if "[[Atrium-Health]]" in l]

    assert line, _broken_lines(report)
    assert "2 pages declare that name" in line[0], line[0]
    assert "Concept_Atrium-Health.md" not in _pages(wiki), "a third page was invented"


def test_the_query_side_also_refuses_a_contested_name(isolated_memory):
    """P1-2: the rule lived in one caller, and the other one wrote the third page.

    Neither claimant's core name matches the contested name, so the covering-page guard cannot
    refuse -- the refusal has to come from the owner, which both callers use.
    """
    first = _VENDOR_PAGE.replace("title: Epic", "title: Atrium Health")
    second = _VENDOR_PAGE.replace("id: 20260101_epic2", "id: 20260101_epic2").replace(
        "type: vendor", "type: product"
    ).replace("title: Epic", "title: Atrium Health")
    wiki = _wiki("Atrium-Health", {"Vendor_First": first, "Product_Second": second})

    created, refused = tool_query._generate_stubs_for_broken_links(
        str(wiki), {"Concept_Source.md"}
    )

    assert (created, refused) == (0, 0)
    assert "Concept_Atrium-Health.md" not in _pages(wiki), "the query path forked the name"


def test_the_query_side_with_nothing_to_do_returns_a_pair(isolated_memory):
    """The early exit returned a bare 0, which the caller unpacks -- a crash, not a count."""
    wiki = _wiki("Concept_Known")
    (wiki / "Concept_Known.md").write_text(_VENDOR_PAGE, encoding="utf-8")

    assert tool_query._generate_stubs_for_broken_links(str(wiki), {"Concept_Source.md"}) == (0, 0)


def test_a_core_is_compared_normalised_when_deciding_whether_a_page_exists(isolated_memory):
    """P1-2's other half: ``Vendor_Foo_Bar.md`` must cover ``[[Foo-Bar]]``.

    Cores were compared raw, so a stub was written beside the page it duplicated.
    """
    wiki = _wiki("Foo-Bar")
    (wiki / "Vendor_Foo_Bar.md").write_text(_VENDOR_PAGE, encoding="utf-8")

    assert stub_creator.covering_page("Concept_Foo-Bar", stub_creator.existence_index(str(wiki)))
    report = tool_lint.lint_vector_lake(auto_fix=True)

    assert "[[Foo-Bar]]" not in report, _broken_lines(report)
    assert "Concept_Foo-Bar.md" not in _pages(wiki)


def test_a_contested_alias_is_not_reported_as_contested_in_the_same_run(isolated_memory):
    """P1-3: check 3 strips the losing alias, so check 7 must not still call the name contested."""
    first = _VENDOR_PAGE.replace("title: Epic", "title: First Claimer")
    second = _VENDOR_PAGE.replace("id: 20260101_epic2", "id: 20260101_epic2").replace(
        "type: vendor", "type: product"
    ).replace("title: Epic", "title: Second Claimer")
    shared = "aliases: [Atrium Health]"
    first = first.replace("title: First Claimer", "title: First Claimer\n" + shared)
    second = second.replace("title: Second Claimer", "title: Second Claimer\n" + shared)
    wiki = _wiki("Atrium Health", {"Vendor_First": first, "Product_Second": second})

    report = tool_lint.lint_vector_lake(auto_fix=True)
    lines = report.splitlines()

    assert any("Alias 'Atrium Health' claimed by" in l for l in lines), lines[:12]
    assert not any("2 pages declare that name" in l for l in lines), [
        l for l in lines if "Atrium" in l
    ]
