"""Relay spool telemetry: "requests produced, nothing consumes" must be visible.

During the 2.8-day automatic-ingest outage the spool held 11 request packets and
never any response, while every surface reported healthy.  This is pure
repo-owned state (the producer writes and polls the spool), so it needs no
knowledge of the host layout, and it must stay in the advisory tier: it says
nothing about whether a canonical write is safe.
"""

from __future__ import annotations

import json
import os

import pytest

from vector_lake import db_store
from vector_lake.runtime_health import assess_runtime_health


@pytest.fixture(autouse=True)
def _initialized(isolated_memory):
    """Health only reaches the infrastructure block once the store exists."""
    db_store.init_db()


def _write_config(meta_dir, spool_dir=None, runner="host_relay") -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "enabled": False,
        "allow_model_processing_raw_text": False,
        "runner": runner,
    }
    if spool_dir is not None:
        payload["runner_options"] = {"spool_dir": str(spool_dir)}
    (meta_dir / "auto_ingest_config.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _seed_spool(spool, *, requests: int, responses: int) -> None:
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "responses").mkdir(parents=True, exist_ok=True)
    for index in range(requests):
        (spool / "requests" / f"job-{index}.packet.json").write_text(
            "{}", encoding="utf-8"
        )
    for index in range(responses):
        (spool / "responses" / f"job-{index}.response.json").write_text(
            "{}", encoding="utf-8"
        )


def test_unconsumed_requests_are_reported_at_the_default_spool(
    isolated_memory,
):
    meta = isolated_memory / "wiki" / ".meta"
    _write_config(meta)
    _seed_spool(meta / "relay-spool", requests=11, responses=0)

    health = assess_runtime_health()

    assert health["detail"]["relay_spool"]["requests"] == 11
    assert health["detail"]["relay_spool"]["responses"] == 0
    assert "relay_spool_unconsumed:requests=11:responses=0" in health["warnings"]


def test_a_configured_spool_directory_is_honoured(isolated_memory):
    meta = isolated_memory / "wiki" / ".meta"
    custom = isolated_memory / "elsewhere" / "spool"
    _write_config(meta, spool_dir=custom)
    _seed_spool(custom, requests=3, responses=0)

    health = assess_runtime_health()

    assert health["detail"]["relay_spool"]["path"] == str(custom)
    assert health["detail"]["relay_spool"]["requests"] == 3
    assert "relay_spool_unconsumed:requests=3:responses=0" in health["warnings"]


def test_answered_requests_are_not_reported(isolated_memory):
    meta = isolated_memory / "wiki" / ".meta"
    _write_config(meta)
    _seed_spool(meta / "relay-spool", requests=2, responses=2)

    health = assess_runtime_health()

    assert health["detail"]["relay_spool"]["responses"] == 2
    assert not any(
        warning.startswith("relay_spool_unconsumed:")
        for warning in health["warnings"]
    )


def test_absent_spool_is_not_reported(isolated_memory):
    meta = isolated_memory / "wiki" / ".meta"
    _write_config(meta)

    health = assess_runtime_health()

    assert health["detail"]["relay_spool"]["requests"] == 0
    assert not any(
        warning.startswith("relay_spool_unconsumed:")
        for warning in health["warnings"]
    )


def test_spool_scan_is_bounded(isolated_memory, monkeypatch):
    meta = isolated_memory / "wiki" / ".meta"
    _write_config(meta)
    _seed_spool(meta / "relay-spool", requests=5, responses=0)
    monkeypatch.setenv("VECTOR_LAKE_RELAY_SPOOL_SCAN_LIMIT", "2")

    health = assess_runtime_health()

    assert health["detail"]["relay_spool"]["scan_complete"] is False
    assert "relay_spool_unconsumed:" in " ".join(health["warnings"])


def test_the_signal_never_blocks_the_write_gate(isolated_memory):
    """Advisory tier only: an unserved lane must not freeze canonical writes."""
    meta = isolated_memory / "wiki" / ".meta"
    db_store.init_db()
    _write_config(meta)
    _seed_spool(meta / "relay-spool", requests=4, responses=0)

    health = assess_runtime_health()

    assert "relay_spool_unconsumed:requests=4:responses=0" in health["warnings"]
    assert not any(
        issue.startswith("relay_spool_unconsumed:") for issue in health["issues"]
    )


def test_an_unreadable_spool_does_not_fail_health(isolated_memory):
    meta = isolated_memory / "wiki" / ".meta"
    _write_config(meta)
    spool = meta / "relay-spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    # A directory where a file is expected: counting must degrade, not raise.
    (spool / "requests" / "not-a-file.packet.json").mkdir()

    health = assess_runtime_health()

    assert health["detail"]["relay_spool"]["requests"] == 1
    assert os.path.isdir(spool / "requests")
