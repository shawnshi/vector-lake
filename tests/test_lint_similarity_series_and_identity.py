"""Two rules the similarity pass was missing, in opposite directions.

The pass compared *names* and called the result a duplicate, which failed both ways at once:

* **It reported a naming convention as duplication.**  On the live corpus 3 782 of the 3 797
  pairs were keys differing only in their numbers -- ``Source_intelligence-20260219-briefing``
  against ``...-20260220-...``, ``Event_南湖HIT论坛-2021`` against ``-2022``.  Those are
  distinct entities that share a filing convention.  Digits are folded before the comparison,
  so members of one series are no longer called duplicates of each other.
* **It could not see the corpus's dominant duplication shape at all.**  ``prefix_a != prefix_b:
  continue`` refuses every cross-type pair, and the window is positional, so ``Concept_DRG-DIP``
  and ``Policy_DRG-DIP`` -- the same name filed under two types -- were excluded twice over.
  Same name under a different type is an identity collision, not near-name similarity, so it is
  grouped by the identity key the rest of the lake resolves links by.

The prefilter (``_ratio_can_exceed``) and the threshold are deliberately untouched;
``test_lint_similarity_prefilter.py`` pins those.

Measured on the live wiki: 3 797 reported pairs -> 57 (42 identity collisions + 15 survivors of
the window), with 3 782 pairs excluded as one naming series.
"""

from __future__ import annotations

from tests.test_stub_creator import _wiki

from vector_lake import tool_lint


def _page(stem: str, node_type: str, title: str) -> str:
    """A minimal schema-valid node, so only the similarity check is under test."""
    return (
        f"---\nid: {stem}\ntitle: {title}\ntype: {node_type}\ndomain: General\n"
        "status: Active\nepistemic-status: seed\ncategories: [Uncategorized]\n"
        "strategic_scope: edge\nupdated: 2026-01-01T00:00:00Z\nsources: []\n---\n\n"
        f"# {title}\n\n## 1. 编译事实\n*[System Directive]*\n\nx\n\n"
        "### 物理机制 (Mechanism)\n- x\n\n---\n\n## 2. 证据时间线\n- [2026-01-01] [Observation] x\n"
    )


def _similarity_line(report: str) -> str:
    return next(line for line in report.splitlines() if line.startswith("9. Filename Similarity"))


def _report() -> str:
    return tool_lint.lint_vector_lake(auto_fix=False)


# --- the helpers, as pure functions -----------------------------------------


def test_naming_series_key_folds_numbers_prefix_and_case():
    assert (
        tool_lint._naming_series_key("Source_intelligence-20260219-briefing")
        == "intelligence-#-briefing"
    )
    assert tool_lint._naming_series_key("Event_南湖HIT论坛-2021") == tool_lint._naming_series_key(
        "Event_南湖HIT论坛-2022"
    )
    # Spelling is not identity: the corpus carries both spellings of the same convention, and a
    # raw-key comparison called every pair of them a duplicate.
    assert tool_lint._naming_series_key("Source_Intelligence-20260715-Briefing") == (
        tool_lint._naming_series_key("Source_intelligence-20260315-briefing")
    )
    # The type prefix is filing, not naming.
    assert tool_lint._naming_series_key("Concept_Agentic-Workflow") == (
        tool_lint._naming_series_key("Product_Agentic-Workflow")
    )
    # A difference that is not a number must survive: these are different names, not one series.
    assert tool_lint._naming_series_key("Concept_Software-1-0") != tool_lint._naming_series_key(
        "Concept_SoftwareX-1-0"
    )


def test_type_prefix_is_the_filing_type_not_the_whole_stem():
    assert tool_lint._type_prefix("Concept_HIS") == "Concept"
    assert tool_lint._type_prefix("Source_2604.24658") == "Source"
    assert tool_lint._type_prefix("untyped") == ""


# --- rule 1: one naming series is not duplication ---------------------------


def test_a_date_series_is_not_reported_as_duplicates(isolated_memory):
    """The false positive that made the check useless: 3 782 of 3 797 on the live corpus."""
    _wiki(
        "Concept_A",
        {
            "Concept_Software-1-0": _page("Concept_Software-1-0", "concept", "Software 1.0"),
            "Concept_Software-2-0": _page("Concept_Software-2-0", "concept", "Software 2.0"),
        },
    )

    report = _report()

    assert "[PASS]" in _similarity_line(report), _similarity_line(report)
    # The exclusion must be visible: a PASS that silently dropped 3 782 pairs reads as "nothing
    # was found", which is not what happened.
    assert "excluded as one naming series" in _similarity_line(report)


def test_only_a_pure_number_difference_counts_as_one_series(isolated_memory):
    """Discriminates against folding too much: ``SoftwareX`` is a different name."""
    _wiki(
        "Concept_A",
        {
            "Concept_Software-1-0": _page("Concept_Software-1-0", "concept", "Software 1.0"),
            "Concept_SoftwareX-1-0": _page("Concept_SoftwareX-1-0", "concept", "SoftwareX 1.0"),
        },
    )

    report = _report()

    assert "[FAIL: 1]" in _similarity_line(report), _similarity_line(report)


# --- rule 2: the same name under two types is a collision -------------------


def test_one_name_under_two_type_prefixes_is_reported(isolated_memory):
    """The dominant duplication shape, which the type-prefix guard could never see."""
    _wiki(
        "Concept_A",
        {
            "Concept_DRG-DIP": _page("Concept_DRG-DIP", "concept", "DRG/DIP"),
            "Policy_DRG-DIP": _page("Policy_DRG-DIP", "policy", "DRG/DIP"),
        },
    )

    report = _report()

    assert "[FAIL: 1]" in _similarity_line(report), _similarity_line(report)
    assert "different type prefix: Concept vs Policy" in report
    assert "Concept_DRG-DIP.md <-> Policy_DRG-DIP.md" in report


def test_a_cross_type_pair_that_is_only_a_number_series_is_still_excluded(isolated_memory):
    """The two rules must compose, not take turns."""
    _wiki(
        "Concept_A",
        {
            "Concept_2026年报": _page("Concept_2026年报", "concept", "年报"),
            "Event_2027年报": _page("Event_2027年报", "event", "年报"),
        },
    )

    report = _report()

    assert "[PASS]" in _similarity_line(report), _similarity_line(report)


def test_the_same_name_under_one_type_is_left_to_the_name_pass(isolated_memory):
    """The identity pass groups cross-type only; a same-type pair is the window's job."""
    _wiki(
        "Concept_A",
        {
            "Concept_Agentic-AI": _page("Concept_Agentic-AI", "concept", "Agentic AI"),
            "Concept_Agentic-API": _page("Concept_Agentic-API", "concept", "Agentic API"),
        },
    )

    report = _report()

    line = _similarity_line(report)
    assert "[FAIL: 1]" in line, line
    assert "different type prefix" not in report


def test_a_cross_type_pair_keeps_its_digits(isolated_memory):
    """The series rule must not leak into the identity pass.

    ``Concept_2023全国深化医改经验推广会`` and ``Event_2023全国深化医改经验推广会`` differ only
    in their type prefix, so they are one thing and the digits are part of its name.  Screening the
    identity pass by naming series -- where the same name trivially shares a series -- would hide
    exactly the duplicates that pass exists to find.
    """
    _wiki(
        "Concept_A",
        {
            "Concept_2023全国深化医改经验推广会": _page(
                "Concept_2023全国深化医改经验推广会", "concept", "2023全国深化医改经验推广会"
            ),
            "Event_2023全国深化医改经验推广会": _page(
                "Event_2023全国深化医改经验推广会", "event", "2023全国深化医改经验推广会"
            ),
        },
    )

    report = _report()

    assert "different type prefix: Concept vs Event" in report, report


def test_a_previously_missed_near_pair_is_now_found(isolated_memory):
    """The window's blind spot: these sit ~1 000 positions apart among 3 983 concept pages."""
    _wiki(
        "Concept_A",
        {
            "Concept_LLM-as-a-Judge": _page("Concept_LLM-as-a-Judge", "concept", "LLM as a Judge"),
            "Concept_VLM-as-a-judge": _page("Concept_VLM-as-a-judge", "concept", "VLM as a Judge"),
        },
    )

    report = _report()

    assert "[FAIL: 1]" in _similarity_line(report), _similarity_line(report)
    assert "Concept_LLM-as-a-Judge.md <-> Concept_VLM-as-a-judge.md" in report


def test_the_same_pair_is_found_whichever_way_round_the_loop_reaches_it(isolated_memory):
    """A band that only looks one direction would halve the findings silently."""
    _wiki(
        "Concept_A",
        {
            "Concept_Transformer": _page("Concept_Transformer", "concept", "Transformer"),
            "Concept_循环Transformer": _page("Concept_循环Transformer", "concept", "循环 Transformer"),
        },
    )

    report = _report()

    assert "[FAIL: 1]" in _similarity_line(report), _similarity_line(report)
