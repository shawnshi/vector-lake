"""Durable scheduling contract for bounded raw-inventory scrub cycles."""

from __future__ import annotations

import contextvars
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from vector_lake.durability import durable_replace_file, sync_open_file


_SCHEMA_VERSION = 1
_DEFAULT_LEDGER_NAME = "raw_scrub_ledger.json"
_ALLOWED_RESULTS = frozenset({"attempting", "busy", "failed", "incomplete", "success"})
_BOUND_RAW_SCRUB_DAY: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "vector_lake_raw_scrub_day_ordinal",
    default=None,
)


class RawScrubLedgerError(RuntimeError):
    """The durable scrub schedule could not be read or advanced safely."""


@dataclass(frozen=True)
class RawScrubDueStatus:
    due: bool
    retry_ready: bool
    day_ordinal: int
    period_days: int
    due_bucket: int | None
    generation: int
    retry_count: int
    result: str


@dataclass(frozen=True)
class RawScrubAttempt:
    generation: int
    day_ordinal: int
    period_days: int
    due_bucket: int | None
    attempted_at: str
    prior_retry_count: int


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RawScrubLedgerError("raw_scrub_clock_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RawScrubLedgerError(f"raw_scrub_ledger_invalid:{field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RawScrubLedgerError(f"raw_scrub_ledger_invalid:{field}") from exc
    return _normalized_utc(parsed)


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RawScrubLedgerError(f"raw_scrub_ledger_invalid:{field}")
    return value


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field)


@contextmanager
def bind_raw_scrub_day(day_ordinal: int | None) -> Iterator[None]:
    """Bind one UTC bucket to a full inventory, including across midnight."""
    if day_ordinal is None:
        yield
        return
    if isinstance(day_ordinal, bool) or int(day_ordinal) <= 0:
        raise ValueError("day_ordinal must be a positive integer")
    token = _BOUND_RAW_SCRUB_DAY.set(int(day_ordinal))
    try:
        yield
    finally:
        _BOUND_RAW_SCRUB_DAY.reset(token)


def current_raw_scrub_day_ordinal() -> int | None:
    return _BOUND_RAW_SCRUB_DAY.get()


class RawScrubLedger:
    """Atomically retain daily due, attempt, result, and success generations."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        if path is None:
            from vector_lake.wiki_utils import get_meta_dir

            path = get_meta_dir() / _DEFAULT_LEDGER_NAME
        self.path = Path(path)
        self._utc_now = utc_now or _default_utc_now

    def _now(self) -> datetime:
        return _normalized_utc(self._utc_now())

    @staticmethod
    def _empty() -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "generation": 0,
            "due_day_ordinal": None,
            "due_bucket": None,
            "period_days": None,
            "result": "never",
            "retry_count": 0,
            "next_retry_at": None,
            "last_attempt_at": None,
            "last_attempt_day_ordinal": None,
            "last_attempt_generation": None,
            "last_success_at": None,
            "last_success_day_ordinal": None,
            "last_success_bucket": None,
            "last_success_generation": None,
            "last_success_period_days": None,
            "updated_at": None,
        }

    def _load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise RawScrubLedgerError(
                f"raw_scrub_ledger_read_failed:{type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise RawScrubLedgerError("raw_scrub_ledger_invalid:schema_version")
        required = set(self._empty())
        if set(payload) != required:
            raise RawScrubLedgerError("raw_scrub_ledger_invalid:fields")
        _nonnegative_int(payload["generation"], "generation")
        _nonnegative_int(payload["retry_count"], "retry_count")
        for field in (
            "due_day_ordinal",
            "due_bucket",
            "period_days",
            "last_attempt_day_ordinal",
            "last_attempt_generation",
            "last_success_day_ordinal",
            "last_success_bucket",
            "last_success_generation",
            "last_success_period_days",
        ):
            _optional_nonnegative_int(payload[field], field)
        for field in ("next_retry_at", "last_attempt_at", "last_success_at", "updated_at"):
            _parse_utc(payload[field], field)
        result = payload["result"]
        if result != "never" and result not in _ALLOWED_RESULTS:
            raise RawScrubLedgerError("raw_scrub_ledger_invalid:result")
        return payload

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                sync_open_file(handle)
            durable_replace_file(temporary, self.path, source_synced=True)
        except OSError as exc:
            raise RawScrubLedgerError(
                f"raw_scrub_ledger_write_failed:{type(exc).__name__}"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def due_status(self, *, period_days: int) -> RawScrubDueStatus:
        period_days = max(0, int(period_days))
        now = self._now()
        current_day_ordinal = now.date().toordinal()
        payload = self._load()
        generation = _nonnegative_int(payload["generation"], "generation")
        retry_count = _nonnegative_int(payload["retry_count"], "retry_count")
        if period_days == 0:
            return RawScrubDueStatus(
                False,
                False,
                current_day_ordinal,
                period_days,
                None,
                generation,
                retry_count,
                str(payload["result"]),
            )
        last_success_day = payload["last_success_day_ordinal"]
        persisted_due_day = payload["due_day_ordinal"]
        if (
            persisted_due_day is not None
            and payload["period_days"] == period_days
            and payload["result"] != "success"
        ):
            next_due_day = int(persisted_due_day)
        elif (
            last_success_day is not None
            and payload["last_success_period_days"] == period_days
        ):
            next_due_day = int(last_success_day) + 1
        else:
            next_due_day = current_day_ordinal
        # One complete period covers every deterministic path bucket. A long
        # outage therefore catches up at most one bounded period, not years of
        # redundant daily scans, while still skipping no bucket.
        due_day_ordinal = max(
            next_due_day,
            current_day_ordinal - period_days + 1,
        )
        due = due_day_ordinal <= current_day_ordinal
        due_bucket = due_day_ordinal % period_days
        next_retry_at = _parse_utc(payload["next_retry_at"], "next_retry_at")
        return RawScrubDueStatus(
            due=due,
            retry_ready=due and (
                next_retry_at is None or now >= next_retry_at
            ),
            day_ordinal=due_day_ordinal,
            period_days=period_days,
            due_bucket=due_bucket,
            generation=generation,
            retry_count=retry_count,
            result=str(payload["result"]),
        )

    def begin_attempt(
        self,
        *,
        day_ordinal: int,
        period_days: int,
        retry_delay_seconds: float,
    ) -> RawScrubAttempt:
        day_ordinal = int(day_ordinal)
        period_days = max(0, int(period_days))
        if day_ordinal <= 0:
            raise RawScrubLedgerError("raw_scrub_attempt_invalid:day_ordinal")
        now = self._now()
        payload = self._load()
        generation = _nonnegative_int(payload["generation"], "generation") + 1
        prior_retry_count = _nonnegative_int(payload["retry_count"], "retry_count")
        due_bucket = day_ordinal % period_days if period_days else None
        attempted_at = now.isoformat()
        payload.update(
            {
                "generation": generation,
                "due_day_ordinal": day_ordinal,
                "due_bucket": due_bucket,
                "period_days": period_days,
                "result": "attempting",
                "next_retry_at": (
                    now + timedelta(seconds=max(0.0, float(retry_delay_seconds)))
                ).isoformat(),
                "last_attempt_at": attempted_at,
                "last_attempt_day_ordinal": day_ordinal,
                "last_attempt_generation": generation,
                "updated_at": attempted_at,
            }
        )
        self._write(payload)
        return RawScrubAttempt(
            generation,
            day_ordinal,
            period_days,
            due_bucket,
            attempted_at,
            prior_retry_count,
        )

    def finish_attempt(
        self,
        attempt: RawScrubAttempt,
        *,
        result: str,
        retry_delay_seconds: float = 0.0,
    ) -> None:
        if result not in _ALLOWED_RESULTS - {"attempting"}:
            raise ValueError(f"unsupported raw scrub result: {result}")
        now = self._now()
        payload = self._load()
        if (
            payload["generation"] != attempt.generation
            or payload["due_day_ordinal"] != attempt.day_ordinal
            or payload["period_days"] != attempt.period_days
            or payload["due_bucket"] != attempt.due_bucket
        ):
            raise RawScrubLedgerError("raw_scrub_attempt_stale")
        payload["result"] = result
        payload["updated_at"] = now.isoformat()
        if result == "success":
            payload.update(
                {
                    "retry_count": 0,
                    "next_retry_at": None,
                    "last_success_at": now.isoformat(),
                    "last_success_day_ordinal": attempt.day_ordinal,
                    "last_success_bucket": attempt.due_bucket,
                    "last_success_generation": attempt.generation,
                    "last_success_period_days": attempt.period_days,
                }
            )
        else:
            payload["retry_count"] = attempt.prior_retry_count + 1
            payload["next_retry_at"] = (
                now + timedelta(seconds=max(0.0, float(retry_delay_seconds)))
            ).isoformat()
        self._write(payload)
