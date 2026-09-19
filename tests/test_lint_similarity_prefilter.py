"""The similarity pass must prefilter by the ceiling of ``ratio()``, not by its value.

``ratio()`` is ``2 * matches / (len(a) + len(b))`` and ``matches`` is at most
``min(len(a), len(b))``, so ``2 * min / (min + max)`` is a hard ceiling on the score.
Comparing that ceiling before constructing a ``SequenceMatcher`` is exact: the 50-wide
sliding window over ~7 900 keys produced 375 283 comparisons on the live corpus, of
which only 120 210 could reach 0.91, and the pass was ~30 s of ``lint``.

These tests pin the two properties that make the shortcut safe: it never drops a pair
that would have been reported, and the pair it does drop is one ``ratio()`` can only
score below the threshold.
"""

from __future__ import annotations

from difflib import SequenceMatcher

import pytest

from vector_lake.tool_lint import SIMILARITY_MERGE_THRESHOLD, _ratio_can_exceed

# Names shaped like real corpus keys: short, CJK, and ASCII with digits.
NAME_PAIRS = [
    ("医院信息化", "医院信息化建设"),
    ("医院信息化", "医院"),
    ("HIS", "HIS"),
    ("HIS", "HIS2"),
    ("HIS", "HIS-"),
    ("DRG", "DIP"),
    ("电子病历六级", "电子病历五级"),
    ("电子病历六级", "六级电子病历"),
    ("Concept_1-X架构", "Concept_1-X框架"),
    ("a", "ab"),
    ("", "HIS"),
    ("", ""),
    ("互联互通", "互联互通标准化成熟度"),
    ("智慧服务", "智慧管理"),
]

# A spread of thresholds, including both sides of the live 0.91.
THRESHOLDS = [0.5, 0.75, 0.83, 0.835, 0.91, 0.95, 1.0]


@pytest.mark.parametrize(("name_a", "name_b"), NAME_PAIRS)
@pytest.mark.parametrize("threshold", THRESHOLDS)
def test_prefilter_never_drops_a_pair_that_would_be_reported(name_a, name_b, threshold):
    """If the score reaches the threshold, the prefilter must have let the pair through."""
    ratio = SequenceMatcher(None, name_a, name_b).ratio()
    if ratio > threshold:
        assert _ratio_can_exceed(name_a, name_b, threshold), (
            f"{name_a!r} vs {name_b!r} scores {ratio:.4f} > {threshold} but was skipped"
        )


@pytest.mark.parametrize(("name_a", "name_b"), NAME_PAIRS)
def test_ceiling_is_the_documented_bound(name_a, name_b):
    """Above the ceiling, the prefilter is allowed to skip; the bound is what it claims."""
    reached = _ratio_can_exceed(name_a, name_b, SIMILARITY_MERGE_THRESHOLD)
    ceiling = _ceiling(name_a, name_b)
    assert reached == (ceiling > SIMILARITY_MERGE_THRESHOLD)
    if not reached:
        assert SequenceMatcher(None, name_a, name_b).ratio() <= SIMILARITY_MERGE_THRESHOLD


def test_empty_pair_is_not_skipped():
    """``ratio()`` returns 1.0 for two empty names, so the shortcut must not claim it cannot."""
    assert _ratio_can_exceed("", "", SIMILARITY_MERGE_THRESHOLD)


def _ceiling(name_a: str, name_b: str) -> float:
    if not name_a and not name_b:
        return 1.0
    return 2.0 * min(len(name_a), len(name_b)) / (len(name_a) + len(name_b))
