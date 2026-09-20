"""What check 9 reports, and what it deliberately does not.

The pass compared *names* and called the result a duplicate, which failed in three ways at once:

* **It reported a naming convention as duplication.**  On the live corpus 3 782 of the 3 797
  pairs were keys differing only in their numbers -- ``Source_intelligence-20260219-briefing``
  against ``...-20260220-...``, ``Event_南湖HIT论坛-2021`` against ``-2022``.  Those are distinct
  entities sharing a filing convention.  Digits are now folded before the distinction is drawn,
  so a series is counted as a series instead of reported as a duplicate.
* **It could not see the shape this corpus actually duplicates in.**  ``prefix_a != prefix_b:
  continue`` refuses every cross-type pair, and the window is positional, so ``Concept_DRG-DIP``
  and ``Policy_DRG-DIP`` -- the same name filed under two types -- were excluded twice over.
  That is an identity collision, not near-name similarity, so it is grouped by the identity key
  the rest of the lake resolves links by.
* **The window dropped 2 262 of the 6 058 threshold-passing pairs (37%)**, and the misses were
  the ones worth reading -- ``Concept_LLM-as-a-Judge`` against ``Concept_VLM-as-a-judge`` sits
  about 1 000 positions away among 3 983 concept pages.  It is replaced by three exact upper
  bounds (length band, character mask, character multiset), which is the whole of what is
  asserted below: a brute-force pass over the same band is the oracle.

And the check now reports name shape rather than answering "are these duplicates", which is
``find_merge_candidates``'s question and lands in the governance queue.  The summary says so.

The prefilter and the threshold are pinned elsewhere (``test_lint_similarity_prefilter.py``).
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


def _similarity_block(report: str) -> list[str]:
    """The check-9 section: header, summary lines, then samples."""
    lines = report.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("9. Name Collisions"))
    block: list[str] = []
    for line in lines[start:]:
        if block and not line.strip():
            break
        block.append(line)
    return block


def _similarity_header(report: str) -> str:
    return _similarity_block(report)[0]


def _similarity_summary(report: str) -> str:
    """Everything in check 9 that is not a sample finding."""
    return "\n".join(line for line in _similarity_block(report) if "collision:" not in line)


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


def test_family_sizes_are_components_not_pairs():
    """Three names in a chain are one family, and the headline must say one, not three."""
    assert tool_lint._family_sizes([("a", "b"), ("b", "c")]) == [3]
    assert tool_lint._family_sizes([("a", "b"), ("c", "d")]) == [2, 2]
    assert tool_lint._family_sizes([]) == []


def test_histogram_reads_largest_first():
    assert tool_lint._histogram([4, 3, 2, 2, 1]) == "4×1, 3×1, 2×2, 1×1"
    assert tool_lint._histogram([]) == ""


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

    assert "[PASS]" in _similarity_header(report), _similarity_header(report)
    # The exclusion must be visible: a PASS that silently dropped 3 782 pairs reads as "nothing
    # was found", which is not what happened.
    assert "excluded as one naming convention: 1 pairs in 1 families" in _similarity_summary(report)


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

    assert "[FAIL: 1]" in _similarity_header(report), _similarity_header(report)


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

    assert "[FAIL: 1]" in _similarity_header(report), _similarity_header(report)
    assert "Identity collision: Concept_DRG-DIP.md <-> Policy_DRG-DIP.md" in report
    assert "different type prefix: Concept vs Policy" in report


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

    assert "[PASS]" in _similarity_header(report), _similarity_header(report)


def test_the_same_name_under_one_type_is_left_to_the_name_pass(isolated_memory):
    """The identity pass groups cross-type only; a same-type pair is the name pass's job."""
    _wiki(
        "Concept_A",
        {
            "Concept_Agentic-AI": _page("Concept_Agentic-AI", "concept", "Agentic AI"),
            "Concept_Agentic-API": _page("Concept_Agentic-API", "concept", "Agentic API"),
        },
    )

    report = _report()

    assert "[FAIL: 1]" in _similarity_header(report), _similarity_header(report)
    assert "Identity collision" not in report
    assert "Name collision: Concept_Agentic-AI.md" in report


def test_a_cross_type_pair_keeps_its_digits(isolated_memory):
    """The series rule must not leak into the identity pass.

    ``Concept_2023全国深化医改经验推广会`` and ``Event_2023全国深化医改经验推广会`` differ only
    in their type prefix, so they are one thing and the digits are part of its name.  Screening the
    identity pass by naming series -- where the same name trivially shares a series -- would hide
    exactly the collisions that pass exists to find.
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


# --- rule 3: the window's blind spot is closed ------------------------------


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

    assert "[FAIL: 1]" in _similarity_header(report), _similarity_header(report)
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

    assert "[FAIL: 1]" in _similarity_header(report), _similarity_header(report)


# --- the report says what the check is, and who decides ---------------------


def test_the_check_reports_name_shape_and_stops_short_of_duplication(isolated_memory):
    """It no longer answers a question that has an owner elsewhere."""
    _wiki(
        "Concept_A",
        {
            "Concept_DRG-DIP": _page("Concept_DRG-DIP", "concept", "DRG/DIP"),
            "Policy_DRG-DIP": _page("Policy_DRG-DIP", "policy", "DRG/DIP"),
        },
    )

    report = _report()

    assert "9. Name Collisions" in report
    assert "Duplicate:" not in report
    assert "merge decisions belong to merge_suggestions_vector_lake" in report


def test_findings_are_summarised_as_families_not_sample_lines(isolated_memory):
    """Three names in a chain is one decision, and the summary is where that shows."""
    _wiki(
        "Concept_A",
        {
            "Concept_Transformer": _page("Concept_Transformer", "concept", "Transformer"),
            "Concept_Transformers": _page("Concept_Transformers", "concept", "Transformers"),
            "Concept_Transformerz": _page("Concept_Transformerz", "concept", "Transformerz"),
        },
    )

    report = _report()
    summary = _similarity_summary(report)

    assert "families: 1 over 3 names" in summary, summary
    assert "sizes 3×1" in summary, summary


def test_the_sample_list_is_cut_short_when_there_is_a_summary(isolated_memory):
    """Ten samples out of thousands is what made the number unreadable."""
    _wiki(
        "Concept_A",
        {
            f"Concept_Near-Case-{index}": _page(
                f"Concept_Near-Case-{index}", "concept", f"Near Case {index}"
            )
            for index in range(8)
        },
    )

    report = _report()
    block = _similarity_block(report)

    assert sum(1 for line in block if "collision:" in line) <= 5, block
