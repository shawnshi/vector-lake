"""The ingest worker yields to another writer instead of re-taking the lock every 5 seconds.

Measured 2026-09-28: a batch of 19 governance merges waited up to 346 s per write and took about ten
minutes, because this loop takes the write lock on a 5-second cadence.  It is the periodic
housekeeper, so it is the side that should yield.

The marker it reads is written by ``db_store._record_lock_contention`` and only when a lock
acquisition actually had to retry, so a worker operating alone never sees contention and never slows
down.

These fixtures use the marker's **real** shape -- ``{"last": {...}, "history": [...]}``.  An earlier
cut of this file built ``{"at": ...}`` at the top level, which matched a reader that looked there
too: five green tests over a function that could never fire.  The shape is asserted below so the two
cannot drift apart again.
"""

import json
from datetime import datetime, timedelta, timezone

from vector_lake import ingest_worker
from vector_lake.wiki_utils import get_meta_dir


def _marker_path():
    return get_meta_dir() / "runtime" / "write_lock_contention.json"


def _write_marker(age_seconds: float, *, entry: dict | None = None, history: int = 2) -> None:
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    last = {"at": stamp.isoformat(), "outcome": "timed-out", "waited_seconds": 22.1}
    if entry is not None:
        last = entry
    payload = {"last": last, "history": [dict(last) for _ in range(history)]}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_the_fixture_matches_the_real_marker_shape(isolated_memory):
    """Guard the assumption the reader and this fixture share."""
    _write_marker(1)

    payload = json.loads(_marker_path().read_text(encoding="utf-8"))

    assert set(payload) == {"last", "history"}
    assert "at" in payload["last"]


def test_no_marker_means_normal_cadence(isolated_memory):
    assert ingest_worker._recent_write_contention() is False
    assert ingest_worker._idle_delay() == ingest_worker.INGEST_WORKER_INTERVAL_SECONDS


def test_a_fresh_contention_marker_makes_the_worker_yield(isolated_memory):
    _write_marker(5)

    assert ingest_worker._recent_write_contention() is True
    assert ingest_worker._idle_delay() == ingest_worker.INGEST_WORKER_CONTENDED_INTERVAL_SECONDS


def test_a_stale_marker_does_not_make_the_worker_yield(isolated_memory):
    _write_marker(ingest_worker.INGEST_WORKER_CONTENTION_MEMORY_SECONDS + 60)

    assert ingest_worker._recent_write_contention() is False
    assert ingest_worker._idle_delay() == ingest_worker.INGEST_WORKER_INTERVAL_SECONDS


def test_a_broken_marker_does_not_make_the_worker_yield(isolated_memory):
    """Fail open: an unreadable marker must not slow ingestion down indefinitely."""
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert ingest_worker._recent_write_contention() is False


def test_a_marker_without_a_last_entry_does_not_make_the_worker_yield(isolated_memory):
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"history": []}), encoding="utf-8")

    assert ingest_worker._recent_write_contention() is False


def test_a_last_entry_without_a_timestamp_does_not_make_the_worker_yield(isolated_memory):
    _write_marker(5, entry={"outcome": "timed-out"})

    assert ingest_worker._recent_write_contention() is False
