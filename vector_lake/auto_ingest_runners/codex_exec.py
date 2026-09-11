"""Thin Codex adapter for the inert runner seam.

The controller switch is deferred until a separate byte-equivalence corpus
has been reviewed and is empty.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .base import GenerationRequest, RunnerHandle, RunnerOptions, RunnerSafetyContract


_CODEX_OPTION_KEYS = (
    "codex_executable",
    "runner_codex_home",
    "required_codex_version",
    "required_codex_sha256",
    "required_system_skills_sha256",
    "required_models_cache_sha256",
    "required_auth_identity_sha256",
    "model",
    "reasoning_effort",
)


def _worker_call(name: str, *args: Any, **kwargs: Any) -> Any:
    from vector_lake import auto_ingest_worker

    return getattr(auto_ingest_worker, name)(*args, **kwargs)


def _gate(
    enforcer: str,
    raises: str,
    verifier: Callable[..., Any],
) -> Callable[..., Any]:
    """Bind one contract gate to the module-level callable that enforces it.

    ``enforcer`` must name a module-level callable on the adapter's own module and
    ``verifier`` must be that exact object.  ``enforcer``, ``raises`` and
    ``enforcer_impl`` are machine-checkable so a registered runner cannot claim a
    gate with a declaration that is not backed by the reviewed function.
    """

    def verify(*args: Any, **kwargs: Any) -> Any:
        return verifier(*args, **kwargs)

    verify.__name__ = f"enforce_{enforcer.lstrip('_')}"
    verify.__doc__ = f"Enforced by {enforcer}; can raise {raises}."
    verify.enforcer = enforcer  # type: ignore[attr-defined]
    verify.raises = raises  # type: ignore[attr-defined]
    verify.enforcer_impl = verifier  # type: ignore[attr-defined]
    return verify


# Module-level thin forwarders.  Each one is the single reviewed enforcer named by
# the manifest, and each defers the worker import so no module-import cycle exists.
def _pinned_runner_binary(*args: Any, **kwargs: Any) -> Any:
    return _worker_call("_pinned_runner_binary", *args, **kwargs)


def _probe_codex_runner_unmanaged(*args: Any, **kwargs: Any) -> Any:
    return _worker_call("_probe_codex_runner_unmanaged", *args, **kwargs)


def _validated_runner_home(*args: Any, **kwargs: Any) -> Any:
    return _worker_call("_validated_runner_home", *args, **kwargs)


def _verify_runner_identity(*args: Any, **kwargs: Any) -> Any:
    return _worker_call("_verify_runner_identity", *args, **kwargs)


class CodexExecAdapter:
    safety_contract = RunnerSafetyContract(
        # Binary identity is enforced by the pinned-handle hasher itself.
        binary_pin=_gate(
            "_pinned_runner_binary",
            "codex_binary_hash_mismatch",
            _pinned_runner_binary,
        ),
        # The version pin is enforced by the runner probe, not by the identity
        # digest helper.
        version_pin=_gate(
            "_probe_codex_runner_unmanaged",
            "codex_version_mismatch",
            _probe_codex_runner_unmanaged,
        ),
        # _validated_runner_home enforces dedicated-home isolation as one unit; the
        # three home-derived gates therefore name it explicitly rather than
        # pointing at unrelated helpers.
        dedicated_home=_gate(
            "_validated_runner_home",
            "codex_runner_home_is_not_dedicated",
            _validated_runner_home,
        ),
        instruction_surface_prohibited=_gate(
            "_validated_runner_home",
            "codex_runner_home_contains_instruction_surfaces",
            _validated_runner_home,
        ),
        user_skills_prohibited=_gate(
            "_validated_runner_home",
            "codex_runner_skills_contains_user_content",
            _validated_runner_home,
        ),
        # The comparison lives in _verify_runner_identity; _auth_identity_digest
        # only computes the digest.
        identity_pin=_gate(
            "_verify_runner_identity",
            "codex_auth_identity_mismatch",
            _verify_runner_identity,
        ),
        credential_actions="forbidden",
    )

    def validate_options(self, raw: Mapping[str, Any]) -> RunnerOptions:
        from vector_lake.auto_ingest_worker import AutoIngestConfig

        config = raw if isinstance(raw, AutoIngestConfig) else AutoIngestConfig(
            **{key: raw[key] for key in _CODEX_OPTION_KEYS if key in raw}
        )
        options = RunnerOptions(
            values={key: getattr(config, key) for key in _CODEX_OPTION_KEYS},
            config=config,
        )
        return options

    def probe(self, options: RunnerOptions) -> RunnerHandle:
        resource = _worker_call("_probe_codex_runner", options.config)
        return RunnerHandle(resource=resource, options=options)

    def generate(
        self,
        handle,
        request: GenerationRequest,
        stop_event: Any,
        health_check: Callable[[], None] | None,
    ):
        return _worker_call(
            "_run_codex_generator",
            handle.resource,
            handle.options.config,
            request.job_id,
            request.lease,
            request.prompt,
            stop_event,
            health_check,
        )


CODEX_EXEC_ADAPTER = CodexExecAdapter()
