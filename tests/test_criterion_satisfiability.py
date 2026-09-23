"""The satisfiability instrument: it measures naming, and its negative result carries the weight.

The open question after three evaluation batches is whether the zero-recall queries have an answer
in the corpus at all.  This instrument answers that with a machine-checkable criterion rather than
another judging round, so two properties matter and are asserted here: it must not claim a page can
answer a query it merely shares a word with, and it must stay pre-registered -- the criterion, the
denominator and the declared limits live in the script's header, and a run without them is a
different instrument wearing the same name.

Loaded by path because ``benchmarks/`` is a directory of scripts, not a package.
"""

from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "criterion_satisfiability", REPO / "benchmarks" / "criterion_satisfiability.py"
)
instrument = importlib.util.module_from_spec(spec)
spec.loader.exec_module(instrument)


PAGES = [
    {"key": "Concept_医保通用名与医用耗材编码", "names": ["医保通用名与医用耗材编码", "Medical Insurance Naming"]},
    {"key": "Concept_信创融合平台", "names": ["信创融合平台"]},
]


def test_a_page_that_names_the_query_is_phrase_or_token_level():
    assert instrument.classify("医保通用名与医用耗材编码", PAGES)["verdict"] == "phrase"
    assert instrument.classify("医保通用名 医用耗材编码", PAGES)["verdict"] == "tokens"
    assert instrument.classify("Medical Insurance Naming", PAGES)["verdict"] == "phrase"


def test_sharing_one_word_is_not_a_match():
    """The loose probe the plan rejected: a title containing one query word is not an answer."""
    outcome = instrument.classify("医保通用名 与 医院数据平台", PAGES)

    assert outcome["verdict"] == "not_found", outcome


def test_a_query_nothing_names_is_the_load_bearing_result():
    outcome = instrument.classify("医院数据平台 信创 下一步", PAGES)

    assert outcome["verdict"] == "not_found"
    assert outcome["pages"] == []


def test_the_instrument_stays_preregistered():
    """The header is the instrument.  Losing it would make the numbers unreadable, not just undocumented."""
    source = (REPO / "benchmarks" / "criterion_satisfiability.py").read_text(encoding="utf-8")

    for required in ("# Pre-registration", "**Question.**", "**Instrument.**", "**Denominator.**",
                     "**What this instrument cannot say"):
        assert required in source, required
