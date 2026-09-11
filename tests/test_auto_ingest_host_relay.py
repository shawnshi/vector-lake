import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vector_lake import auto_ingest_worker
from vector_lake.auto_ingest_runners.base import GenerationRequest
from vector_lake.auto_ingest_runners.host_relay import (
    DEFAULT_FULL_RESERVATION_TOKENS,
    EXPECTED_OUTPUT,
    HOST_RELAY_ADAPTER,
    RELAY_PROTOCOL_VERSION,
)


def _options(tmp_path, **changes):
    spool = tmp_path / "spool"
    spool.mkdir()
    raw = {
        "spool_dir": str(spool.resolve()),
        "relay_protocol_version": RELAY_PROTOCOL_VERSION,
        "poll_seconds": 0.05,
    }
    raw.update(changes)
    return HOST_RELAY_ADAPTER.validate_options(raw)


def _request(attempt="attempt_1", generation=3, prompt="prompt", maximum=4096, timeout=1):
    return GenerationRequest(
        "job_1", ("owner", "token", generation), attempt, prompt, 4096, maximum, timeout
    )


def _packet(spool):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        packets = list((spool / "requests").glob("*.packet.json"))
        if packets:
            return packets[0], json.loads(packets[0].read_text(encoding="utf-8"))
        time.sleep(0.01)
    raise AssertionError("packet_not_published")


def _response_path(spool, packet, suffix="response"):
    stem = f"{packet['attempt_id']}.{packet['lease_generation']}.{packet['nonce']}"
    return spool / "responses" / f"{stem}.{suffix}.json"


def _document(packet, **changes):
    value = {
        "protocol": RELAY_PROTOCOL_VERSION,
        "attempt_id": packet["attempt_id"],
        "lease_generation": packet["lease_generation"],
        "nonce": packet["nonce"],
        "output": {"files_written": [], "processed_data": [], "usage": {"total": 1}},
    }
    value.update(changes)
    return value


def _run(options, request, responder=None, stop=None, health=None):
    handle = HOST_RELAY_ADAPTER.probe(options)
    thread = None
    if responder:
        thread = threading.Thread(target=responder, args=(handle.resource,), daemon=True)
        thread.start()
    try:
        return HOST_RELAY_ADAPTER.generate(
            handle, request, stop or threading.Event(), health
        )
    finally:
        if thread:
            thread.join(timeout=2)


def _atomic_responder(spool, mutate=None):
    _, packet = _packet(spool)
    assert set(packet) == {
        "protocol", "job_id", "attempt_id", "lease_generation", "nonce", "prompt",
        "max_input_bytes", "max_output_bytes", "expected_output", "created_at",
    }
    assert packet["expected_output"] == EXPECTED_OUTPUT
    document = _document(packet)
    if mutate:
        mutate(document)
    target = _response_path(spool, packet)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(document), encoding="utf-8")
    os.replace(temporary, target)


def test_success_round_trip_and_full_reservation_usage(tmp_path):
    result = _run(_options(tmp_path), _request(), _atomic_responder)
    assert result["usage"] == {"total": 1}
    assert result.usage == {
        "input_tokens": DEFAULT_FULL_RESERVATION_TOKENS,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    # Cleanup is deliberately conditional: it only runs where the platform offers
    # a directory-relative unlink, so that an unverifiable delete can never escape
    # the pinned spool. Elsewhere the artifacts are intentionally left in place,
    # which is the accepted trade for security decision A (spool ACL is the real
    # boundary; path checks are defence in depth).
    from vector_lake.auto_ingest_runners.host_relay import _supports_handle_relative_io

    requests_left = list((tmp_path / "spool" / "requests").iterdir())
    responses_left = list((tmp_path / "spool" / "responses").iterdir())
    if _supports_handle_relative_io():
        assert not requests_left
        assert not responses_left
    else:
        # Without handle-relative I/O the relay must NOT delete anything, so the
        # packet and the response are both expected to remain. Asserting that they
        # ARE present (rather than merely tolerating an empty directory) prevents
        # this test from masking an unsafe path-based deletion regression.
        assert len(requests_left) == 1, requests_left
        assert requests_left[0].name.endswith(".packet.json"), requests_left
        assert len(responses_left) == 1, responses_left
        assert responses_left[0].name.endswith(".response.json"), responses_left


def test_output_usage_key_cannot_change_accounting(tmp_path):
    def responder(spool):
        _atomic_responder(
            spool,
            lambda document: document["output"].__setitem__(
                "usage", {"input_tokens": 0, "total": -999999}
            ),
        )

    result = _run(_options(tmp_path), _request(), responder)
    assert result["usage"] == {"input_tokens": 0, "total": -999999}
    assert result.usage == {
        "input_tokens": DEFAULT_FULL_RESERVATION_TOKENS,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }


def test_configured_full_reservation_and_retain_artifacts(tmp_path):
    options = _options(tmp_path)
    options = type(options)(options.values, SimpleNamespace(max_tokens_per_task=23456, retain_artifacts=True))
    result = _run(options, _request(), _atomic_responder)
    assert sum(result.usage.values()) == 23456
    assert list((tmp_path / "spool" / "requests").glob("*.packet.json"))
    assert list((tmp_path / "spool" / "responses").glob("*.response.json"))


def test_timeout_without_responder_calls_health_and_cleans(tmp_path):
    calls = []
    with pytest.raises(auto_ingest_worker.AutoIngestInfrastructureError, match="relay_response_timeout"):
        _run(_options(tmp_path), _request(timeout=0.12), health=lambda: calls.append(1))
    assert calls


def test_stop_event_aborts_wait(tmp_path):
    stop = threading.Event()
    timer = threading.Timer(0.08, stop.set)
    timer.start()
    with pytest.raises(auto_ingest_worker.AutoIngestInfrastructureError, match="watchdog_shutdown"):
        _run(_options(tmp_path), _request(), stop=stop)
    timer.join()


def test_preexisting_stop_wins_over_health_failure(tmp_path):
    stop = threading.Event()
    stop.set()

    def unhealthy():
        raise AssertionError("health_check_must_not_run_after_stop")

    with pytest.raises(
        auto_ingest_worker.AutoIngestInfrastructureError, match="watchdog_shutdown"
    ):
        _run(_options(tmp_path), _request(), stop=stop, health=unhealthy)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{", "relay_response_json_invalid"),
        (b"x" * 5000, "relay_response_exceeds_max_output_bytes"),
    ],
)
def test_malformed_and_oversized_response_are_policy_errors(tmp_path, body, code):
    def responder(spool):
        _, packet = _packet(spool)
        _response_path(spool, packet).write_bytes(body)

    with pytest.raises(auto_ingest_worker.AutoIngestPolicyError, match=code):
        _run(_options(tmp_path), _request(maximum=1024), responder)


@pytest.mark.parametrize("field,value", [("nonce", "wrong"), ("lease_generation", 99)])
def test_wrong_binding_is_infrastructure_error(tmp_path, field, value):
    def responder(spool):
        _atomic_responder(spool, lambda doc: doc.__setitem__(field, value))

    with pytest.raises(
        auto_ingest_worker.AutoIngestInfrastructureError,
        match="relay_response_binding_mismatch",
    ):
        _run(_options(tmp_path), _request(), responder)


def test_stale_response_is_ignored(tmp_path):
    options = _options(tmp_path)
    stale = options.values["responses_dir"] / "earlier.1.deadbeef.response.json"
    stale.write_text("{}", encoding="utf-8")
    result = _run(options, _request(), _atomic_responder)
    assert result["files_written"] == []
    assert stale.exists()


def test_partially_written_response_is_ignored_until_renamed(tmp_path):
    def responder(spool):
        _, packet = _packet(spool)
        target = _response_path(spool, packet)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(_document(packet)), encoding="utf-8")
        time.sleep(0.12)
        os.replace(temporary, target)

    started = time.monotonic()
    _run(_options(tmp_path), _request(), responder)
    assert time.monotonic() - started >= 0.1


def test_symlinked_response_is_rejected(tmp_path):
    def responder(spool):
        _, packet = _packet(spool)
        source = spool / "outside.json"
        source.write_text(json.dumps(_document(packet)), encoding="utf-8")
        try:
            _response_path(spool, packet).symlink_to(source)
        except OSError:
            pytest.skip("symlink creation unavailable")

    with pytest.raises(auto_ingest_worker.AutoIngestInfrastructureError, match="relay_response_is_link"):
        _run(_options(tmp_path), _request(), responder)


def test_filesystem_read_failure_is_infrastructure_error(tmp_path, monkeypatch):
    original_open = Path.open

    def denied(path, *args, **kwargs):
        if path.name.endswith(".response.json"):
            raise PermissionError
        return original_open(path, *args, **kwargs)

    def responder(spool):
        monkeypatch.setattr(Path, "open", denied)
        _atomic_responder(spool)

    with pytest.raises(
        auto_ingest_worker.AutoIngestInfrastructureError,
        match="relay_response_read_failed",
    ):
        _run(_options(tmp_path), _request(), responder)


def test_prompt_budget_fails_before_publication(tmp_path):
    request = _request(prompt="too long")
    request = GenerationRequest(*request.__dict__.values())
    request = GenerationRequest(request.job_id, request.lease, request.attempt_id, request.prompt, 2, 4096, 1)
    with pytest.raises(auto_ingest_worker.AutoIngestPolicyError, match="relay_prompt_exceeds_max_input_bytes"):
        _run(_options(tmp_path), request)


def test_host_reported_failure_is_validated(tmp_path):
    def responder(spool):
        _, packet = _packet(spool)
        document = _document(packet, failed="host_capacity_exhausted")
        document.pop("output")
        _response_path(spool, packet, "failed").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(auto_ingest_worker.AutoIngestPolicyError, match="relay_reported_failure:host_capacity_exhausted"):
        _run(_options(tmp_path), _request(), responder)


def test_validate_options_fails_closed(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    valid = {"spool_dir": str(spool.resolve()), "relay_protocol_version": RELAY_PROTOCOL_VERSION}
    cases = [
        ({}, "relay_spool_dir_missing"),
        ({"spool_dir": str(spool.resolve())}, "relay_protocol_version_missing"),
        ({**valid, "spool_dir": "relative"}, "relay_spool_dir_invalid"),
        ({**valid, "relay_protocol_version": "v2"}, "relay_protocol_version_unsupported"),
        ({**valid, "extra": True}, "relay_runner_options_unknown_key"),
    ]
    for raw, code in cases:
        with pytest.raises(ValueError, match=code):
            HOST_RELAY_ADAPTER.validate_options(raw)


def test_validate_options_rejects_linked_spool_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="relay_spool_dir_invalid"):
        HOST_RELAY_ADAPTER.validate_options(
            {"spool_dir": str(link.absolute()), "relay_protocol_version": RELAY_PROTOCOL_VERSION}
        )
