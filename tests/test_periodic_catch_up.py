"""The recovery sweep, and the marker leak that made it necessary.

Two findings from the 2026-09-19 audit are pinned here:

* C1/C3 -- ``prepare_ingest_batch`` and ``expire_stale_subagent_jobs`` were reachable only from
  a raw filesystem event or a manual CLI/MCP call, so 8 of 28 un-ingested raw files had no job
  row at all and 27 jobs aged up to 184.6 h were still open.  The sweep is what makes both
  automatic.
* C4 -- ``prepare_ingest_batch`` marked the whole batch in-flight *before* enqueuing, and an
  exception mid-loop left every later file marked with no job, skipped for the full TTL.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import db_store, periodic_catch_up, tool_ingest


@pytest.fixture
def raw_tree(isolated_memory):
    raw = isolated_memory / "raw"
    (raw / "news").mkdir(parents=True, exist_ok=True)
    (raw / "news" / "one.md").write_text("# one\n", encoding="utf-8")
    (raw / "news" / "two.md").write_text("# two\n", encoding="utf-8")
    db_store.init_db()
    return raw


def _job_paths():
    return [
        json.loads(row["payload"]).get("filepath")
        for row in db_store.get_connection().execute("SELECT payload FROM jobs")
    ]


def test_the_sweep_enqueues_an_un_ingested_source(raw_tree):
    """The gap C1 was about: a source with no event and no manual call must still be found."""
    assert _job_paths() == []

    summary = periodic_catch_up.catch_up_once()

    assert summary["errors"] == []
    assert "enqueued 2" in summary["enqueued"]
    assert sorted(_job_paths()) == sorted([str(raw_tree / "news" / "one.md"), str(raw_tree / "news" / "two.md")])


def test_the_sweep_retires_a_stale_task(raw_tree, monkeypatch):
    """A task older than the age bound is cancelled by the sweep, not only by an operator."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT INTO jobs (job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("stale-1", "ingest", json.dumps({"filepath": "x.md"}), "awaiting_subagent", 0, old, old),
        )

    summary = periodic_catch_up.catch_up_once()

    assert summary["expired"] == 1
    status = db_store.get_connection().execute(
        "SELECT status FROM jobs WHERE job_id = 'stale-1'"
    ).fetchone()[0]
    assert status == "failed", status


def test_one_half_failing_does_not_lose_the_other(raw_tree, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tool_ingest, "prepare_ingest_batch", _explode)
    summary = periodic_catch_up.catch_up_once()

    assert summary["enqueued"] == ""
    assert any("scan:" in error for error in summary["errors"])
    assert "scan=" in periodic_catch_up.describe(summary)


def test_a_failed_enqueue_does_not_leave_the_rest_marked_in_flight(raw_tree, monkeypatch):
    """C4: a marker must never exist without the job it was meant to protect.

    The batch is marked in-flight before the enqueue loop, so a failure part-way through used
    to leave every later file marked and unqueued -- skipped for the whole TTL while looking
    scheduled.  The invariant is exactly "no marker without a job": a marker on a file that
    *was* enqueued is correct (that is what stops a concurrent scan double-scheduling it).
    """
    (raw_tree / "news" / "three.md").write_text("# three\n", encoding="utf-8")
    real_enqueue = db_store.enqueue_job

    def _fail_on_two(task_type, payload, **kwargs):
        if str(payload.get("filepath", "")).endswith("two.md"):
            raise RuntimeError("enqueue refused")
        return real_enqueue(task_type, payload, **kwargs)

    monkeypatch.setattr(db_store, "enqueue_job", _fail_on_two)
    with pytest.raises(RuntimeError):
        tool_ingest.prepare_ingest_batch(batch_size=50)

    enqueued = set(_job_paths())
    assert len(enqueued) < 3, "the batch was supposed to abort part-way through"
    leaked = [ref for ref in tool_ingest._load_ingest_in_flight() if ref not in enqueued]
    assert leaked == [], f"markers leaked without jobs: {sorted(leaked)}"


def test_the_sweep_can_be_disabled(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS", "0")
    assert periodic_catch_up.interval_seconds() == 0

    written = {}
    monkeypatch.setattr(
        periodic_catch_up, "write_status",
        lambda state, tq, iq, action, error="", component="watchdog": written.update(state=state, action=action),
    )
    periodic_catch_up.catch_up_loop()

    assert written["state"] == "idle"
    assert "disabled" in written["action"].lower()


def test_the_interval_is_configurable_and_bounded():
    import os

    for value, expected in (("600", 600), ("15", 15), ("0", 0), ("-5", 0)):
        os.environ["VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS"] = value
        assert periodic_catch_up.interval_seconds() == expected
    os.environ.pop("VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS", None)
    assert periodic_catch_up.interval_seconds() == periodic_catch_up.DEFAULT_INTERVAL_SECONDS


def test_the_watchdog_registers_the_sweep():
    import inspect

    from vector_lake import watchdog_app

    source = inspect.getsource(watchdog_app._start_watchdog_locked)
    assert '"catch-up"' in source
    assert "catch_up_loop" in source


def test_a_marker_without_a_dispatchable_job_is_released(isolated_memory, raw_tree):
    """The reconciliation: an in-flight marker must not outlive the job it protects.

    Observed live 2026-09-19 07:41: 24 sources marked in one batch, 2 jobs created, and three
    of the marked sources had no job row and no idempotency-key match.  Whatever produced it,
    the state has to be recoverable: ``prepare_ingest_batch`` skips a marked source, so a
    marker without a job is a source that is never enqueued again.
    """
    from vector_lake import tool_ingest

    orphan = raw_tree / "news" / "one.md"
    tool_ingest._mark_ingest_in_flight([str(orphan)])
    ledger = tool_ingest._load_ingest_in_flight()
    assert str(orphan.resolve()) in ledger

    # Inside the grace period the marker is a scan legitimately holding it, not an orphan.
    inside = tool_ingest.reconcile_ingest_in_flight(grace_seconds=3600)
    assert inside["released"] == []

    # Past it, with no job for the source, it is released -- and the source becomes visible.
    released = tool_ingest.reconcile_ingest_in_flight(grace_seconds=0)
    assert [ref for ref in released["released"]] == [str(orphan.resolve())]
    assert tool_ingest._load_ingest_in_flight() == {}

    summary = periodic_catch_up.catch_up_once()
    assert summary["errors"] == []
    assert "enqueued 2" in summary["enqueued"]


def test_a_marker_with_a_live_job_is_kept(isolated_memory, raw_tree):
    """A real in-flight source must not be released and re-enqueued by the sweep."""
    from vector_lake import tool_ingest

    target = raw_tree / "news" / "one.md"
    tool_ingest._mark_ingest_in_flight([str(target)])
    db_store.enqueue_job(
        "ingest",
        {"filepath": str(target), "hash": "deadbeef", "canonical_name": "Source_one.md"},
    )

    assert tool_ingest.reconcile_ingest_in_flight(grace_seconds=0)["released"] == []
    assert str(target.resolve()) in tool_ingest._load_ingest_in_flight()


def test_a_marker_for_a_cancelled_job_is_released(isolated_memory, raw_tree):
    """A cancelled job is not dispatchable, so its marker is stale."""
    from vector_lake import tool_ingest

    target = raw_tree / "news" / "one.md"
    tool_ingest._mark_ingest_in_flight([str(target)])
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": str(target), "hash": "deadbeef", "canonical_name": "Source_one.md"},
    )
    db_store.update_job_status(job_id, "cancelled", "expired three times unclaimed")

    assert tool_ingest.reconcile_ingest_in_flight(grace_seconds=0)["released"] == [str(target.resolve())]


def test_a_marker_pointing_at_a_failed_at_cap_job_is_released(isolated_memory, raw_tree):
    """A terminal job is not a dispatcher, so its marker is an orphan.

    Measured live 2026-09-19: 7 of 11 terminal failures held in-flight markers, and counting
    ``failed`` as active kept every one of those sources invisible to the catch-up scan that
    would have re-enqueued them.
    """
    from vector_lake import tool_ingest

    target = raw_tree / "news" / "one.md"
    tool_ingest._mark_ingest_in_flight([str(target)])
    job_id = db_store.enqueue_job(
        "ingest",
        {"filepath": str(target), "hash": "deadbeef", "canonical_name": "Source_one.md"},
    )

    # Inside the budget the job is still dispatchable: keep the marker.
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET status='failed', retries=? WHERE job_id=?", (0, job_id)
        )
    assert tool_ingest.reconcile_ingest_in_flight(grace_seconds=0)["released"] == []

    # At the cap it is not, so the marker goes and the source becomes visible again.
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE jobs SET status='failed', retries=? WHERE job_id=?", (db_store.MAX_INGEST_ATTEMPTS, job_id)
        )
    assert tool_ingest.reconcile_ingest_in_flight(grace_seconds=0)["released"] == [str(target.resolve())]
