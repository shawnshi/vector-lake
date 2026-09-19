"""Supervise the host-side ingest runner as a resident process.

Route B splits the runtime from the model seam: the watchdog publishes task packets
and this runner consumes them, but the model call happens in a child process the
Vector Lake runtime never spawns itself.

The runner already survives task-level failures and heartbeats per cycle. This
supervisor adds the one thing it cannot give itself: restart-on-death. It keeps its own
status file so health can tell "the runner is down" apart from "the runner never ran",
and it exits cleanly (taking the child with it) on SIGINT/SIGTERM.

Usage:
    python scripts/ingest_runner_service.py --limit 2 --interval 120
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
# Run as a script, ``sys.path[0]`` is this directory, so the package would not import.
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
def _supervisor_status_path() -> Path:
    """Resolved through the library so no install-specific path ships in the source."""
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "runtime" / "runner_supervisor.json"


SUPERVISOR_STATUS = _supervisor_status_path()
BACKOFF = [5, 15, 60, 300]
HEALTHY_RUN_SECONDS = 900  # a run this long resets the restart ladder
#: How often to refresh ``updated_at`` while a child is running.
#:
#: ``runner_supervisor.json`` is this process's health surface, and health decides
#: staleness from ``updated_at`` (``VECTOR_LAKE_RUNNER_STALE_SECONDS``, default 2 400 s).
#: The child runner is long-lived -- it loops internally and only exits on failure -- so a
#: supervisor that wrote only on a transition would report itself stalled 40 minutes into a
#: perfectly healthy run.  That was survivable while a human started it by hand; the
#: watchdog now keeps one resident, which makes it the normal path.
SUPERVISOR_HEARTBEAT_SECONDS = 60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_status(**fields) -> None:
    """Merge-write the supervisor status so a crash never wipes the last known state."""
    payload = {"pid": os.getpid(), "updated_at": _utc_now()}
    try:
        if SUPERVISOR_STATUS.exists():
            existing = json.loads(SUPERVISOR_STATUS.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload.update(existing)
    except (OSError, ValueError):
        pass
    payload.update({"pid": os.getpid(), "updated_at": _utc_now()})
    payload.update(fields)
    try:
        SUPERVISOR_STATUS.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUPERVISOR_STATUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SUPERVISOR_STATUS)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Resident supervisor for the ingest runner.")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--model-cmd", required=True)
    parser.add_argument("--no-shadow", action="store_true", help="Let the runner write real pages.")
    parser.add_argument("--max-restarts", type=int, default=0, help="0 = unlimited.")
    args = parser.parse_args()

    # One supervisor per MEMORY root.  The watchdog starts this script automatically, so a
    # second instance started by hand would publish a second runner against the same task
    # queue: leases would stop two consumers fighting over the same packets, but a duplicate
    # is still wasted work and a confusing status surface.  The loser of the race exits
    # cleanly (code 0) after recording why, which is also the signal the watchdog reads as
    # "another supervisor is live; check again shortly".
    from filelock import FileLock, Timeout

    from vector_lake.wiki_utils import get_meta_dir

    lock_path = get_meta_dir() / "runtime" / ".runner_service.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    instance_lock = FileLock(str(lock_path))
    try:
        instance_lock.acquire(timeout=0)
    except Timeout:
        # Deliberately no status write: ``runner_supervisor.json`` is the *winner's* record,
        # and the watchdog reads it to report which supervisor owns the runner.  A loser
        # that overwrote it with its own pid and status="duplicate" made health show a dead
        # pid as the owner.  Exiting 0 is the signal the watchdog reads as "another
        # supervisor is live"; it does not need this file to learn that.
        print(f"Ingest runner supervisor already running for this MEMORY root ({lock_path}).", flush=True)
        return 0

    child_argv = [
        sys.executable or "python",
        str(PROJECT / "scripts" / "ingest_runner.py"),
        "--limit", str(args.limit),
        "--interval", str(args.interval),
        "--model-cmd", args.model_cmd,
    ]
    if args.no_shadow:
        child_argv.append("--no-shadow")

    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT", "1800")

    stopping = {"flag": False}
    child: subprocess.Popen | None = None

    def _shutdown(signum, _frame):
        stopping["flag"] = True
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except OSError:
                    pass
        write_status(status="stopped", reason=f"signal {signum}", child_pid=None)
        raise SystemExit(0)

    for _sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, _sig):
            try:
                signal.signal(getattr(signal, _sig), _shutdown)
            except (ValueError, OSError):
                pass

    restarts = 0
    write_status(status="starting", child_pid=None, restarts=restarts,
                 model_command=args.model_cmd, shadow=not args.no_shadow,
                 interval=args.interval, limit=args.limit)

    while not stopping["flag"]:
        started = time.monotonic()
        write_status(status="running", child_pid=None, restarts=restarts,
                     last_started_at=_utc_now())
        try:
            # Its own process group (and no attached console) so a control event aimed at
            # this supervisor's session cannot kill a mid-flight model call in the child.
            child_flags = 0
            if os.name == "nt":
                child_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            child = subprocess.Popen(child_argv, cwd=str(PROJECT), env=child_env,
                                     creationflags=child_flags)
        except OSError as exc:
            write_status(status="failed", reason=f"cannot start runner: {exc}",
                         child_pid=None, restarts=restarts)
            return 1
        write_status(status="running", child_pid=child.pid, restarts=restarts,
                     last_started_at=_utc_now())

        exit_code = None
        while exit_code is None:
            try:
                exit_code = child.wait(timeout=SUPERVISOR_HEARTBEAT_SECONDS)
            except subprocess.TimeoutExpired:
                if stopping["flag"]:
                    # The signal handler terminated it; reap it and fall through.
                    exit_code = child.wait()
                else:
                    write_status(status="running", child_pid=child.pid, restarts=restarts)
        elapsed = time.monotonic() - started
        if stopping["flag"]:
            break
        if elapsed >= HEALTHY_RUN_SECONDS:
            restarts = 0
        restarts += 1
        write_status(status="restarting", child_pid=None, restarts=restarts,
                     last_exit_code=exit_code, last_exit_at=_utc_now(),
                     last_run_seconds=int(elapsed),
                     reason=f"runner exited {exit_code} after {int(elapsed)}s")
        if args.max_restarts and restarts > args.max_restarts:
            write_status(status="failed", reason=f"restart budget exhausted ({restarts})",
                         child_pid=None, restarts=restarts)
            return 1
        delay = BACKOFF[min(restarts - 1, len(BACKOFF) - 1)]
        for _ in range(delay):
            if stopping["flag"]:
                break
            time.sleep(1)

    write_status(status="stopped", reason="supervisor loop ended", child_pid=None)
    return 0


if __name__ == "__main__":
    # The instance lock is a local of ``main`` and stays referenced for as long as the
    # supervisor runs; the process exit releases it, including on the return paths above.
    raise SystemExit(main())
