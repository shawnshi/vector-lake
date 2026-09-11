"""Evidence for the controller-to-runner switch that the transcript cannot see.

The frozen byte-equivalence transcript covers the codex success path across the
generation boundary. It cannot observe the two branches the switch introduced:
re-probing when the configured runner name changes, and converting a registry
error into an infrastructure error. These tests cover exactly those paths.
"""

from __future__ import annotations

import pytest

from vector_lake import auto_ingest_worker
from vector_lake.auto_ingest_runners.base import (
    RunnerHandle,
    RunnerOptions,
    RunnerRegistrationError,
)


class _RecordingAdapter:
    def __init__(self) -> None:
        self.probes: list[object] = []
        self.validated: list[object] = []

    def validate_options(self, raw):
        self.validated.append(raw)
        return RunnerOptions(values={"stub": True}, config=raw)

    def probe(self, options):
        self.probes.append(options.config)
        return RunnerHandle(resource=f"handle-{len(self.probes)}", options=options)


def _config(runner: str) -> auto_ingest_worker.AutoIngestConfig:
    return auto_ingest_worker.AutoIngestConfig(runner=runner, enabled=True)


def test_get_runner_reprobes_when_the_runner_name_changes(monkeypatch):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(
        auto_ingest_worker, "get_runner_adapter", lambda _name: adapter
    )
    controller = auto_ingest_worker.AutoIngestController()

    first = controller._get_runner(_config("codex_exec"))
    second = controller._get_runner(_config("codex_exec"))
    # Same runner inside the TTL: the cached handle must be reused, not re-probed.
    assert first is second
    assert len(adapter.probes) == 1

    third = controller._get_runner(_config("host_relay"))
    assert third is not first
    assert len(adapter.probes) == 2
    assert controller._runner_name == "host_relay"


def test_get_runner_reprobes_after_the_ttl_elapses(monkeypatch):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(
        auto_ingest_worker, "get_runner_adapter", lambda _name: adapter
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(auto_ingest_worker.time, "monotonic", lambda: clock["now"])
    controller = auto_ingest_worker.AutoIngestController()

    controller._get_runner(_config("codex_exec"))
    clock["now"] += auto_ingest_worker._RUNNER_PROBE_TTL_SECONDS - 1
    controller._get_runner(_config("codex_exec"))
    assert len(adapter.probes) == 1

    clock["now"] += 2
    controller._get_runner(_config("codex_exec"))
    assert len(adapter.probes) == 2


def test_registration_error_is_converted_to_infrastructure_error(monkeypatch):
    def reject(_name):
        raise RunnerRegistrationError("runner_is_unknown:mystery")

    monkeypatch.setattr(auto_ingest_worker, "get_runner_adapter", reject)
    controller = auto_ingest_worker.AutoIngestController()

    with pytest.raises(auto_ingest_worker.AutoIngestInfrastructureError) as exc:
        controller._get_runner(_config("mystery"))
    # The caller only catches AutoIngestInfrastructureError, so a bare ValueError
    # must never escape here, and the code must stay fixed rather than free text.
    assert "auto_ingest_runner_registration_failed" in str(exc.value)
