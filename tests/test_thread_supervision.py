"""A loop thread that stops running must be restartable and reportable, never silent.

The daemon's work runs on daemon threads, and nothing watched them: the main thread kept the
aggregate status timestamp fresh, and both health surfaces read only ``components[*].status``,
never a component's age.  So a dead outbox consumer left "idle" in the file and the process
looked healthy indefinitely -- while its plausible death path was an exception in a ``finally``
block rather than anything exotic.
"""

from __future__ import annotations

import threading
import time

import pytest

from vector_lake import thread_supervision


@pytest.fixture(autouse=True)
def _clean_registry():
    thread_supervision.reset()
    yield
    thread_supervision.reset()


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_running_loop_is_reported_alive():
    stop = threading.Event()
    thread_supervision.start("steady", lambda: stop.wait(5))
    assert thread_supervision.supervise_once() == {"steady": "alive"}
    stop.set()


def test_a_dead_loop_is_restarted():
    """A loop that returns or raises is restarted, and the restart is observable."""
    calls = []

    def _dies_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        time.sleep(5)

    thread_supervision.start("flaky", _dies_once)
    assert _wait_until(lambda: len(calls) == 1)
    assert _wait_until(lambda: thread_supervision.snapshot()["flaky"]["alive"] is False)

    assert thread_supervision.supervise_once() == {"flaky": "restarted"}
    assert _wait_until(lambda: len(calls) == 2)
    assert thread_supervision.snapshot()["flaky"]["restarts"] == 1
    assert thread_supervision.supervise_once() == {"flaky": "alive"}


def test_a_normal_return_counts_as_death():
    """The loop contract is "until the process ends", so a return is a death too."""
    thread_supervision.start("returns", lambda: None)
    assert _wait_until(lambda: thread_supervision.snapshot()["returns"]["alive"] is False)
    assert thread_supervision.supervise_once() == {"returns": "restarted"}


def test_the_restart_budget_is_bounded_and_then_reported():
    """An immediately-dying loop must stop being restarted and start being reported."""
    thread_supervision.start("doomed", lambda: None, restart_limit=2)

    seen = []
    for _ in range(6):
        outcome = thread_supervision.supervise_once()
        seen.append(outcome["doomed"])
        if outcome["doomed"] == "restarted":
            assert _wait_until(lambda: thread_supervision.snapshot()["doomed"]["alive"] is False)

    assert seen.count("restarted") == 2
    assert seen[-1] == "dead"
    state, message = thread_supervision.describe({"doomed": "dead"})
    assert state == "error"
    assert "doomed" in message


def test_describe_separates_healthy_from_recovering_from_dead():
    assert thread_supervision.describe({"a": "alive"})[0] == "idle"
    assert thread_supervision.describe({"a": "restarted"})[0] == "processing"
    assert thread_supervision.describe({"a": "dead", "b": "alive"})[0] == "error"


def test_snapshot_exposes_the_last_exit_reason():
    thread_supervision.start("explains", lambda: (_ for _ in ()).throw(ValueError("why it died")))
    assert _wait_until(lambda: "why it died" in thread_supervision.snapshot()["explains"]["last_exit_reason"])


def test_the_watchdog_registers_every_loop_and_heartbeats_them():
    """The wiring must be real: all four loops registered, and the heartbeat supervises."""
    import inspect

    from vector_lake import watchdog_app

    source = inspect.getsource(watchdog_app._start_watchdog_locked)
    for name in ("outbox", "scheduler", "runner-supervisor", "ingest-worker"):
        assert f'"{name}"' in source, f"{name} is not registered with thread_supervision"
    assert "supervise_once()" in source, "the heartbeat does not supervise the loops"


def test_a_loop_that_finished_by_design_is_not_restarted_or_reported_dead():
    """Some loops return on purpose, and that must not look like a fault.

    ``catch_up_loop`` returns when the interval is 0 and ``runner_supervisor_loop`` returns when
    the host opts out of ingestion: both are documented, supported configurations.  Treating the
    return as a death restarted them on every heartbeat and, once the budget was spent, reported
    the whole daemon as ``error`` (independent review, 2026-09-19).
    """
    started = []

    def _ends_by_design():
        started.append(1)
        thread_supervision.finish("opted-out", "disabled by configuration")

    thread_supervision.start("opted-out", _ends_by_design)
    assert _wait_until(lambda: thread_supervision.snapshot()["opted-out"]["finished"] is True)

    for _ in range(3):
        assert thread_supervision.supervise_once() == {"opted-out": "finished"}

    assert len(started) == 1, "a finished loop was restarted"
    state, message = thread_supervision.describe({"opted-out": "finished"})
    assert state == "idle"
    assert "finished by design" in message
    assert thread_supervision.snapshot()["opted-out"]["restarts"] == 0


def test_finishing_does_not_hide_a_loop_that_is_still_running():
    stop = threading.Event()
    thread_supervision.start("busy", lambda: stop.wait(5))
    thread_supervision.finish("busy", "declared finished while running")
    assert thread_supervision.supervise_once() == {"busy": "alive"}
    stop.set()


def test_finish_on_an_unknown_loop_is_ignored():
    thread_supervision.finish("never-started", "nothing to do")
    assert thread_supervision.supervise_once() == {}
