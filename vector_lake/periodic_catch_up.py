"""Periodic recovery sweep: re-enqueue un-ingested sources and retire stale tasks.

Two gaps made the pipeline's state unrecoverable without a human:

* ``prepare_ingest_batch`` -- the only thing that turns a raw file into a job -- ran on a raw
  filesystem event or a manual CLI/MCP call and nothing else.  Measured on 2026-09-19: of 28
  un-ingested raw files, 8 had no job row at all, and because no raw file had been written for
  nine hours nothing re-scanned, so they were invisible to the runner with no path back.  The
  same applies to work the expiry policy cancelled ("expired three times without ever being
  claimed"): nothing put it back.
* ``expire_stale_subagent_jobs`` was reachable only from ``cli.py ingest-tasks --expire-stale``
  and its MCP tool, so 27 jobs aged 33.8 h to 184.6 h were still open when a consumer finally
  appeared.

This loop makes both automatic.  It is deliberately boring: it does the same two calls an
operator would, on a timer, and reports what they did.
"""

from __future__ import annotations

import logging
import os
import threading

from vector_lake.watchdog_status import write_status

log = logging.getLogger("vector-lake-catch-up")

COMPONENT = "catch-up"
#: How often the sweep runs.  ``0`` disables it.
DEFAULT_INTERVAL_SECONDS = 900
#: Delay before the first sweep, so a restart does not race the rest of the startup.
DEFAULT_FIRST_DELAY_SECONDS = 30
DEFAULT_BATCH_SIZE = 50
DEFAULT_STALE_TASK_MAX_AGE_SECONDS = 86400


def _declare_finished(name: str, reason: str) -> None:
    """Tell the thread supervisor this return is the intended end, not a death.

    Deferred import: the supervisor imports this module, and a module-level import would be a
    cycle.  Best effort, because a host that starts this loop directly has no registry.
    """
    try:
        from vector_lake import thread_supervision

        thread_supervision.finish(name, reason)
    except Exception:  # noqa: BLE001 - the loop's own contract is what matters
        pass


def interval_seconds() -> int:
    raw = str(os.environ.get("VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        return max(0, int(float(raw)))
    except ValueError:
        log.warning("Ignoring unparsable VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS=%r", raw)
        return DEFAULT_INTERVAL_SECONDS


def stale_task_max_age_seconds() -> int:
    raw = str(os.environ.get("VECTOR_LAKE_STALE_TASK_MAX_AGE_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_STALE_TASK_MAX_AGE_SECONDS
    try:
        return max(60, int(float(raw)))
    except ValueError:
        return DEFAULT_STALE_TASK_MAX_AGE_SECONDS


def catch_up_once(
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_age_seconds: int | None = None,
) -> dict:
    """Run one recovery sweep and return what it did.

    Both halves are independent and each is contained: a failure to expire must not stop the
    scan, and vice versa.  The scan is the same call the raw watcher makes, so an in-flight
    source is still skipped and concurrent enqueue is still prevented.
    """
    from vector_lake import tool_ingest
    from vector_lake.db_store import expire_stale_subagent_jobs

    age = stale_task_max_age_seconds() if max_age_seconds is None else int(max_age_seconds)
    summary = {"expired": 0, "enqueued": "", "markers_released": 0, "errors": []}

    try:
        # Markers first: a source whose marker outlived its job is invisible to the scan below,
        # so releasing those is what makes the scan able to see it.
        reconciled = tool_ingest.reconcile_ingest_in_flight()
        summary["markers_released"] = len(reconciled["released"])
        if reconciled["released"]:
            log.info(
                "Catch-up released %d in-flight marker(s) with no dispatchable job: %s",
                len(reconciled["released"]), ", ".join(sorted(reconciled["released"])[:3]),
            )
    except Exception as exc:  # noqa: BLE001 - one half failing must not lose the others
        summary["errors"].append(f"reconcile: {type(exc).__name__}: {exc}")
        log.warning("Catch-up could not reconcile in-flight markers: %s: %s", type(exc).__name__, exc)

    try:
        summary["expired"] = int(expire_stale_subagent_jobs(max_age_seconds=age))
    except Exception as exc:  # noqa: BLE001 - one half failing must not lose the other
        summary["errors"].append(f"expire: {type(exc).__name__}: {exc}")
        log.warning("Catch-up could not expire stale ingest tasks: %s: %s", type(exc).__name__, exc)

    try:
        summary["enqueued"] = str(tool_ingest.prepare_ingest_batch(batch_size=batch_size))
    except Exception as exc:  # noqa: BLE001 - a locked database is a retry, not a fault
        summary["errors"].append(f"scan: {type(exc).__name__}: {exc}")
        log.warning("Catch-up scan failed: %s: %s", type(exc).__name__, exc)

    return summary


def describe(summary: dict) -> str:
    """One line for the status surface."""
    parts = [
        f"markers_released={summary.get('markers_released', 0)}",
        f"expired={summary['expired']}",
        f"scan={str(summary['enqueued'])[:80]}",
    ]
    if summary["errors"]:
        parts.append("errors=" + "; ".join(summary["errors"])[:160])
    return "; ".join(parts)


def catch_up_loop(stop_event: threading.Event | None = None) -> None:
    """Sweep on a timer for as long as the watchdog runs."""
    interval = interval_seconds()
    if interval <= 0:
        log.info("Periodic catch-up disabled (VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS=0).")
        write_status(
            "idle", 0, 0, "Catch-up disabled by VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS=0",
            "", component=COMPONENT,
        )
        _declare_finished("catch-up", "disabled by VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS=0")
        return

    event = stop_event or threading.Event()
    # Short first delay, then the steady interval: a restart should recover a backlog quickly
    # rather than wait a whole period.
    delay = min(DEFAULT_FIRST_DELAY_SECONDS, interval)
    log.info("Periodic catch-up sweep every %ds (first in %ds).", interval, delay)

    while not event.is_set():
        if event.wait(timeout=delay):
            break
        try:
            summary = catch_up_once()
        except Exception as exc:  # noqa: BLE001 - the loop itself must never end
            log.error("Catch-up sweep raised: %s: %s", type(exc).__name__, exc)
            write_status("error", 0, 0, "Catch-up sweep raised", f"{type(exc).__name__}: {exc}", component=COMPONENT)
            delay = interval
            continue

        state = "processing" if summary["errors"] else "idle"
        write_status(state, 0, 0, "Catch-up: " + describe(summary), "; ".join(summary["errors"])[:200], component=COMPONENT)
        if summary["expired"] or "enqueued 0" not in str(summary["enqueued"]):
            log.info("Catch-up: %s", describe(summary))
        delay = interval

    write_status("idle", 0, 0, "Catch-up stopped", "", component=COMPONENT)
