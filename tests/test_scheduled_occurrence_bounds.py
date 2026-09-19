"""A failing scheduled occurrence must be bounded, and must not take the maintenance chain down.

Both halves came from the 2026-09-19 audit (S3):

* the lint ran bare at the top of the occurrence block, so a lint failure skipped the WAL
  checkpoint, the gram rebuild and the backup retention that follow it -- the only periodic
  maintenance the system has;
* ``last_occurrence`` was advanced only on success, so a failing occurrence retried every 30 s
  forever, each attempt holding ``global_task_lock`` for the whole block and therefore starving
  the outbox consumer behind it.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import watchdog_app
from vector_lake.wiki_utils import get_meta_dir


def _state() -> dict:
    path = get_meta_dir() / ".scheduled_lint_state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_marker_records_an_outcome(isolated_memory):
    watchdog_app._save_last_scheduled_lint("2026-09-19-10")
    assert _state()["outcome"] == "completed"

    watchdog_app._save_last_scheduled_lint("2026-09-19-10", outcome="failed", detail="lint: ValueError: boom")
    data = _state()
    assert data["outcome"] == "failed"
    assert "boom" in data["detail"]
    # The reader the loop uses must keep working across the widened format.
    assert watchdog_app._load_last_scheduled_lint() == "2026-09-19-10"


def test_the_maintenance_chain_is_outside_the_lints_try_block():
    """The checkpoint, rebuild and retention must run even when the lint raises."""
    import inspect

    source = inspect.getsource(watchdog_app.scheduled_lint_loop)

    lint_index = source.index("lint_vector_lake(auto_fix=False)")
    lint_try = source.rindex("try:", 0, lint_index)
    lint_except = source.index("failures.append(f\"lint:", lint_index)
    # Everything between the lint's try and its except is just the lint.
    assert "WAL checkpoint" not in source[lint_try:lint_except]
    assert "wal_checkpoint" not in source[lint_try:lint_except]

    for marker in ("wal_checkpoint(TRUNCATE)", "maybe_rebuild_memory_gram_index", "prune_backups"):
        assert source.index(marker) > lint_except, f"{marker} is inside the lint's blast radius"


def test_the_read_only_lint_runs_outside_the_mutex_it_used_to_hold():
    """The lint writes nothing, so it must not serialise the outbox drain.

    Verified under ``PRAGMA query_only=ON``: a 13.1 s lint of the live corpus completes without
    attempting a write, while ``refresh_graph_topology_if_dirty`` -- the only writer in that
    block, and one that opens its write transaction before deciding anything is dirty -- holds
    the lock.  Serialising a 13 s read against the mutation path was the whole cost.
    """
    import inspect

    source = inspect.getsource(watchdog_app.scheduled_lint_loop)
    locked = source.split("with global_task_lock:", 1)[1].split("# The lint is read-only", 1)[0]
    assert "lint_vector_lake(auto_fix=False)" not in locked, "the lint is back inside the mutex"
    assert "refresh_graph_topology_if_dirty" in locked, "the writing step must stay serialised"


def test_the_retry_is_bounded_and_the_occurrence_is_recorded_when_given_up_on():
    """A permanently failing occurrence stops being retried every tick."""
    import inspect

    source = inspect.getsource(watchdog_app.scheduled_lint_loop)
    assert "occurrence_failures += 1" in source
    assert "occurrence_failures >= MAX_OCCURRENCE_ATTEMPTS" in source
    assert 'outcome="failed"' in source
    assert "occurrence_failures = 0" in source
    assert 3 == watchdog_app.MAX_OCCURRENCE_ATTEMPTS


def test_a_failed_occurrence_records_which_occurrence_was_skipped(monkeypatch):
    """The recorded marker must name the occurrence that was given up on, not the next one."""
    import inspect

    source = inspect.getsource(watchdog_app.scheduled_lint_loop)
    given_up = source.split("if occurrence_failures >= MAX_OCCURRENCE_ATTEMPTS:", 1)[1]
    assert "last_occurrence = due" in given_up.split("time.sleep", 1)[0]


@pytest.mark.parametrize("hour", [10, 23])
def test_the_scheduled_hours_are_unchanged(hour):
    assert hour in watchdog_app.SCHEDULED_LINT_HOURS


def test_a_self_healing_cooldown_is_not_reported_as_a_fault(isolated_memory):
    """S4: ``halted`` outranks ``error`` in the aggregate, so a cooldown pinned the daemon."""
    from vector_lake import watchdog_status

    watchdog_status.reset_components()
    watchdog_status.write_status("idle", 0, 0, "heartbeat", component="watchdog")
    watchdog_status.write_status("recovering", 0, 0, "Outbox consumer cooling down", "", component="outbox")

    data = json.loads((get_meta_dir() / ".watchdog_status.json").read_text(encoding="utf-8"))
    assert data["components"]["outbox"]["status"] == "recovering"
    assert data["status"] == "recovering"
    assert data["status"] not in {"error", "halted"}, "a cooldown must not read as unhealthy"

    # A real fault still outranks it, and a genuine halt still outranks that.
    watchdog_status.write_status("error", 0, 0, "boom", "", component="scheduler")
    data = json.loads((get_meta_dir() / ".watchdog_status.json").read_text(encoding="utf-8"))
    assert data["status"] == "error"

    watchdog_status.write_status("halted", 0, 0, "terminal", "", component="outbox")
    data = json.loads((get_meta_dir() / ".watchdog_status.json").read_text(encoding="utf-8"))
    assert data["status"] == "halted"


def test_the_rebuild_runs_outside_the_global_task_lock():
    """It used to hold the in-process mutex for its whole duration.

    That blocked the outbox consumer for ~8-10 minutes per scheduled rebuild, and on 2026-09-18
    it stalled the projection drain twice (1 110 then 610 pending rows) while logging lock
    failures.  The rebuild commits one short transaction per batch now, and exactness under that
    interleaving is the snapshot fence's job -- so the mutex is no longer what makes it safe, and
    holding it is now only a way to block the mutation path.
    """
    import inspect

    source = inspect.getsource(watchdog_app.scheduled_lint_loop)
    locked = source.split("with global_task_lock:", 1)[1].split("\n                # The gram rebuild", 1)[0]
    assert "maybe_rebuild_memory_gram_index" not in locked, (
        "the gram rebuild is inside global_task_lock again"
    )
    after = source.split("# The gram rebuild deliberately runs *outside*", 1)[1]
    assert "maybe_rebuild_memory_gram_index" in after
    # The checkpoint must still follow the rebuild: the rebuild defers its auto-checkpoint.
    assert after.index("maybe_rebuild_memory_gram_index") < after.index("wal_checkpoint(TRUNCATE)")
