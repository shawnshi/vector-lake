import pytest

from vector_lake.timeline_semantics import parse_timeline_prefix, validate_timeline_date


@pytest.mark.parametrize(
    ("text", "event_date", "tag", "description", "precision"),
    [
        ("[2024-02-29] [Observation] Leap", "2024-02-29", "Observation", "Leap", "day"),
        ("- [2026-08-24] [Pivot] Changed", "2026-08-24", "Pivot", "Changed", "day"),
        ("[2026] Annual", "2026", None, "Annual", "year"),
        ("[2026-09] Monthly", "2026-09", None, "Monthly", "month"),
        ("[2026-Q4] Quarterly", "2026-Q4", None, "Quarterly", "quarter"),
        ("[2026-H2] Half", "2026-H2", None, "Half", "half"),
        ("[2026] [[Concept_Test]]", "2026", None, "[[Concept_Test]]", "year"),
    ],
)
def test_parse_timeline_prefix_valid_formats(text, event_date, tag, description, precision):
    parsed = parse_timeline_prefix(text)
    assert parsed == (event_date, tag, description, precision)


@pytest.mark.parametrize(
    "text",
    [
        "[2023-02-29] [Observation] Not a leap day",
        "[2026-13] Invalid month",
        "[2026-Q5] Invalid quarter",
        "[2026-H3] Invalid half",
        "[2026-08-24] [Broken tag Missing bracket",
    ],
)
def test_invalid_prefix_is_left_entirely_untouched(text):
    assert parse_timeline_prefix(text) == (None, None, text, None)


def test_unrelated_inline_brackets_are_preserved():
    text = "[2026-08-24] [Observation] Kept [inline] evidence"
    assert parse_timeline_prefix(text).description == "Kept [inline] evidence"


@pytest.mark.parametrize("value", [True, {}, [], "2026-02-30", "2026-Q5", "not-a-date"])
def test_validate_timeline_date_rejects_malformed_payload_values(value):
    assert validate_timeline_date(value) is None


@pytest.mark.parametrize("value", ["2026", "2026-09", "2026-Q2", "2026-H1", "2026-09-09", "2026-09-09T12:30:00+00:00"])
def test_validate_timeline_date_preserves_supported_precision_and_timestamps(value):
    assert validate_timeline_date(value) == value
