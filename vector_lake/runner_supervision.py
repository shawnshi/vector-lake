"""Keep the host-side ingest runner resident, under the watchdog's lifecycle.

Why this module exists
----------------------
The pipeline is split on purpose: the watchdog publishes ingest task packets, and a
**separate** host process consumes them and makes the model call
(``scripts/ingest_runner.py``, restarted by ``scripts/ingest_runner_service.py``).  The
runtime never performs that model call itself.

What was missing is a *lifecycle owner*.  The README required two manually resident
processes, but only ``watchdog_sync.py`` had a launcher skill, so "start the daemon" left
the pipeline half-alive: the watchdog published packets into ``awaiting_subagent``, nothing
claimed them, and the queue grew without any component reporting a fault.  The observed
failure on 2026-09-17 22:16 was exactly that -- both processes were launched from an agent
session and were killed with it (no reboot: uptime was continuous), and 28 jobs sat with
expired leases for 34 hours afterwards.

So the watchdog now spawns the runner's supervisor as a **child process** and restarts it.
The architectural boundary is unchanged: the model call still happens in a child of the
runner, never inside the runtime process, and the runner still owns its own restart ladder
for the runner itself.  This module only owns the supervisor's lifecycle.

It is deliberately fail-open and observable: a host that does not do ingestion opts out
(``VECTOR_LAKE_RUNNER_EXPECTED=0``), a host that wants the warning without the autostart
sets ``VECTOR_LAKE_RUNNER_AUTOSTART=0``, and a supervisor started by hand wins the lock, in
which case this loop reports that and waits instead of spawning a second one.
"""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from vector_lake import get_extension_root
from vector_lake.watchdog_status import write_status

log = logging.getLogger("vector-lake-runner")

#: The command recorded as running on this host (``runner_supervisor.json``,
#: ``model_command``), and the one the README documents.  It is a shell string because
#: ``ingest_runner.run_model`` runs it with ``shell=True``.
DEFAULT_MODEL_COMMAND = "python scripts/ingest_model_pi_subagents.py"
#: Seconds between batches.  Operator-set (2026-09-19): 120 rather than 180, which is
#: ~20 files/hour instead of ~14 at ``--limit 1``.  This is the throughput knob, not a
#: bound -- a batch that finds no work costs one claim query.
DEFAULT_INTERVAL_SECONDS = 120
DEFAULT_LIMIT = 2
#: Same ladder as the runner supervisor uses for its own child.
BACKOFF_SECONDS = (5, 15, 60, 300)
#: A supervisor run at least this long resets the restart ladder.
HEALTHY_RUN_SECONDS = 900
#: How long to wait after a supervisor exited *immediately* -- the shape a duplicate,
#: a lock conflict or a missing prerequisite produces.  Treated as "nothing to supervise
#: right now" rather than a crash, so this cannot become an error storm the way repeated
#: lock failures do elsewhere.
FAST_EXIT_SECONDS = 10
FAST_EXIT_POLL_SECONDS = 60
#: How often to refresh this component's ``updated_at`` while its child runs.  Bounded well
#: inside any staleness window a health check would use for a component.
HEARTBEAT_SECONDS = 60
COMPONENT = "runner"

_stop = threading.Event()
_child: "subprocess.Popen | None" = None
_child_lock = threading.Lock()


@dataclass(frozen=True)
class RunnerPlan:
    """Whether the ingest runner should be started, and with what arguments."""

    enabled: bool
    reason: str
    argv: tuple[str, ...] = ()
    model_command: str = ""
    shadow: bool = False
    supervisor_script: str = ""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "on", "true", "yes"}


def _declare_finished(name: str, reason: str) -> None:
    """Tell the thread supervisor this return is the intended end, not a death."""
    try:
        from vector_lake import thread_supervision

        thread_supervision.finish(name, reason)
    except Exception:  # noqa: BLE001 - a host running this loop directly has no registry
        pass


def plan_runner_autostart(
    env: dict | None = None,
    repo_root: Path | None = None,
) -> RunnerPlan:
    """Decide whether to keep an ingest runner resident, and build its command line.

    Pure and environment-driven so the decision can be tested without spawning anything.
    """
    env = os.environ if env is None else env
    root = Path(repo_root) if repo_root is not None else get_extension_root()

    if str(env.get("VECTOR_LAKE_RUNNER_EXPECTED", "1")).strip() == "0":
        return RunnerPlan(
            enabled=False,
            reason="VECTOR_LAKE_RUNNER_EXPECTED=0 (this host does not do ingestion)",
        )
    if str(env.get("VECTOR_LAKE_RUNNER_AUTOSTART", "1")).strip().lower() in {"0", "off", "false"}:
        return RunnerPlan(
            enabled=False,
            reason="VECTOR_LAKE_RUNNER_AUTOSTART=0 (the runner is managed by hand)",
        )

    supervisor = root / "scripts" / "ingest_runner_service.py"
    if not supervisor.is_file():
        return RunnerPlan(
            enabled=False,
            reason=f"the runner supervisor script is not present at {supervisor}",
        )

    model_command = str(env.get("VECTOR_LAKE_RUNNER_MODEL_CMD") or "").strip() or DEFAULT_MODEL_COMMAND
    # Writes are the production behaviour this host was already running (shadow=false in
    # runner_supervisor.json); report-only is the opt-in, so a fresh host cannot start
    # mutating the wiki merely because the watchdog came up.
    shadow = _truthy(env.get("VECTOR_LAKE_RUNNER_SHADOW"))

    argv = [
        sys.executable or "python",
        str(supervisor),
        "--limit", str(DEFAULT_LIMIT),
        "--interval", str(DEFAULT_INTERVAL_SECONDS),
        "--model-cmd", model_command,
    ]
    if not shadow:
        argv.append("--no-shadow")

    return RunnerPlan(
        enabled=True,
        reason="runner supervision enabled",
        argv=tuple(argv),
        model_command=model_command,
        shadow=shadow,
        supervisor_script=str(supervisor),
    )


def _spawn(plan: RunnerPlan) -> "subprocess.Popen | None":
    """Start the supervisor in its own process group, detached from this console.

    ``CREATE_NEW_PROCESS_GROUP`` keeps a control event aimed at the watchdog's console from
    reaching a mid-flight model call, which is the same reason the supervisor isolates its
    own child.
    """
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        return subprocess.Popen(
            list(plan.argv),
            cwd=str(get_extension_root()),
            env=child_env,
            creationflags=flags,
        )
    except OSError as exc:
        log.error("Could not start the ingest runner supervisor: %s: %s", type(exc).__name__, exc)
        write_status(
            "error", 0, 0, "Ingest runner supervisor could not start",
            f"{type(exc).__name__}: {exc}", component=COMPONENT,
        )
        return None


def terminate_child(timeout: float = 15.0) -> None:
    """Stop the supervisor this loop started, if any.

    The child is in its own process group, so it would otherwise outlive the watchdog and
    turn into a second owner of the ingest queue the next time one starts.
    """
    global _child
    with _child_lock:
        child = _child
        _child = None
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
    except OSError:
        pass


def stop() -> None:
    """Ask the loop to end and stop the supervisor it owns."""
    _stop.set()
    terminate_child()


atexit.register(terminate_child)


def runner_supervisor_loop(stop_event: threading.Event | None = None) -> None:
    """Keep the ingest runner's supervisor alive for as long as the watchdog runs."""
    global _child

    event = _stop if stop_event is None else stop_event
    try:
        plan = plan_runner_autostart()
    except Exception as exc:  # noqa: BLE001 - a supervision loop must not die on a probe
        reason = f"could not evaluate the runner plan: {type(exc).__name__}: {exc}"
        log.error("Ingest runner not supervised: %s", reason)
        write_status("error", 0, 0, "Ingest runner plan failed", reason, component=COMPONENT)
        return
    if not plan.enabled:
        log.info("Ingest runner not supervised: %s", plan.reason)
        write_status("idle", 0, 0, f"Ingest runner not started: {plan.reason}", "", component=COMPONENT)
        _declare_finished("runner-supervisor", plan.reason)
        return

    log.info(
        "Supervising the ingest runner: %s (shadow=%s, writes=%s)",
        plan.model_command, plan.shadow, not plan.shadow,
    )
    restarts = 0
    while not event.is_set():
        # A supervisor that is already running is adopted, not duplicated: it may be one a
        # previous watchdog started and lost to a hard kill, or one an operator started by
        # hand.  Spawning a second one would put two consumers on the same task queue, and
        # reporting "nothing running" would be false.
        if supervisor_is_running():
            pid = _adopted_supervisor_pid()
            suffix = f" (pid {pid})" if pid else ""
            write_status(
                "processing", 0, 0,
                f"Ingest runner supervised by an existing supervisor{suffix}",
                "", component=COMPONENT,
            )
            if _sleep(event, FAST_EXIT_POLL_SECONDS):
                break
            continue

        started = time.monotonic()
        child = _spawn(plan)
        if child is None:
            reason = "the supervisor could not be started; see the watchdog log"
            write_status("error", 0, 0, "Ingest runner supervisor failed to start", reason, component=COMPONENT)
            if _sleep(event, BACKOFF_SECONDS[min(restarts, len(BACKOFF_SECONDS) - 1)]):
                break
            restarts += 1
            continue

        with _child_lock:
            _child = child
        action = f"Ingest runner supervised (pid {child.pid}, {plan.model_command})"
        write_status("processing", 0, 0, action, "", component=COMPONENT)

        # Heartbeat while the supervisor runs.  It is a long-lived child (it restarts the
        # runner itself), so writing only on a transition would freeze this component's
        # ``updated_at`` for the whole run -- the same staleness the supervisor's own file was
        # given a heartbeat for.  Health does not read component age today, which is exactly
        # why the component should not be the next thing to lie about it.
        exit_code: "int | None" = None
        try:
            while exit_code is None:
                try:
                    exit_code = child.wait(timeout=HEARTBEAT_SECONDS)
                except subprocess.TimeoutExpired:
                    if event.is_set():
                        terminate_child()
                        exit_code = child.wait()
                    else:
                        write_status("processing", 0, 0, action, "", component=COMPONENT)
        except Exception as exc:  # noqa: BLE001 - never let supervision itself crash the watchdog
            log.error("Waiting on the ingest runner supervisor failed: %s: %s", type(exc).__name__, exc)
            exit_code = -1
        with _child_lock:
            _child = None
        elapsed = time.monotonic() - started
        if event.is_set():
            break

        if elapsed >= HEALTHY_RUN_SECONDS:
            restarts = 0
        restarts += 1

        if elapsed < FAST_EXIT_SECONDS:
            # A duplicate supervisor (another one holds the lock), a missing prerequisite or
            # an unreadable script all look like this.  Retrying minute by minute is enough
            # and does not fill the log with lock failures.
            log.info(
                "Ingest runner supervisor exited after %.1fs (code %s); re-checking in %ds.",
                elapsed, exit_code, FAST_EXIT_POLL_SECONDS,
            )
            write_status(
                "idle", 0, 0,
                f"Ingest runner supervisor exited immediately (code {exit_code}); re-checking",
                "", component=COMPONENT,
            )
            if _sleep(event, FAST_EXIT_POLL_SECONDS):
                break
            continue

        delay = BACKOFF_SECONDS[min(restarts - 1, len(BACKOFF_SECONDS) - 1)]
        log.warning(
            "Ingest runner supervisor exited after %.0fs (code %s); restart %d in %ds.",
            elapsed, exit_code, restarts, delay,
        )
        write_status(
            "error", 0, 0,
            f"Ingest runner supervisor exited (code {exit_code}) after {int(elapsed)}s; restarting in {delay}s",
            f"runner supervisor exit {exit_code}", component=COMPONENT,
        )
        if _sleep(event, delay):
            break

    write_status("idle", 0, 0, "Ingest runner supervision stopped", "", component=COMPONENT)


def _sleep(event: threading.Event, seconds: float) -> bool:
    """Sleep interruptibly; return True when asked to stop."""
    return event.wait(timeout=seconds)


def supervisor_lock_path() -> Path:
    """The lock a running ingest runner supervisor holds for this MEMORY root."""
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "runtime" / ".runner_service.lock"


def supervisor_is_running() -> bool:
    """Whether a supervisor -- ours or one started by hand -- currently holds the lock.

    This is what makes a watchdog restart safe.  A hard kill cannot run the watchdog's
    shutdown hook, so the supervisor it started is orphaned (the supervisor puts itself in
    its own process group on purpose, so a control event cannot reach a mid-flight model
    call).  Asking the lock, rather than our own child handle, is what distinguishes
    "another supervisor owns the runner" from "the runner is unsupervised" -- the first must
    be adopted and reported, the second must be started.
    """
    from filelock import FileLock

    path = supervisor_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    lock = FileLock(str(path))
    try:
        lock.acquire(timeout=0)
    except Exception:  # noqa: BLE001 - any failure to take the lock means it is held
        return True
    try:
        lock.release()
    except Exception:  # noqa: BLE001 - releasing is best effort
        pass
    return False


def _adopted_supervisor_pid() -> "int | None":
    """The pid recorded by the supervisor that holds the lock, for the status line only."""
    import json

    from vector_lake.wiki_utils import get_meta_dir

    path = get_meta_dir() / "runtime" / "runner_supervisor.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return int(data.get("pid"))
    except (TypeError, ValueError):
        return None
