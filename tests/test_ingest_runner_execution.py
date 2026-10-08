"""Real queue and controlled execution regressions; never production sources/providers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ingest_runner as runner
from vector_lake import db_store, tool_ingest


def _awaiting(memory: Path, name: str, packet_text: str | None = None) -> str:
    db_store.init_db()
    source = memory / "raw" / f"{name}.md"
    source.write_text(f"# Synthetic source {name}\n", encoding="utf-8")
    payload = {"filepath": str(source), "hash": tool_ingest.calculate_hash(str(source)),
               "canonical_name": f"Source_{name}.md", "source_hash": ""}
    job = db_store.enqueue_job("ingest", payload)
    packet = memory / f"{name}-packet.json"
    packet.write_text(packet_text if packet_text is not None else json.dumps({
        "metadata": {"processed_data": payload},
    }), encoding="utf-8")
    db_store.mark_job_awaiting_subagent(job, str(packet))
    return job


def _row(job: str) -> dict:
    return dict(db_store.get_connection().execute("SELECT * FROM jobs WHERE job_id=?", (job,)).fetchone())


def test_bad_first_packet_does_not_hide_healthy_work_in_serial_cycle(isolated_memory):
    bad = _awaiting(isolated_memory, "a", "{broken JSON")
    healthy = _awaiting(isolated_memory, "b")
    batch = runner.process_once(5, True, "", {})
    assert batch["claimed"] == 2
    assert batch["errors"] == 1 and batch["needs-model"] == 1
    assert _row(bad)["retries"] == 1, "failure must be recorded exactly once"
    assert _row(healthy)["retries"] == 0
    assert _row(healthy)["status"] == "failed", "healthy shadow task must actually be processed/released"


def test_filtered_failures_cannot_exceed_parallel_cycle_cap(isolated_memory):
    bad = _awaiting(isolated_memory, "a", "{broken JSON")
    healthy = _awaiting(isolated_memory, "b")
    later = _awaiting(isolated_memory, "c")
    batch = runner.process_once(2, True, "", {}, concurrency=2)
    assert batch["claimed"] == 2
    assert batch["errors"] == batch["needs-model"] == 1
    assert _row(bad)["retries"] == 1 and _row(healthy)["retries"] == 0
    assert _row(later)["status"] == "awaiting_subagent"
    assert _row(later)["lease_generation"] == 0


@pytest.mark.parametrize("packet", ["null", "[]", '"not an object"', '{"metadata": []}', '{"metadata":{"processed_data":[]}}'])
def test_malformed_packet_is_visible_work_not_empty_queue(isolated_memory, packet):
    bad = _awaiting(isolated_memory, "bad", packet)
    tasks = json.loads(tool_ingest.claim_ingest_tasks(limit=1))
    assert len(tasks) == 1 and tasks[0]["job_id"] == bad
    assert isinstance(tasks[0]["task_packet"], dict) and tasks[0]["task_packet"].get("error")
    assert _row(bad)["retries"] == 0, "claim must not pre-spend the execution failure budget"
    counts, _ = runner._process_task(tasks[0], True, "", {})
    assert counts["errors"] == 1 and _row(bad)["retries"] == 1


def _circuit(memory, now=None, reset=False):
    from vector_lake.ingest_execution import BackendCircuit
    import time

    return BackendCircuit(memory / "runner_backend_state.json", "fake", "fake-command", reset=reset,
                          clock=(lambda: now[0]) if now is not None else time.time)


def test_backend_failure_preserves_source_and_pauses_across_restart(isolated_memory, monkeypatch):
    from vector_lake.ingest_execution import BackendFailure
    import time

    jobs = [_awaiting(isolated_memory, f"job{i}") for i in range(5)]
    now = [time.time()]
    execution = _circuit(isolated_memory, now)
    calls = []
    monkeypatch.setattr(runner, "run_model", lambda *args: (calls.append(1) or None, BackendFailure("unavailable", "process_exit")))
    for index in range(3):
        if index:
            now[0] = execution.snapshot()["retry_at"] + 1
        batch = runner.process_once(5, False, "fake", {}, execution=execution)
        assert batch["claimed"] == batch["backend-failed"] == batch["model-failed"] == 1
        assert _row(jobs[index])["retries"] == 0
    assert len(calls) == 3
    assert execution.snapshot()["status"] == "open"
    assert runner.process_once(5, False, "fake", {}, execution=execution)["claimed"] == 0
    restored = _circuit(isolated_memory, now)
    assert not restored.ready() and restored.snapshot()["failures"] == 3
    assert runner.process_once(5, False, "fake", {}, execution=restored)["claimed"] == 0
    assert _row(jobs[3])["status"] == _row(jobs[4])["status"] == "awaiting_subagent"
    assert _row(jobs[3])["lease_generation"] == 0
    assert db_store.list_abandoned_sources() == []
    restored.success()
    assert not restored.ready(), "an in-flight success cannot silently undo an owner pause"
    reset = _circuit(isolated_memory, now, reset=True)
    assert reset.ready() and reset.snapshot()["failures"] == 0


def test_repair_backend_failure_does_not_spend_content_budget(isolated_memory, monkeypatch):
    from vector_lake.ingest_execution import BackendFailure

    job = _awaiting(isolated_memory, "repair")
    task = json.loads(tool_ingest.claim_ingest_tasks(limit=1))[0]
    answers = iter([({"files_written": [], "integration": {"disposition": "standalone"}}, ""),
                    (None, BackendFailure("timeout", "timeout"))])
    monkeypatch.setattr(runner, "run_model", lambda *_: next(answers))
    monkeypatch.setattr(runner, "finalize_ingest", lambda *_: "Schema Violation")
    monkeypatch.setattr(runner, "REPAIR_ATTEMPTS", 2)
    execution = _circuit(isolated_memory)
    counts, error = runner._process_task(task, False, "fake", None, execution)
    assert error and counts["backend-failed"] == counts["model-failed"] == 1
    assert _row(job)["retries"] == 0 and _row(job)["status"] == "failed"
    assert execution.snapshot()["failures"] == 1
    assert execution.timings()["phases"]["model"]["count"] == 2
    assert execution.timings()["phases"]["finalize"]["count"] == 1


def test_host_finalize_fault_is_not_content_abandonment(isolated_memory, monkeypatch):
    job = _awaiting(isolated_memory, "infra")
    task = json.loads(tool_ingest.claim_ingest_tasks(limit=1))[0]
    monkeypatch.setattr(runner, "run_model", lambda *_: ({"files_written": [], "integration": {"disposition": "rejected"}}, ""))

    def fail(*_):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(runner, "finalize_ingest", fail)
    execution = _circuit(isolated_memory)
    counts, error = runner._process_task(task, False, "fake", None, execution)
    assert counts["errors"] == 1 and "source budget untouched" in error
    assert _row(job)["retries"] == 0 and db_store.list_abandoned_sources() == []
    assert execution.snapshot()["last_code"] == "host_runtime_error"


@pytest.mark.parametrize("corrupt", ["{broken", "null", '{"version":1}', '[]'])
def test_unreadable_circuit_fails_closed_until_explicit_owner_reset(isolated_memory, corrupt):
    path = isolated_memory / "runner_backend_state.json"
    path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(RuntimeError, match="explicit owner reset"):
        _circuit(isolated_memory)
    assert _circuit(isolated_memory, reset=True).ready()


def test_ready_backlog_yields_one_second_then_empty_queue_backs_off(isolated_memory):
    execution = _circuit(isolated_memory)
    _awaiting(isolated_memory, "ready")
    assert runner.next_cycle_delay({"claimed": 2}, 120, 0, execution) == 1
    task = db_store.claim_subagent_jobs(limit=1)[0]
    assert not db_store.ingest_tasks_ready(), "live lease cannot be treated as available backlog"
    assert runner.next_cycle_delay({"claimed": 2}, 120, 0, execution) == 5
    assert [runner.next_cycle_delay({"claimed": 0}, 120, count, execution) for count in range(1, 8)] == [5, 10, 20, 40, 80, 120, 120]
    execution.failure("process_exit")
    assert 1 <= runner.next_cycle_delay({"claimed": 1}, 120, 0, execution) <= 5
    assert _row(task["job_id"])["status"] == "subagent_processing"


def test_timings_are_bounded_and_never_include_content_or_commands(isolated_memory):
    from vector_lake.ingest_execution import METRIC_WINDOW

    execution = _circuit(isolated_memory)
    for index in range(1000):
        execution.observe("model", float(index))
    execution.observe("unknown", 1)
    execution.observe("model", float("nan"))
    metrics = execution.timings()
    assert metrics["phases"]["model"]["count"] == 1000
    assert metrics["phases"]["model"]["window_count"] == METRIC_WINDOW
    assert metrics["phases"]["model"]["p50_ms"] == 935.5
    assert metrics["invalid_samples"] == 2
    assert metrics["phases"]["finalize"]["p50_ms"] is None
    assert "fake-command" not in json.dumps(metrics)
    assert str(isolated_memory) not in json.dumps(metrics)
