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

This loop makes both automatic.  It is deliberately boring: it does the same calls an operator
would, on a timer, and reports what they did.  A third call joined it on 2026-09-25: the memory
Gram-index maintenance above, because the 10:00/23:00 scheduled occurrences were the only place it
ran and that left the slow path in place for up to 13 hours after a day of churn.
"""

from __future__ import annotations

import json
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
#: Nodes the vector sweep may re-embed per run.  The incremental index path invalidates a
#: page's vector on every rewrite, so the sweep has to keep up with ingest traffic without
#: holding the loop for minutes; ``0`` disables the sweep.
DEFAULT_EMBEDDING_BATCH_SIZE = 200
#: Seconds one batch of the vector sweep may wait -- rate-limit window plus retries -- before the
#: overshoot is reported as a failed batch and left to the next sweep.  A quota error sleeps a
#: flat 60 s per retry, so an unbounded batch can consume most of a 900 s interval on its own.
DEFAULT_EMBEDDING_BUDGET_SECONDS = 120.0


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


def embedding_batch_size() -> int:
    raw = str(os.environ.get("VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH", "")).strip()
    if not raw:
        return DEFAULT_EMBEDDING_BATCH_SIZE
    try:
        return max(0, int(float(raw)))
    except ValueError:
        log.warning("Ignoring unparsable VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH=%r", raw)
        return DEFAULT_EMBEDDING_BATCH_SIZE


def embedding_budget_seconds() -> float:
    raw = str(os.environ.get("VECTOR_LAKE_CATCHUP_EMBEDDING_BUDGET_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_EMBEDDING_BUDGET_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        log.warning("Ignoring unparsable VECTOR_LAKE_CATCHUP_EMBEDDING_BUDGET_SECONDS=%r", raw)
        return DEFAULT_EMBEDDING_BUDGET_SECONDS


def _embedding_catch_up(batch_size: int) -> dict:
    """Re-embed pages whose vector the incremental index path invalidated.

    ``indexer.update_index_items`` drops a page's vector the moment the page is rewritten and,
    by contract, never calls an embedding provider (pinned by
    ``test_incremental_index_invalidates_stale_vector_without_api``).  The intended
    counterpart was "explicit backfill" -- a manual CLI/MCP call -- so nothing ever put the
    vector back, and the projection only shrank.  Measured 2026-09-21: 4 573 of 7 175 nodes
    had no vector, and those 4 573 were exactly the pages rewritten on or after 2026-09-16;
    every page untouched before that date still had one.

    This is that missing counterpart.  It reuses the rate-limited backfill, so the RPM/TPM
    window, the ``GEMINI_API_KEY`` guard and the single-writer ``.embedding-backfill.lock``
    all still apply, and an operator-run backfill in flight simply makes this sweep skip.
    """
    if batch_size <= 0:
        return {"candidates": 0, "embedded": 0, "skipped": "disabled by VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH=0"}

    from vector_lake.embedding_scheduler import (
        embedding_backfill,
        page_bodies_for_keys,
        stale_embedding_keys,
    )
    from vector_lake.wiki_utils import get_index_path

    index_path = get_index_path()
    if not index_path.exists():
        return {"candidates": 0, "embedded": 0, "skipped": "index.json not found"}

    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    # A vector can be present and still wrong: nothing deletes it when a page is rewritten by a
    # path that bypasses the incremental index, or when a full rebuild changes the aliases and
    # summary the embedding text is built from.  Coverage alone therefore does not say whether the
    # projection is current, so every sweep also compares each node's recorded input digest with
    # the input it would use now.  That pass is one body scan plus one digest per node -- measured
    # at 0.39 s for 7 175 nodes on 2026-09-21 -- which is why it runs every sweep rather than on a
    # slower cadence of its own.
    bodies = page_bodies_for_keys(list((index_data.get("nodes") or {}).keys()))
    stale = stale_embedding_keys(index_data, bodies)
    plan = embedding_backfill(
        index_data,
        dry_run=False,
        limit=batch_size,
        bodies=bodies,
        extra_keys=stale,
        budget_seconds=embedding_budget_seconds(),
    )
    return {
        "candidates": plan.get("candidates", 0),
        "embedded": plan.get("embedded", 0),
        "failed_batches": plan.get("failed_batches", 0),
        "stale_inputs": len(stale),
        "skipped": plan.get("skipped", ""),
        "coverage_after": plan.get("coverage_after") or plan.get("coverage_before") or {},
    }


def catch_up_once(
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_age_seconds: int | None = None,
    embedding_batch: int | None = None,
) -> dict:
    """Run one recovery sweep and return what it did.

    The halves are independent and each is contained: a failure to expire must not stop the
    scan, and neither must stop the vector sweep.  The scan is the same call the raw watcher
    makes, so an in-flight source is still skipped and concurrent enqueue is still prevented.
    """
    from vector_lake import tool_ingest
    from vector_lake.db_store import expire_stale_subagent_jobs

    age = stale_task_max_age_seconds() if max_age_seconds is None else int(max_age_seconds)
    embed_batch = embedding_batch_size() if embedding_batch is None else int(embedding_batch)
    summary = {"expired": 0, "enqueued": "", "markers_released": 0, "embeddings": {}, "gram": "", "errors": []}

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

    try:
        # Gram-index maintenance rides this loop because the scheduled occurrences (10:00 and
        # 23:00) are too far apart to bound the slow path: a day of churn left every memory search
        # on the exact projected scan for up to 13 hours (measured 2026-09-25).  The call is cheap
        # when nothing is due -- it compares a dirty count and a search count -- and spends the
        # ~77 s rebuild only when :func:`memory_gram_index.rebuild_due_reason` can show the debt has
        # already paid for it.
        from vector_lake import memory_gram_index

        summary["gram"] = str(memory_gram_index.maybe_rebuild_memory_gram_index())
    except Exception as exc:  # noqa: BLE001 - one half failing must not lose the others
        summary["errors"].append(f"gram: {type(exc).__name__}: {exc}")
        log.warning("Catch-up gram-index maintenance failed: %s: %s", type(exc).__name__, exc)

    try:
        summary["embeddings"] = _embedding_catch_up(embed_batch)
    except Exception as exc:  # noqa: BLE001 - a provider outage must not stop the other halves
        summary["embeddings"] = {"candidates": 0, "embedded": 0, "skipped": ""}
        summary["errors"].append(f"embeddings: {type(exc).__name__}: {exc}")
        log.warning("Catch-up vector sweep failed: %s: %s", type(exc).__name__, exc)

    return summary


def describe(summary: dict) -> str:
    """One line for the status surface."""
    embeddings = summary.get("embeddings") or {}
    coverage = embeddings.get("coverage_after") or {}
    if embeddings.get("skipped"):
        vectors = f"skipped({embeddings['skipped']})"
    else:
        vectors = (
            f"+{embeddings.get('embedded', 0)} missing={coverage.get('missing', '?')}"
            f" stale_inputs={embeddings.get('stale_inputs', 0)}"
        )
    parts = [
        f"markers_released={summary.get('markers_released', 0)}",
        f"expired={summary['expired']}",
        f"scan={str(summary['enqueued'])[:80]}",
        f"gram={str(summary.get('gram') or 'skipped')[:80]}",
        f"vectors={vectors}",
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
        if summary["expired"] or "enqueued 0" not in str(summary["enqueued"]) or (summary.get("embeddings") or {}).get("embedded"):
            log.info("Catch-up: %s", describe(summary))
        delay = interval

    write_status("idle", 0, 0, "Catch-up stopped", "", component=COMPONENT)
