import base64
import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from vector_lake import auto_ingest_worker
from vector_lake.auto_ingest_runners import _validate_contract, get_runner_adapter
from vector_lake.auto_ingest_runners.base import (
    GenerationRequest,
    RunnerHandle,
    RunnerRegistrationError,
    RunnerSafetyContract,
)
from vector_lake.auto_ingest_runners.codex_exec import (
    CODEX_EXEC_ADAPTER,
    CodexExecAdapter,
    _gate,
)


# Module-level enforcers with the reviewed names.  They live in this module so a
# synthetic adapter defined here resolves its declared enforcer by object identity,
# exactly as a real adapter resolves one on its own module.
def _pinned_runner_binary(*_args, **_kwargs):
    return None


def _probe_codex_runner_unmanaged(*_args, **_kwargs):
    return None


def _validated_runner_home(*_args, **_kwargs):
    return None


def _verify_runner_identity(*_args, **_kwargs):
    return None


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

_ENFORCERS = {
    "binary_pin": ("_pinned_runner_binary", "codex_binary_hash_mismatch"),
    "version_pin": ("_probe_codex_runner_unmanaged", "codex_version_mismatch"),
    "dedicated_home": ("_validated_runner_home", "codex_runner_home_is_not_dedicated"),
    "instruction_surface_prohibited": (
        "_validated_runner_home",
        "codex_runner_home_contains_instruction_surfaces",
    ),
    "user_skills_prohibited": (
        "_validated_runner_home",
        "codex_runner_skills_contains_user_content",
    ),
    "identity_pin": ("_verify_runner_identity", "codex_auth_identity_mismatch"),
}


class _FakeAdapter:
    """A real class so ``type(adapter).__module__`` is this test module."""

    def __init__(self, contract):
        self.safety_contract = contract


def _contract(**overrides):
    values = {
        field: _gate(name, raises, globals()[name])
        for field, (name, raises) in _ENFORCERS.items()
    }
    values["credential_actions"] = "forbidden"
    values.update(overrides)
    return RunnerSafetyContract(**values)


def test_registry_is_a_frozen_whitelist_without_public_registration():
    import vector_lake.auto_ingest_runners as registry

    assert isinstance(registry._REGISTRY, MappingProxyType)
    assert set(registry._REGISTRY) == {"codex_exec", "host_relay"}
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
    adapter = _FakeAdapter(
        RunnerSafetyContract(
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
    adapter = _FakeAdapter(
        _contract(version_pin=_gate("_verify_runner_identity", "codex_auth_identity_mismatch", _verify_runner_identity))
    )
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_gate_enforcer_mismatch:version_pin"


def test_missing_gate_and_bad_credential_actions_are_rejected():
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", _FakeAdapter(None), _CODEX_MANIFEST)
    assert str(exc.value) == "runner_contract_incomplete:binary_pin"

    adapter = _FakeAdapter(_contract(credential_actions="allowed"))
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_contract_credential_actions_invalid"


def test_not_applicable_gate_requires_a_reason_and_unknown_manifest_fails():
    manifest = dict(_CODEX_MANIFEST)
    manifest["identity_pin"] = ("not_applicable", "no model credential is used")
    adapter = _FakeAdapter(_contract(identity_pin=("not_applicable", "reason")))
    _validate_contract("codex_exec", adapter, MappingProxyType(manifest))

    bad = _FakeAdapter(_contract(identity_pin=("not_applicable", "")))
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", bad, MappingProxyType(manifest))
    assert str(exc.value) == "runner_contract_incomplete:identity_pin"

    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("no_such_runner", CODEX_EXEC_ADAPTER)
    assert str(exc.value) == "runner_manifest_missing:no_such_runner"


def test_declared_name_must_be_the_wired_module_function():
    """A reviewed name plus a different callable must be rejected.

    Without this, an adapter could declare a reviewed enforcer and wire a no-op.
    """
    adapter = _FakeAdapter(
        _contract(dedicated_home=_gate("_validated_runner_home", "codex_runner_home_is_not_dedicated", lambda *_a, **_k: None))
    )
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_gate_enforcer_unresolved:dedicated_home"


def test_gate_naming_a_nonexistent_function_is_rejected():
    adapter = _FakeAdapter(
        _contract(version_pin=_gate("_no_such_enforcer", "codex_version_mismatch", _probe_codex_runner_unmanaged))
    )
    with pytest.raises(RunnerRegistrationError) as exc:
        _validate_contract("codex_exec", adapter, _CODEX_MANIFEST)
    assert str(exc.value) == "runner_gate_enforcer_mismatch:version_pin"


def test_codex_gate_mapping_names_the_real_enforcer():
    contract = CodexExecAdapter.safety_contract
    expected = {
        "binary_pin": "_pinned_runner_binary",
        "version_pin": "_probe_codex_runner_unmanaged",
        "dedicated_home": "_validated_runner_home",
        "instruction_surface_prohibited": "_validated_runner_home",
        "user_skills_prohibited": "_validated_runner_home",
        "identity_pin": "_verify_runner_identity",
    }
    for field, enforcer in expected.items():
        gate = getattr(contract, field)
        assert getattr(gate, "enforcer", None) == enforcer, field
        assert callable(getattr(gate, "enforcer_impl", None)), field
        assert callable(getattr(auto_ingest_worker, enforcer, None)), field
    assert contract.credential_actions == "forbidden"


def _assert_declared_failure(field, invoke):
    gate = getattr(CodexExecAdapter.safety_contract, field)
    with pytest.raises(auto_ingest_worker.AutoIngestInfrastructureError) as exc:
        invoke(gate.enforcer_impl)
    assert str(exc.value).partition(":")[0] == gate.raises


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"x.{payload.decode()}.x"


def _valid_runner_fixture(tmp_path):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"synthetic codex")
    home = tmp_path / "runner-home"
    system = home / "skills" / ".system"
    system.mkdir(parents=True)
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {"account_id": "account", "id_token": _jwt({"sub": "subject"})},
    }
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    cache = home / "models_cache.json"
    cache.write_text("{}", encoding="utf-8")
    cache.chmod(stat.S_IREAD)
    config = auto_ingest_worker.AutoIngestConfig(
        codex_executable=str(executable),
        runner_codex_home=str(home),
        required_codex_version="1.2.3",
        required_codex_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        required_system_skills_sha256=auto_ingest_worker._directory_tree_digest(system),
        required_models_cache_sha256=hashlib.sha256(cache.read_bytes()).hexdigest(),
    )
    return executable, config, auto_ingest_worker._auth_identity_digest(config)


def test_real_binary_pin_enforces_declared_failure(tmp_path):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"synthetic")
    _assert_declared_failure(
        "binary_pin", lambda enforcer: enforcer(executable, "0" * 64).__enter__()
    )


def test_real_home_gates_enforce_declared_failures(tmp_path, monkeypatch):
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(dedicated))
    _assert_declared_failure(
        "dedicated_home",
        lambda enforcer: enforcer(auto_ingest_worker.AutoIngestConfig(runner_codex_home=str(dedicated))),
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "inherited"))
    instruction = tmp_path / "instruction"
    instruction.mkdir()
    (instruction / ".agents").mkdir()
    _assert_declared_failure(
        "instruction_surface_prohibited",
        lambda enforcer: enforcer(auto_ingest_worker.AutoIngestConfig(runner_codex_home=str(instruction))),
    )
    skills_home = tmp_path / "skills-home"
    (skills_home / "skills" / ".system").mkdir(parents=True)
    (skills_home / "skills" / "user-skill").mkdir()
    _assert_declared_failure(
        "user_skills_prohibited",
        lambda enforcer: enforcer(auto_ingest_worker.AutoIngestConfig(runner_codex_home=str(skills_home))),
    )


def test_real_identity_pin_enforces_declared_failure(tmp_path):
    executable, config, digest = _valid_runner_fixture(tmp_path)
    mismatched = replace(
        config, required_auth_identity_sha256=("0" * 64 if digest != "0" * 64 else "1" * 64)
    )
    _assert_declared_failure(
        "identity_pin", lambda enforcer: enforcer(executable, mismatched)
    )


def test_real_version_pin_enforces_declared_failure(tmp_path, monkeypatch):
    _, config, digest = _valid_runner_fixture(tmp_path)
    config = replace(config, required_auth_identity_sha256=digest)
    monkeypatch.setattr(auto_ingest_worker, "_safe_environment", lambda _config: {})
    monkeypatch.setattr(
        auto_ingest_worker, "_validated_auto_scratch_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        auto_ingest_worker,
        "_run_contained_probe",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli 9.9.9", stderr=""
        ),
    )
    _assert_declared_failure("version_pin", lambda enforcer: enforcer(config))


@pytest.mark.parametrize(
    "document",
    [
        {"protocol": "vector-lake-ingest-relay/v1", "attempt_id": "attempt", "lease_generation": 2, "nonce": "wrong"},
        {"protocol": "vector-lake-ingest-relay/v1", "attempt_id": "wrong", "lease_generation": 2, "nonce": "expected-nonce"},
        {"protocol": "vector-lake-ingest-relay/v1", "attempt_id": "attempt", "lease_generation": 3, "nonce": "expected-nonce"},
    ],
)
def test_each_host_relay_gate_enforces_its_declared_failure(document):
    adapter = get_runner_adapter("host_relay")
    version_gate = adapter.safety_contract.version_pin
    with pytest.raises(ValueError) as exc:
        version_gate.enforcer_impl("unsupported")
    assert str(exc.value) == version_gate.raises

    request = GenerationRequest(
        "job-1", ("owner", "token", 2), "attempt", "p", 1, 2, 3
    )
    binding_gate = adapter.safety_contract.identity_pin
    with pytest.raises(auto_ingest_worker.AutoIngestInfrastructureError) as exc:
        binding_gate.enforcer_impl(document, request, "expected-nonce")
    assert str(exc.value) == binding_gate.raises


def test_codex_contract_and_generate_forwarding(monkeypatch):
    adapter = CodexExecAdapter()
    config = auto_ingest_worker.AutoIngestConfig()
    options = adapter.validate_options(config)
    result = auto_ingest_worker._GeneratedOutput({"ok": True}, {"total": 1})
    calls = []

    def fake_run(*args):
        calls.append(args)
        return result

    monkeypatch.setattr(auto_ingest_worker, "_run_codex_generator", fake_run)
    # Build the handle directly: a real probe would require a live isolated runner
    # home, which is deliberately out of scope for this unit test.
    handle = RunnerHandle(resource=Path("C:/codex.exe"), options=options)
    request = GenerationRequest("job-1", ("owner", "token", 2), "attempt", "p", 1, 2, 3)
    stop_event = object()
    health_check = lambda: None

    actual = adapter.generate(handle, request, stop_event, health_check)

    assert actual is result
    assert calls == [(handle.resource, config, "job-1", request.lease, "p", stop_event, health_check)]


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

    handle = adapter.probe(options)
    assert handle.resource is expected
    assert handle.options is options
    assert calls == [config]
