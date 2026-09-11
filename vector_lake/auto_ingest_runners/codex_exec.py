"""Thin Codex adapter for the inert runner seam.

The controller switch is deferred until a separate byte-equivalence corpus
has been reviewed and is empty.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .base import GenerationRequest, RunnerOptions, RunnerSafetyContract


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


def _gate(enforcer: str, raises: str) -> Callable[..., Any]:
    """Bind one contract gate to the worker function that actually enforces it.

    The ``enforcer`` attribute is machine-checkable so a registered runner cannot
    silently claim a gate that its declared verifier does not implement.
    """

    def verify(*args: Any, **kwargs: Any) -> Any:
        return _worker_call(enforcer, *args, **kwargs)

    verify.__name__ = f"enforce_{enforcer.lstrip('_')}"
    verify.__doc__ = f"Enforced by {enforcer}; can raise {raises}."
    verify.enforcer = enforcer  # type: ignore[attr-defined]
    verify.raises = raises  # type: ignore[attr-defined]
    return verify


class CodexExecAdapter:
    safety_contract = RunnerSafetyContract(
        # Binary identity is enforced by the pinned-handle hasher itself.
        binary_pin=_gate("_pinned_runner_binary", "codex_binary_hash_mismatch"),
        # The version pin is enforced by the runner probe, not by the identity
        # digest helper.
        version_pin=_gate(
            "_probe_codex_runner_unmanaged", "codex_version_mismatch"
        ),
        # _validated_runner_home enforces dedicated-home isolation as one unit; the
        # three home-derived gates therefore name it explicitly rather than
        # pointing at unrelated helpers.
        dedicated_home=_gate(
            "_validated_runner_home",
            "codex_runner_home_is_not_dedicated",
        ),
        instruction_surface_prohibited=_gate(
            "_validated_runner_home",
            "codex_runner_home_contains_instruction_surfaces",
        ),
        user_skills_prohibited=_gate(
            "_validated_runner_home",
            "codex_runner_skills_contains_user_content",
        ),
        # The comparison lives in _verify_runner_identity; _auth_identity_digest
        # only computes the digest.
        identity_pin=_gate(
            "_verify_runner_identity", "codex_auth_identity_mismatch"
        ),
        credential_actions="forbidden",
    )

    def __init__(self) -> None:
        self._options: RunnerOptions | None = None

    def validate_options(self, raw: Mapping[str, Any]) -> RunnerOptions:
        from vector_lake.auto_ingest_worker import AutoIngestConfig

        config = raw if isinstance(raw, AutoIngestConfig) else AutoIngestConfig(
            **{key: raw[key] for key in _CODEX_OPTION_KEYS if key in raw}
        )
        options = RunnerOptions(
            values={key: getattr(config, key) for key in _CODEX_OPTION_KEYS},
            config=config,
        )
        self._options = options
        return options

    def probe(self, options: RunnerOptions):
        self._options = options
        return _worker_call("_probe_codex_runner", options.config)

    def generate(
        self,
        handle,
        request: GenerationRequest,
        stop_event: Any,
        health_check: Callable[[], None] | None,
    ):
        if self._options is None:
            raise RuntimeError("runner_options_not_validated")
        return _worker_call(
            "_run_codex_generator",
            handle,
            self._options.config,
            request.job_id,
            request.lease,
            request.prompt,
            stop_event,
            health_check,
        )


CODEX_EXEC_ADAPTER = CodexExecAdapter()

