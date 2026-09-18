"""Catch-up semantics for the autonomous lint schedule.

The loop used to fire only while ``tm_min == 0`` was sampled, so a restart or a
slow tick over that minute skipped the whole day's lint -- together with the only
periodic ``wal_checkpoint(TRUNCATE)`` in the system.
"""

import time
from datetime import datetime

import pytest

from vector_lake import watchdog_app


def _local_time(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute).timetuple()


def test_occurrence_is_the_most_recent_scheduled_hour_of_today():
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, 10)) == "2026-09-17-10"
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, 10, 59)) == "2026-09-17-10"
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, 23, 30)) == "2026-09-17-23"


def test_occurrence_before_the_first_scheduled_hour_is_yesterdays_last():
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, 0, 1)) == "2026-09-16-23"
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, 9, 59)) == "2026-09-16-23"


def test_occurrence_rolls_over_month_and_year_boundaries():
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 3, 1, 2, 0)) == "2026-02-28-23"
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 1, 1, 2, 0)) == "2025-12-31-23"


def test_occurrence_changes_twice_a_day():
    keys = {
        watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, hour))
        for hour in range(24)
    }
    assert keys == {"2026-09-16-23", "2026-09-17-10", "2026-09-17-23"}


def test_occurrence_is_stable_within_the_scheduled_hour():
    """A restart mid-hour must not create a second occurrence to run."""
    assert watchdog_app.scheduled_lint_occurrence(
        _local_time(2026, 9, 17, 10, 0)
    ) == watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 17, 10, 59))


def test_a_skipped_occurrence_is_still_due_after_a_restart(isolated_memory, monkeypatch):
    """The reported defect: a missed 23:00 must be caught up, not dropped."""
    watchdog_app._save_last_scheduled_lint("2026-09-15-23")

    assert watchdog_app._load_last_scheduled_lint() == "2026-09-15-23"
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 16, 23, 5)) != "2026-09-15-23"
    # And a completed occurrence is not repeated.
    watchdog_app._save_last_scheduled_lint("2026-09-16-23")
    assert watchdog_app.scheduled_lint_occurrence(_local_time(2026, 9, 16, 23, 45)) == "2026-09-16-23"
    assert watchdog_app._load_last_scheduled_lint() == "2026-09-16-23"


def test_missing_or_corrupt_marker_reads_as_unknown(isolated_memory):
    assert watchdog_app._load_last_scheduled_lint() == ""

    path = watchdog_app._scheduled_lint_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert watchdog_app._load_last_scheduled_lint() == ""


def test_the_marker_survives_a_restart(isolated_memory):
    """Only the first read after a restart must pay for the state file."""
    watchdog_app._save_last_scheduled_lint("2026-09-17-10")
    stamp = watchdog_app._scheduled_lint_state_path().stat().st_mtime

    assert watchdog_app._load_last_scheduled_lint() == "2026-09-17-10"
    assert watchdog_app._scheduled_lint_state_path().stat().st_mtime == stamp
    assert not list(
        watchdog_app._scheduled_lint_state_path().parent.glob(".scheduled_lint_state.json.tmp")
    ), "the temp file was left behind"


def test_repeated_saves_are_atomic_and_idempotent(isolated_memory, monkeypatch):
    """A crash mid-write must never leave a truncated marker behind."""
    watchdog_app._save_last_scheduled_lint("2026-09-17-10")
    time.sleep(0.01)
    watchdog_app._save_last_scheduled_lint("2026-09-17-10")
    watchdog_app._save_last_scheduled_lint("2026-09-17-23")

    assert watchdog_app._load_last_scheduled_lint() == "2026-09-17-23"


def test_loop_runs_a_due_occurrence_and_records_only_a_completed_one(isolated_memory, monkeypatch):
    """End-to-end wiring: a due occurrence lints, then the marker is persisted."""
    events = []
    monkeypatch.setattr(watchdog_app, "_load_last_scheduled_lint", lambda: "")
    monkeypatch.setattr(watchdog_app, "_save_last_scheduled_lint", events.append)
    monkeypatch.setattr(watchdog_app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(watchdog_app.time, "localtime", lambda *a: _local_time(2026, 9, 17, 12))

    import vector_lake.indexer as indexer
    import vector_lake.tool_lint as tool_lint

    monkeypatch.setattr(indexer, "refresh_graph_topology_if_dirty", lambda: False)
    monkeypatch.setattr(tool_lint, "lint_vector_lake", lambda auto_fix=False: events.append(("lint", auto_fix)))

    class _Stop(Exception):
        pass

    def stop(_seconds):
        raise _Stop

    monkeypatch.setattr(watchdog_app.time, "sleep", stop)
    with pytest.raises(_Stop):
        watchdog_app.scheduled_lint_loop()

    assert ("lint", False) in events, "the due occurrence did not run the lint"
    assert "2026-09-17-10" in events, "a completed occurrence was not recorded"


def test_loop_rebuilds_the_gram_index_before_the_checkpoint(isolated_memory, monkeypatch):
    """Order matters: the rebuild is the largest transaction, so the truncate follows it.

    The cadence only holds if a scheduled occurrence actually calls the rebuild, and the
    WAL the rebuild grows is only reclaimed by the checkpoint that runs after it.
    """
    events = []

    class _Stop(Exception):
        pass

    monkeypatch.setattr(watchdog_app, "_load_last_scheduled_lint", lambda: "")
    monkeypatch.setattr(watchdog_app, "_save_last_scheduled_lint", lambda _o: None)
    monkeypatch.setattr(watchdog_app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(watchdog_app.time, "localtime", lambda *a: _local_time(2026, 9, 17, 12))
    monkeypatch.setattr(
        watchdog_app.time, "sleep", lambda _s: (_ for _ in ()).throw(_Stop())
    )

    import vector_lake.db_store as db_store
    import vector_lake.memory_gram_index as memory_gram_index
    import vector_lake.tool_lint as tool_lint
    import vector_lake.indexer as indexer

    monkeypatch.setattr(indexer, "refresh_graph_topology_if_dirty", lambda: False)
    monkeypatch.setattr(tool_lint, "lint_vector_lake", lambda auto_fix=False: events.append("lint"))

    def _due(conn=None):
        events.append("due")
        return True

    def _rebuild(dry_run=False):
        events.append("gram")
        return "rebuilt"

    monkeypatch.setattr(memory_gram_index, "rebuild_due", _due)
    monkeypatch.setattr(memory_gram_index, "maybe_rebuild_memory_gram_index", _rebuild)
    monkeypatch.setattr(
        "vector_lake.backup_retention.prune_backups",
        lambda path, dry_run=False: events.append("backup") or {},
    )

    # Only the checkpoint block imports get_connection *inside* the function, so patching
    # the module attribute reaches it without disturbing the modules that bound the name
    # at import time.
    real = db_store.get_connection()

    class _Recorder:
        def execute(self, sql, *args):
            if "checkpoint" in sql:
                events.append("checkpoint")
            return real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(db_store, "get_connection", lambda: _Recorder())

    with pytest.raises(_Stop):
        watchdog_app.scheduled_lint_loop()

    assert "gram" in events, "a scheduled occurrence never settled the index"
    assert "checkpoint" in events, "the checkpoint did not run"
    assert events.index("gram") < events.index("checkpoint"), events


def test_loop_does_not_rerun_an_already_completed_occurrence(isolated_memory, monkeypatch):
    monkeypatch.setattr(watchdog_app, "_load_last_scheduled_lint", lambda: "2026-09-17-10")
    monkeypatch.setattr(watchdog_app, "_save_last_scheduled_lint", lambda _o: pytest.fail("re-ran"))
    monkeypatch.setattr(watchdog_app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(watchdog_app.time, "localtime", lambda *a: _local_time(2026, 9, 17, 12))

    import vector_lake.tool_lint as tool_lint

    monkeypatch.setattr(
        tool_lint, "lint_vector_lake", lambda auto_fix=False: pytest.fail("re-ran the lint")
    )

    class _Stop(Exception):
        pass

    monkeypatch.setattr(watchdog_app.time, "sleep", lambda _s: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        watchdog_app.scheduled_lint_loop()
