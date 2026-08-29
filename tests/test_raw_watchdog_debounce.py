import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace


class ManualMonotonic:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)


class ShutdownObservedEvent:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.shutdown_set = threading.Event()
        self.handler = None

    def clear(self) -> None:
        self._event.clear()

    def set(self) -> None:
        if self.handler is not None and self.handler.shutting_down:
            self.shutdown_set.set()
        self._event.set()

    def wait(self, timeout=None) -> bool:
        return self._event.wait(timeout)


def _raw_event(path: Path):
    return SimpleNamespace(is_directory=False, src_path=str(path))


def _wake_at(handler, clock: ManualMonotonic, value: float) -> None:
    clock.set(value)
    handler._debounce_wake.set()


def _successful_result(overflow: bool) -> str:
    if not overflow:
        return "ok"
    from vector_lake.tool_ingest import FULL_SCAN_COMPLETE_TOKEN

    return f"{FULL_SCAN_COMPLETE_TOKEN}\ncomplete"


def test_raw_burst_coalesces_on_default_trailing_edge(tmp_path, monkeypatch):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.delenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_RAW_EVENT_MAX_WAIT_SECONDS", raising=False)
    clock = ManualMonotonic()
    called = threading.Event()
    calls = []
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    handler = RawWatchdogHandler(monotonic=clock)

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        called.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    try:
        assert handler._debounce_quiet_seconds == 0.75
        assert handler._debounce_max_wait_seconds == 5.0
        handler.handle_event(_raw_event(first))
        clock.set(0.2)
        handler.handle_event(_raw_event(first))
        clock.set(0.6)
        handler.handle_event(_raw_event(second))
        assert not called.is_set()

        _wake_at(handler, clock, 1.35)
        assert called.wait(timeout=1)
    finally:
        assert handler.shutdown(timeout_seconds=1)

    assert calls == [
        ([str(first.resolve()), str(second.resolve())], False),
    ]


def test_raw_continuous_burst_flushes_at_configured_max_wait(
    tmp_path,
    monkeypatch,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "10")
    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_MAX_WAIT_SECONDS", "5")
    clock = ManualMonotonic()
    called = threading.Event()
    calls = []
    source = tmp_path / "continuous.txt"
    handler = RawWatchdogHandler(monotonic=clock)

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        called.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    try:
        for index in range(10):
            clock.set(index * 0.5)
            handler.handle_event(_raw_event(source))
        assert not called.is_set()

        _wake_at(handler, clock, 5.0)
        assert called.wait(timeout=1)
    finally:
        assert handler.shutdown(timeout_seconds=1)

    assert calls == [([str(source.resolve())], False)]


def test_raw_inflight_events_run_once_on_the_next_trailing_pass(
    tmp_path,
    monkeypatch,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "0.75")
    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_MAX_WAIT_SECONDS", "5")
    clock = ManualMonotonic()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    calls = []
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    handler = RawWatchdogHandler(monotonic=clock)

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=1)
        else:
            second_started.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    try:
        handler.handle_event(_raw_event(first))
        _wake_at(handler, clock, 0.75)
        assert first_started.wait(timeout=1)

        clock.set(1.0)
        handler.handle_event(_raw_event(first))
        clock.set(1.2)
        handler.handle_event(_raw_event(second))
        _wake_at(handler, clock, 1.95)
        assert not second_started.is_set()

        release_first.set()
        assert second_started.wait(timeout=1)
    finally:
        release_first.set()
        assert handler.shutdown(timeout_seconds=1)

    assert calls == [
        ([str(first.resolve())], False),
        ([str(first.resolve()), str(second.resolve())], False),
    ]


def test_raw_debounce_overflow_promotes_batch_to_full_scan(tmp_path, monkeypatch):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_BUFFER", "1")
    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "0.75")
    clock = ManualMonotonic()
    called = threading.Event()
    calls = []
    first = tmp_path / "first.txt"
    overflowed = tmp_path / "overflowed.txt"
    handler = RawWatchdogHandler(monotonic=clock)

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        called.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    try:
        handler.handle_event(_raw_event(first))
        clock.set(0.05)
        handler.handle_event(_raw_event(first))
        with handler.lock:
            assert handler.pending_overflow is False
        clock.set(0.1)
        handler.handle_event(_raw_event(overflowed))
        assert not called.is_set()

        _wake_at(handler, clock, 0.85)
        assert called.wait(timeout=1)
    finally:
        assert handler.shutdown(timeout_seconds=1)

    assert calls == [([str(first.resolve())], True)]


def test_raw_shutdown_drains_a_pending_debounce_within_bound(
    tmp_path,
    monkeypatch,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "60")
    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_MAX_WAIT_SECONDS", "300")
    clock = ManualMonotonic()
    drained = threading.Event()
    calls = []
    source = tmp_path / "pending.txt"
    handler = RawWatchdogHandler(monotonic=clock)

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        drained.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    handler.handle_event(_raw_event(source))

    assert handler.shutdown(timeout_seconds=1)
    assert drained.is_set()
    assert calls == [([str(source.resolve())], False)]


def test_raw_completed_future_remains_single_flight_until_callback_cleanup(
    tmp_path,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    calls = []
    source = tmp_path / "single-flight.txt"
    completed = Future()
    completed.set_result("done but callback not cleaned")
    handler = RawWatchdogHandler()
    handler._run_ingest = lambda paths, overflow: calls.append((paths, overflow)) or "ok"

    with handler.lock:
        handler.sync_future = completed
        handler._queue_batch_locked([str(source.resolve())], False)
        handler._submit_pending_locked()
        assert handler.sync_future is completed
        assert handler.pending_paths == {str(source.resolve())}
        assert calls == []

        handler.sync_future = None
        handler._submit_pending_locked()

    assert handler.shutdown(timeout_seconds=1)
    assert calls == [([str(source.resolve())], False)]


def test_raw_ingest_thread_start_failure_retries_without_another_event(
    tmp_path,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    failed_once = False
    called = threading.Event()
    calls = []
    source = tmp_path / "start-failure.txt"

    class FailedStartThread:
        def start(self):
            raise RuntimeError("injected raw worker start failure")

    def thread_factory(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("name") == "vector-lake-raw-ingest" and not failed_once:
            failed_once = True
            return FailedStartThread()
        return threading.Thread(*args, **kwargs)

    handler = RawWatchdogHandler(
        retry_base_seconds=0.01,
        thread_factory=thread_factory,
    )

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        called.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    try:
        with handler.lock:
            handler._queue_batch_locked([str(source.resolve())], False)
            handler._submit_pending_locked()
        assert called.wait(timeout=1)
    finally:
        assert handler.shutdown(timeout_seconds=1)

    assert failed_once is True
    assert calls == [([str(source.resolve())], False)]


def test_raw_retry_release_still_waits_for_new_event_quiet_edge(
    tmp_path,
    monkeypatch,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "0.75")
    clock = ManualMonotonic()
    called = threading.Event()
    calls = []
    source = tmp_path / "retry-with-new-event.txt"
    handler = RawWatchdogHandler(monotonic=clock)

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        called.set()
        return _successful_result(overflow)

    handler._run_ingest = run_ingest
    try:
        with handler.lock:
            handler.retry_timer = object()
        handler.handle_event(_raw_event(source))
        clock.set(0.25)
        handler._retry_timer_fired()
        assert not called.is_set()

        _wake_at(handler, clock, 0.75)
        assert called.wait(timeout=1)
    finally:
        assert handler.shutdown(timeout_seconds=1)

    assert calls == [([str(source.resolve())], False)]


def test_raw_shutdown_reports_incomplete_overflow_drain(tmp_path, monkeypatch):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_BUFFER", "1")
    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "60")
    clock = ManualMonotonic()
    calls = []
    first = tmp_path / "first.txt"
    overflowed = tmp_path / "overflowed.txt"
    handler = RawWatchdogHandler(monotonic=clock)
    handler._run_ingest = (
        lambda paths, overflow: calls.append((paths, overflow)) or "incomplete"
    )

    handler.handle_event(_raw_event(first))
    handler.handle_event(_raw_event(overflowed))

    assert handler.shutdown(timeout_seconds=1) is False
    assert calls == [([str(first.resolve())], True)]


def test_raw_shutdown_waits_inflight_then_drains_next_pass_once(
    tmp_path,
    monkeypatch,
):
    from vector_lake.watchdog_app import RawWatchdogHandler

    monkeypatch.setenv("VECTOR_LAKE_RAW_EVENT_QUIET_SECONDS", "0.75")
    clock = ManualMonotonic()
    wake = ShutdownObservedEvent()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    shutdown_complete = threading.Event()
    shutdown_result = []
    calls = []
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    handler = RawWatchdogHandler(
        monotonic=clock,
        debounce_wake_event=wake,
    )
    wake.handler = handler

    def run_ingest(paths, overflow):
        calls.append((paths, overflow))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=1)
        else:
            second_started.set()
        return _successful_result(overflow)

    def shutdown_handler():
        shutdown_result.append(handler.shutdown(timeout_seconds=1))
        shutdown_complete.set()

    handler._run_ingest = run_ingest
    handler.handle_event(_raw_event(first))
    _wake_at(handler, clock, 0.75)
    assert first_started.wait(timeout=1)

    clock.set(1.0)
    handler.handle_event(_raw_event(second))
    shutdown_thread = threading.Thread(target=shutdown_handler)
    shutdown_thread.start()
    assert wake.shutdown_set.wait(timeout=1)
    assert not shutdown_complete.is_set()

    release_first.set()
    assert shutdown_complete.wait(timeout=1)
    shutdown_thread.join(timeout=1)

    assert shutdown_result == [True]
    assert second_started.is_set()
    assert calls == [
        ([str(first.resolve())], False),
        ([str(second.resolve())], False),
    ]
