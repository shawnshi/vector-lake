"""Query shaping: the keyword guess, and the cached expansion that must stay pure.

Neither changes ranking, which is why they are here and not in the measured switches.  Both were
"works but for a reason nobody wrote down" before:

* ``_classify_intent`` matched the substring ``"202"``, so a query containing those digits for any
  reason -- an identifier, a version, a price -- was called temporal and its decayed pages escaped
  the relevance penalty.  It is also not a classifier, and the name said it was.
* ``_expand_query_locally`` was ``lru_cache``d *and* mutated global tokenizer state, so the mutation
  happened on a cache miss and not on a hit, and the cache key covered the backend identity but not
  the dictionary state it changed.
"""

from __future__ import annotations

import pytest

from vector_lake import tool_search


@pytest.mark.parametrize(
    "query,expected",
    [
        ("2026 年 的 政策", "temporal"),
        ("2024", "temporal"),
        ("上周 的 会议", "temporal"),
        ("last week's numbers", "temporal"),
        ("是谁 负责 这个", "entity"),
        ("where is it", "entity"),
        ("简单 的一个 问题", "general"),
    ],
)
def test_the_keyword_guess_still_answers_the_same_way(query, expected):
    assert tool_search._keyword_intent(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "SNOMED 2023",  # a year, and also a version -- temporal either way, kept for contrast
        "HIS2020X",  # digits inside an identifier
        "1.202 kg",  # digits inside a decimal
        "产品 编号 1202",  # contains "202" but is not a year
    ],
)
def test_only_a_year_makes_a_query_temporal(query):
    """``"202"`` was a substring test, so it matched identifiers and decimals too."""
    lowered = query.lower()
    looks_temporal = tool_search._YEAR_PATTERN.search(lowered) is not None
    assert tool_search._keyword_intent(query) == ("temporal" if looks_temporal else "general")


def test_a_year_inside_a_longer_number_is_not_a_year():
    assert tool_search._YEAR_PATTERN.search("1202") is None
    assert tool_search._YEAR_PATTERN.search("20260") is None
    assert tool_search._YEAR_PATTERN.search("2026") is not None


def test_the_cached_expansion_is_pure(isolated_memory):
    """Same input, same output -- no side effect depends on whether the cache was hit."""
    first = tool_search._local_expansions("医院数据平台 信创")
    second = tool_search._local_expansions("医院数据平台 信创")
    assert first == second
    assert isinstance(first, tuple)

    tool_search._local_expansions.cache_clear()
    third = tool_search._local_expansions("医院数据平台 信创")
    assert third == first


def test_the_public_wrapper_hands_out_a_copy(isolated_memory):
    """A caller mutating the result must not corrupt the cache for everyone else."""
    result = tool_search._expand_query_locally("大模型 落地")
    assert isinstance(result, list)
    baseline = list(result)

    result.append("mutated")

    assert tool_search._expand_query_locally("大模型 落地") == baseline
