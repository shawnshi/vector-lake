"""Types for the inert auto-ingest runner seam.

The controller remains on its Codex call sites in this slice.  Switching to
this seam is gated on a separately reviewed byte-equivalence corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Protocol, TypeAlias


NotApplicableGate: TypeAlias = tuple[Literal["not_applicable"], str]
SafetyGate: TypeAlias = Callable[..., Any] | NotApplicableGate

@dataclass(frozen=True)
class RunnerHandle:
    """A probed resource paired with its immutable validated options."""

    resource: Any
    options: "RunnerOptions"


@dataclass(frozen=True)
class RunnerOptions:
    """Validated runner-specific values plus the unchanged controller config."""

    values: Mapping[str, Any]
    config: Any


@dataclass(frozen=True)
class RunnerSafetyContract:
    binary_pin: SafetyGate
    version_pin: SafetyGate
    dedicated_home: SafetyGate
    instruction_surface_prohibited: SafetyGate
    user_skills_prohibited: SafetyGate
    identity_pin: SafetyGate
    credential_actions: str


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    lease: tuple[str, str, int]
    attempt_id: str
    prompt: str
    max_input_bytes: int
    max_output_bytes: int
    timeout_seconds: int


class GeneratorResult(Protocol):
    """Controller payload contract.

    The Codex adapter returns the existing ``_GeneratedOutput`` dict subclass,
    including its ``usage`` attribute, unchanged.
    """

    usage: dict[str, int]


class RunnerAdapter(Protocol):
    safety_contract: RunnerSafetyContract

    def validate_options(self, raw: Mapping[str, Any]) -> RunnerOptions: ...

    def probe(self, options: RunnerOptions) -> RunnerHandle: ...

    def generate(
        self,
        handle: RunnerHandle,
        request: GenerationRequest,
        stop_event: Any,
        health_check: Callable[[], None] | None,
    ) -> GeneratorResult: ...


class RunnerRegistrationError(ValueError):
    """A registry error whose message is restricted to fixed error codes."""
