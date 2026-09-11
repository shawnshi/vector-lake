from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from vector_lake import auto_ingest_worker
from vector_lake.auto_ingest_runners import get_runner_adapter
from vector_lake.auto_ingest_runners import _validate_contract
from vector_lake.auto_ingest_runners.base import (
    GenerationRequest,
    RunnerRegistrationError,
    RunnerSafetyContract,
)
from vector_lake.auto_ingest_runners.codex_exec import CODEX_EXEC_ADAPTER, CodexExecAdapter


_CODEX_MANIFEST = MappingProxyType(
    {
        "binary_pin": ("applicable", "_pinned_runner_binary", "codex_binary_hash_mismatch"),
        "version_pin": ("applicable", "_probe_codex_runner_unmanaged", "codex_version_mismatch"),
        "dedicated_home": ("applicable", "_validated_runner_home", "codex_runner_home_is_not_dedicated"),
        "instruction_surface_prohibited": (
            "applicable",
            "_validated_runner_home",
            "codex_runner_home_contains_instruction_surfaces",
        ),
        "user_skills_prohibited": (
            "applicable",
            "_validated_runner_home",
            "codex_runner_skills_contains_user_content",
        ),
        "identity_pin": ("applicable", "_verify_runner_identity", "codex_auth_identity_mismatch"),
    }
)


def _gate(enforcer: str, raises: str):
    def verify(*_args, **_kwargs):
        return None

    verify.enforcer = enforcer
    verify.raises = raises
    return verify


def _contract(**overrides):
    values = {
        "binary_pin": _gate("_pinned_runner_binary", "codex_binary_hash_mismatch"),
        "version_pin": _gate("_probe_codex_runner_unmanaged", "codex_version_mismatch"),
        "dedicated_home": _gate("_validated_runner_home", "codex_runner_home_is_not_dedicated"),
        "instruction_surface_prohibited": _gate(
            "_validated_runner_home", "codex_runner_home_contains_instruction_surfaces"
        ),
        "user_skills_prohibited": _gate(
            "_validated_runner_home", "codex_runner_skills_contains_user_content"
        ),
        "identity_pin": _gate("_verify_runner_identity", "codex_auth_identity_mismatch"),
        "credential_actions": "forbidden",
    }
    values.update(overrides)
    return RunnerSafetyContract(**values)


def test_registry_is_a_frozen_whitelist_without_public_registration():
    import vector_lake.auto_ingest_runners as registry

    assert isinstance(registry._REGISTRY, MappingProxyType)
    assert set(registry._REGISTRY) == {"codex_exec"}
    assert not hasattr(registry, "register_runner")
    assert registry.__all__ == ["get_runner_adapter"]
    with pytest.raises(TypeError):
        registry._REGISTRY["other"] = CODEX_EXEC_ADAPTER  # type: ignore[index]


def test_registry_resolves_codex_and_rejects_unknown():
    assert isinstance(get_runner_adapter("codex_exec"), CodexExecAdapter)
    with pytest.raises(RunnerRegistrationError) as exc:
        get_runner_adapter("missing")
    assert str(exc.value) == "runner_is_unknown:missing"


def test_reviewed_manifest_matches_the_shipped_codex_contract():
    """Single source of truth: the manifest and the adapter must agree."""
    contract = CODEX_EXEC_ADAPTER.safety_contract
    for gate, (kind, enforcer, raises) in _CODEX_MANIFEST.items():
        assert kind == "applicable"
        declared = getattr(contract, gate)
        assert declared.enforcer == enforcer, gate
        assert declared.raises == raises, gate
        assert callable(getattr(auto_ingest_worker, enforcer, None)), gate
    assert contract.credential_actions == "forbidden"
    _validate_contract("codex_exec", CODEX_EXEC_ADAPTER, _CODEX_MANIFEST)


def test_noop_lambda_gates_are_rejected():
    """The reviewer's counterexample: lambdas that check nothing must not register."""
    adapter = SimpleNamespace(
        safety_contract=RunnerSafetyContract(
            binary_pin=lambda: None,
            version_pin=lambda: None,
            dedicated_home=lambda: None,
            instruction_surface_prohibited=lambda: None,
            user_skills_prohibited=lambda: None,
            identity_pin=lambda: None,
            credential_actions="forbidden",
        )
    )
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_gate_enforcer_mismatch:binary_pin"


def test_wrong_enforcer_for_one_gate_is_rejected():
    adapter = SimpleNamespace(
        safety_contract=_contract(
            version_pin=_gate("_verify_runner_identity", "codex_auth_identity_mismatch")
        )
    )
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_gate_enforcer_mismatch:version_pin"


def test_missing_gate_and_bad_credential_actions_are_rejected():
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", SimpleNamespace(safety_contract=None), _CODEX_MANIFEST)
    assert str(exc.value) == "runner_contract_incomplete:binary_pin"

    adapter = SimpleNamespace(safety_contract=_contract(credential_actions="allowed"))
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_contract_credential_actions_invalid"


def test_not_applicable_gate_requires_a_reason_and_unknown_manifest_fails():
    manifest = dict(_CODEX_MANIFEST)
    manifest["identity_pin"] = ("not_applicable", "no model credential is used")
    adapter = SimpleNamespace(safety_contract=_contract(identity_pin=("not_applicable", "reason")))
    _validate_contract("codex_exec", adapter, MappingProxyType(manifest))

    bad = SimpleNamespace(safety_contract=_contract(identity_pin=("not_applicable", "")))
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", bad, MappingProxyType(manifest))
    assert str(exc.value) == "runner_contract_incomplete:identity_pin"

    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("no_such_runner", CODEX_EXEC_ADAPTER)
    assert str(exc.value) == "runner_manifest_missing:no_such_runner"


def test_codex_contract_and_generate_forwarding(monkeypatch):
    adapter = CodexExecAdapter()
    config = auto_ingest_worker.AutoIngestConfig()
    adapter.validate_options(config)
    result = auto_ingest_worker._GeneratedOutput({"ok": True}, {"total": 1})
    calls = []

    def fake_run(*args):
        calls.append(args)
        return result

    monkeypatch.setattr(auto_ingest_worker, "_run_codex_generator", fake_run)
    handle = Path("C:/codex.exe")
    request = GenerationRequest("job-1", ("owner", "token", 2), "attempt", "p", 1, 2, 3)
    stop_event = object()
    health_check = lambda: None

    actual = adapter.generate(handle, request, stop_event, health_check)

    assert actual is result
    assert calls == [(handle, config, "job-1", request.lease, "p", stop_event, health_check)]


def test_codex_probe_forwards_to_clean_probe(monkeypatch):
    adapter = CodexExecAdapter()
    config = auto_ingest_worker.AutoIngestConfig()
    options = adapter.validate_options(config)
    expected = Path("C:/codex.exe")
    calls = []

    def fake_probe(value):
        calls.append(value)
        return expected

    monkeypatch.setattr(auto_ingest_worker, "_probe_codex_runner", fake_probe)

    assert adapter.probe(options) is expected
    assert calls == [config]
