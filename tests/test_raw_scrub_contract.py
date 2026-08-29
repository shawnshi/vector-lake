import json
import threading
import time
from datetime import datetime, timedelta, timezone


class ManualUtcClock:
    def __init__(self, value: datetime) -> None:
        self._lock = threading.Lock()
        self._value = value

    def __call__(self) -> datetime:
        with self._lock:
            return self._value

    def advance(self, **delta) -> None:
        with self._lock:
            self._value += timedelta(**delta)


def _wait_for_idle(handler, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with handler.lock:
            if handler.sync_future is None and handler.retry_timer is None:
                return
        time.sleep(0.01)
    raise AssertionError("raw watchdog did not become idle")


def test_scrub_ledger_busy_is_due_after_persisted_backoff_and_restart(tmp_path):
    from vector_lake.raw_scrub_contract import RawScrubLedger

    clock = ManualUtcClock(datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc))
    ledger_path = tmp_path / "raw-scrub.json"
    ledger = RawScrubLedger(ledger_path, utc_now=clock)

    due = ledger.due_status(period_days=7)
    assert due.due is True
    assert due.retry_ready is True
    attempt = ledger.begin_attempt(
        day_ordinal=due.day_ordinal,
        period_days=due.period_days,
        retry_delay_seconds=60,
    )
    ledger.finish_attempt(attempt, result="busy", retry_delay_seconds=60)

    restarted = RawScrubLedger(ledger_path, utc_now=clock)
    deferred = restarted.due_status(period_days=7)
    assert deferred.due is True
    assert deferred.retry_ready is False
    clock.advance(seconds=60)
    retry = restarted.due_status(period_days=7)
    assert retry.due is True
    assert retry.retry_ready is True

    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert payload["generation"] == 1
    assert payload["due_bucket"] == due.day_ordinal % 7
    assert payload["last_attempt_at"]
    assert payload["last_success_at"] is None
    assert payload["result"] == "busy"


def test_long_lived_handler_runs_one_daily_cycle_and_restart_reuses_success(
    tmp_path,
):
    from vector_lake.tool_ingest import FULL_SCAN_COMPLETE_TOKEN
    from vector_lake.watchdog_app import RawWatchdogHandler

    clock = ManualUtcClock(datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc))
    ledger_path = tmp_path / "raw-scrub.json"
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    handler = RawWatchdogHandler(
        utc_now=clock,
        scrub_ledger_path=ledger_path,
        retry_base_seconds=0.01,
    )

    def run_ingest(paths, overflow):
        calls.append((list(paths), overflow))
        first_started.set()
        assert release_first.wait(timeout=2)
        return f"{FULL_SCAN_COMPLETE_TOKEN}\ncomplete"

    handler._run_ingest = run_ingest
    try:
        assert handler.request_scrub_if_due() is True
        assert first_started.wait(timeout=1)
        assert handler.request_scrub_if_due() is False
        assert calls == [([], True)]
        release_first.set()
        _wait_for_idle(handler)
        assert handler.request_scrub_if_due() is False
    finally:
        release_first.set()
        assert handler.shutdown(timeout_seconds=1)

    restarted_calls = []
    restarted = RawWatchdogHandler(
        utc_now=clock,
        scrub_ledger_path=ledger_path,
        retry_base_seconds=0.01,
    )
    restarted._run_ingest = (
        lambda paths, overflow: restarted_calls.append((list(paths), overflow))
        or f"{FULL_SCAN_COMPLETE_TOKEN}\ncomplete"
    )
    try:
        assert restarted.request_scrub_if_due() is False
        assert restarted_calls == []
        clock.advance(days=1)
        assert restarted.request_scrub_if_due() is True
        _wait_for_idle(restarted)
        assert restarted.request_scrub_if_due() is False
    finally:
        assert restarted.shutdown(timeout_seconds=1)

    assert restarted_calls == [([], True)]
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert payload["generation"] == 2
    assert payload["last_success_generation"] == 2
    assert payload["last_success_day_ordinal"] == clock().date().toordinal()
    assert payload["result"] == "success"


def test_restart_catches_up_each_missed_bucket_with_a_bounded_backlog(tmp_path):
    from vector_lake.raw_scrub_contract import RawScrubLedger

    clock = ManualUtcClock(datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc))
    ledger_path = tmp_path / "raw-scrub.json"
    ledger = RawScrubLedger(ledger_path, utc_now=clock)
    first_due = ledger.due_status(period_days=7)
    first = ledger.begin_attempt(
        day_ordinal=first_due.day_ordinal,
        period_days=7,
        retry_delay_seconds=1,
    )
    ledger.finish_attempt(first, result="success")

    clock.advance(days=8)
    restarted = RawScrubLedger(ledger_path, utc_now=clock)
    current_day = clock().date().toordinal()
    recovered_days = []
    recovered_buckets = []
    for _ in range(7):
        due = restarted.due_status(period_days=7)
        assert due.due is True
        recovered_days.append(due.day_ordinal)
        recovered_buckets.append(due.due_bucket)
        attempt = restarted.begin_attempt(
            day_ordinal=due.day_ordinal,
            period_days=7,
            retry_delay_seconds=1,
        )
        restarted.finish_attempt(attempt, result="success")

    assert recovered_days == list(range(current_day - 6, current_day + 1))
    assert set(recovered_buckets) == set(range(7))
    assert restarted.due_status(period_days=7).due is False
