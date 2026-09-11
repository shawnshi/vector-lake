import re
from datetime import date, datetime
from typing import NamedTuple


class TimelinePrefix(NamedTuple):
    event_date: str | None
    event_tag: str | None
    description: str
    precision: str | None


_PREFIX = re.compile(
    r"^(?P<bullet>-\s+)?\[(?P<date>[^\]\r\n]+)\]"
    r"(?:\s+\[(?P<tag>[^\[\]\r\n]+)\])?\s*(?P<description>.*)$"
)
_YEAR = re.compile(r"20\d{2}")
_MONTH = re.compile(r"20\d{2}-(?:0[1-9]|1[0-2])")
_QUARTER = re.compile(r"20\d{2}-Q[1-4]")
_HALF = re.compile(r"20\d{2}-H[1-2]")
_FULL_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")


def validate_timeline_date(value: object) -> str | None:
    """Return a supported, parseable Timeline date string or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    if _YEAR.fullmatch(value) or _MONTH.fullmatch(value) or _QUARTER.fullmatch(value) or _HALF.fullmatch(value):
        return value
    if _FULL_DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value
    if "T" in value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    return None


def parse_timeline_prefix(text: str) -> TimelinePrefix:
    """Parse a complete leading Timeline date/tag prefix without broad cleanup."""
    raw = str(text or "")
    match = _PREFIX.fullmatch(raw)
    if not match:
        return TimelinePrefix(None, None, raw, None)

    raw_date = match.group("date")
    precision = None
    if _FULL_DATE.fullmatch(raw_date):
        if validate_timeline_date(raw_date) is None:
            return TimelinePrefix(None, None, raw, None)
        precision = "day"
    elif _MONTH.fullmatch(raw_date):
        precision = "month"
    elif _QUARTER.fullmatch(raw_date):
        precision = "quarter"
    elif _HALF.fullmatch(raw_date):
        precision = "half"
    elif _YEAR.fullmatch(raw_date):
        precision = "year"
    else:
        return TimelinePrefix(None, None, raw, None)

    tag = match.group("tag")
    description = match.group("description")
    if tag is None and description.startswith("[") and not description.startswith("[["):
        # Do not accept a date-only prefix when a following tag is malformed.
        return TimelinePrefix(None, None, raw, None)
    return TimelinePrefix(
        raw_date,
        tag.strip() if tag else None,
        description,
        precision,
    )
