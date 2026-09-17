"""Retiring an earlier generation of community indexes.

The clustering daemon names its output ``System_Community_<level>_<uuid>.md``.
Its cleanup step used to glob ``System_Community_[0-9]*.md`` -- an underscore
followed by a digit -- which matches none of the artifacts a previous generation
wrote (``System_Community-L<level>-<suffix>.md``).  506 pages and ~41k canonical
rows keyed to them therefore survived indefinitely, and the 12 alias conflicts on
the live wiki were all one page from each generation claiming the same alias.

These tests pin the replacement predicate: the machine pattern is matched, and
hand-named pages, operator-acknowledged stubs and freshly written files are not.
"""

from scripts.community_clustering_daemon import superseded_community_artifacts


def test_machine_artifacts_of_an_earlier_generation_are_retired():
    """Both generations' machine spellings match, whatever the separator is."""
    names = [
        "System_Community-L0-0.md",
        "System_Community-L0-169.md",
        "System_Community-L1-d7beaf24.md",
        "System_Community_L0_eb7072ad.md",
        "System_Community_L1_ada6b608.md",
    ]

    assert superseded_community_artifacts(names, []) == sorted(names)


def test_hand_named_and_governed_pages_are_never_retired():
    """These carry a human or governance decision, so the pattern must not match."""
    names = [
        "System_Community-L0-Eroom-s-Law.md",
        "System_Community-L0-AI-Layoff-Trap.md",
        "System_Community-L0-Event-Driven-Real-time-Bus.md",
        "System_Community-L0-Vendor-深圳市南山区人民医院.md",
        "System_Community-L0.md",
        "System_Community.md",
        "Concept_Community-L1-d7beaf24.md",
    ]

    assert superseded_community_artifacts(names, []) == []


def test_a_run_never_retires_what_it_just_wrote():
    names = ["System_Community_L0_eb7072ad.md", "System_Community-L1-d7beaf24.md"]

    assert superseded_community_artifacts(names, [names[0]]) == [names[1]]
    assert superseded_community_artifacts(names, names) == []


def test_the_retired_set_is_sorted_and_deduplicated_by_construction():
    """Ordering is stable so a run cannot thrash the mutation batch ordering."""
    names = ["System_Community_L1_ada6b608.md", "System_Community-L0-0.md"]

    assert superseded_community_artifacts(names, []) == [
        "System_Community-L0-0.md",
        "System_Community_L1_ada6b608.md",
    ]
