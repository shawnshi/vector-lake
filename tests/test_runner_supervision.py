"""The watchdog must own the ingest runner's lifecycle, and only once.

The pipeline was split deliberately -- the watchdog publishes ingest task packets and a
separate host process consumes them, because the model call must not happen inside the
runtime.  What was missing was an owner: the README required two manually resident
processes, only the watchdog had a launcher, and the observed consequence was 28 jobs
sitting in ``subagent_processing`` with expired leases for 34 hours while nothing reported
a fault.

These tests pin the decision (which is pure, so no process is spawned) and the two
properties that make the supervision safe: a host can opt out, and a supervisor started by
hand is not duplicated by the watchdog.
"""

from __future__ import annotations

import pytest

from vector_lake import runner_supervision

SUPERVISOR = "scripts/ingest_runner_service.py"


@pytest.fixture
def repo_with_supervisor(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ingest_runner_service.py").write_text("# stand-in\n", encoding="utf-8")
    return tmp_path


def test_default_plan_reproduces_the_hosts_own_configuration(repo_with_supervisor):
    """No switches means the configuration this host was already running."""
    plan = runner_supervision.plan_runner_autostart({}, repo_with_supervisor)

    assert plan.enabled is True
    assert plan.model_command == "python scripts/ingest_model_pi_subagents.py"
    assert plan.shadow is False, "the recorded production run wrote real pages"
    assert "--no-shadow" in plan.argv
    assert str(repo_with_supervisor / SUPERVISOR) in plan.argv
    assert "--model-cmd" in plan.argv


def test_expected_zero_disables_supervision(repo_with_supervisor):
    plan = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_EXPECTED": "0"}, repo_with_supervisor
    )

    assert plan.enabled is False
    assert "VECTOR_LAKE_RUNNER_EXPECTED" in plan.reason
    assert plan.argv == ()


def test_autostart_can_be_disabled_while_keeping_the_health_warning(repo_with_supervisor):
    """``EXPECTED`` and ``AUTOSTART`` are different questions and must both exist.

    ``EXPECTED=0`` silences ``runner_absent``; a host that runs the supervisor by hand wants
    the warning kept but the watchdog out of the way.
    """
    plan = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_AUTOSTART": "0"}, repo_with_supervisor
    )

    assert plan.enabled is False
    assert "VECTOR_LAKE_RUNNER_AUTOSTART" in plan.reason

    expected_off = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_EXPECTED": "0", "VECTOR_LAKE_RUNNER_AUTOSTART": "0"},
        repo_with_supervisor,
    )
    assert expected_off.enabled is False
    assert "VECTOR_LAKE_RUNNER_EXPECTED" in expected_off.reason


@pytest.mark.parametrize("value", ["0", "off", "false", "OFF", "False"])
def test_autostart_switch_accepts_the_documented_false_spellings(repo_with_supervisor, value):
    plan = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_AUTOSTART": value}, repo_with_supervisor
    )
    assert plan.enabled is False


def test_shadow_is_opt_in_and_omits_the_write_flag(repo_with_supervisor):
    plan = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_SHADOW": "1"}, repo_with_supervisor
    )

    assert plan.enabled is True
    assert plan.shadow is True
    assert "--no-shadow" not in plan.argv


def test_model_command_is_overridable(repo_with_supervisor):
    plan = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_MODEL_CMD": "python -m my.seam"}, repo_with_supervisor
    )

    assert plan.model_command == "python -m my.seam"
    assert plan.argv[plan.argv.index("--model-cmd") + 1] == "python -m my.seam"


def test_blank_model_command_falls_back_to_the_default(repo_with_supervisor):
    plan = runner_supervision.plan_runner_autostart(
        {"VECTOR_LAKE_RUNNER_MODEL_CMD": "   "}, repo_with_supervisor
    )
    assert plan.model_command == runner_supervision.DEFAULT_MODEL_COMMAND


def test_missing_supervisor_script_disables_supervision(tmp_path):
    plan = runner_supervision.plan_runner_autostart({}, tmp_path)

    assert plan.enabled is False
    assert "not present" in plan.reason


def test_disabled_plan_reports_instead_of_spawning(tmp_path, monkeypatch):
    """A disabled plan must still publish why, so "not supervised" is not silent."""
    written = {}

    def _record(state, task_queue, index_queue, action, error="", component="watchdog"):
        written.update(state=state, action=action, component=component)

    monkeypatch.setattr(runner_supervision, "write_status", _record)
    monkeypatch.setattr(
        runner_supervision, "plan_runner_autostart",
        lambda *a, **k: runner_supervision.RunnerPlan(enabled=False, reason="unit-test reason"),
    )

    runner_supervision.runner_supervisor_loop(stop_event=_never_set())

    assert written["component"] == "runner"
    assert "unit-test reason" in written["action"]


def test_terminate_child_is_safe_without_a_child():
    runner_supervision.terminate_child(timeout=0.1)


def _never_set():
    import threading

    return threading.Event()


def test_module_does_not_spawn_at_import():
    """Importing the module must not start anything: the watchdog decides when."""
    assert runner_supervision._child is None


def test_supervisor_heartbeat_fits_inside_the_health_staleness_window():
    """The heartbeat must be well inside the window health uses to call a supervisor stale.

    The child runner is long-lived, so without a heartbeat ``runner_supervisor.json`` would
    look stalled mid-run and ``doctor`` would report ``runner_supervisor_stalled`` on a
    healthy daemon.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "ingest_runner_service.py").read_text(
        encoding="utf-8"
    )
    heartbeat = int(re.search(r"SUPERVISOR_HEARTBEAT_SECONDS = (\d+)", source).group(1))
    stale_window = int(re.search(
        r'VECTOR_LAKE_RUNNER_STALE_SECONDS", "(\d+)"',
        (Path(__file__).resolve().parents[1] / "vector_lake" / "runtime_health.py").read_text(encoding="utf-8"),
    ).group(1))

    assert 0 < heartbeat <= stale_window / 10


def test_supervisor_takes_a_single_instance_lock():
    """Two supervisors would publish two runners against one task queue."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "ingest_runner_service.py").read_text(
        encoding="utf-8"
    )
    assert ".runner_service.lock" in source
    assert "already running for this MEMORY root" in source


def test_a_live_supervisor_is_detected_from_its_lock(tmp_path, monkeypatch):
    """A watchdog restart must adopt a supervisor it did not start.

    A hard kill cannot run the shutdown hook, so the supervisor (which isolates itself in
    its own process group) survives as an orphan.  The new watchdog has to see that from the
    lock, or it either duplicates the consumer or reports nothing running.
    """
    from filelock import FileLock

    from vector_lake import wiki_utils

    monkeypatch.setattr(wiki_utils, "get_meta_dir", lambda: tmp_path)
    assert runner_supervision.supervisor_is_running() is False

    lock = FileLock(str(runner_supervision.supervisor_lock_path()))
    lock.acquire(timeout=0)
    try:
        assert runner_supervision.supervisor_is_running() is True
    finally:
        lock.release()

    assert runner_supervision.supervisor_is_running() is False


def test_adoption_reports_the_recorded_pid(tmp_path, monkeypatch):
    import json

    from vector_lake import wiki_utils

    monkeypatch.setattr(wiki_utils, "get_meta_dir", lambda: tmp_path)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    (tmp_path / "runtime" / "runner_supervisor.json").write_text(
        json.dumps({"pid": 4321, "status": "running"}), encoding="utf-8"
    )
    assert runner_supervision._adopted_supervisor_pid() == 4321

    (tmp_path / "runtime" / "runner_supervisor.json").write_text("{ not json", encoding="utf-8")
    assert runner_supervision._adopted_supervisor_pid() is None


def test_running_supervisor_short_circuits_the_loop(tmp_path, monkeypatch):
    """With a supervisor live, the loop must report and wait, never spawn."""
    actions = []
    monkeypatch.setattr(
        runner_supervision, "write_status",
        lambda state, tq, iq, action, error="", component="watchdog": actions.append(action),
    )
    monkeypatch.setattr(runner_supervision, "supervisor_is_running", lambda: True)
    monkeypatch.setattr(runner_supervision, "_adopted_supervisor_pid", lambda: 999)
    monkeypatch.setattr(runner_supervision, "_sleep", lambda event, seconds: True)
    monkeypatch.setattr(
        runner_supervision, "_spawn",
        lambda plan: pytest.fail("must not spawn while a supervisor holds the lock"),
    )

    runner_supervision.runner_supervisor_loop(stop_event=_never_set())

    adopted = [action for action in actions if "existing supervisor" in action]
    assert adopted, actions
    assert "999" in adopted[0]


def test_the_supervision_loop_heartbeats_while_its_child_runs():
    """A long-lived child must not freeze this component's ``updated_at``.

    The supervisor's *own* file was given a heartbeat for exactly this reason; the watchdog's
    ``runner`` component had the same defect, so it would have been the next thing to lie about
    a healthy run if component age is ever read.
    """
    import inspect

    source = inspect.getsource(runner_supervision.runner_supervisor_loop)
    assert "HEARTBEAT_SECONDS" in source
    assert "child.wait(timeout=HEARTBEAT_SECONDS)" in source
    # The heartbeat must be well inside any window a health check would call stale.
    assert 0 < runner_supervision.HEARTBEAT_SECONDS <= 120
