"""Static registry for an inert auto-ingest runner seam.

The controller still uses its direct Codex calls.  A later reviewed slice may
switch it only after the byte-equivalence corpus is empty.

Registration policy
-------------------
The registry is frozen at import time from ``_REVIEWED_MANIFESTS``.  There is no
public registration API and no dynamic import: adding a runner requires editing
this reviewed data, which is deliberate friction.

What the manifest check does and does not prove
-----------------------------------------------
``_validate_contract`` proves that an adapter *declares* each gate against the
reviewed manifest and that the declared name resolves to the reviewed
module-level object wired into the gate.  It does **not** prove that the function
enforces anything.  Enforcement for every applicable gate is established by
behavioural tests plus review.  Declarations are checked here; behaviour there.
"""

from __future__ import annotations

import sys
from types import MappingProxyType
from typing import Mapping

from .base import RunnerAdapter, RunnerRegistrationError, RunnerSafetyContract
from .codex_exec import CODEX_EXEC_ADAPTER
from .host_relay import HOST_RELAY_ADAPTER


_APPLICABLE_GATES = (
    "binary_pin",
    "version_pin",
    "dedicated_home",
    "instruction_surface_prohibited",
    "user_skills_prohibited",
    "identity_pin",
)

# Reviewed data.  Each entry is either
#   ("applicable", <module-level callable name on the adapter's module>,
#    <fixed error code>) or
#   ("not_applicable", <non-empty reason>).
# For an applicable gate the named callable must exist on the adapter's own module
# and must be the exact object the gate is wired to; the registry verifies that
# identity.  This proves wiring, not the callable's enforcement behaviour.
_REVIEWED_MANIFESTS: Mapping[str, Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "codex_exec": MappingProxyType(
            {
                "binary_pin": (
                    "applicable",
                    "_pinned_runner_binary",
                    "codex_binary_hash_mismatch",
                ),
                "version_pin": (
                    "applicable",
                    "_probe_codex_runner_unmanaged",
                    "codex_version_mismatch",
                ),
                "dedicated_home": (
                    "applicable",
                    "_validated_runner_home",
                    "codex_runner_home_is_not_dedicated",
                ),
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
                "identity_pin": (
                    "applicable",
                    "_verify_runner_identity",
                    "codex_auth_identity_mismatch",
                ),
            }
        ),
        "host_relay": MappingProxyType(
            {
                "binary_pin": (
                    "not_applicable",
                    "No executable is spawned because the relay protocol is data-only.",
                ),
                "version_pin": (
                    "applicable",
                    "_enforce_relay_protocol_version",
                    "relay_protocol_version_unsupported",
                ),
                "dedicated_home": (
                    "not_applicable",
                    "The data-only relay has no runner home.",
                ),
                "instruction_surface_prohibited": (
                    "not_applicable",
                    "Compensating controls are the pinned spool root, data-only response, downstream purpose contract, and output validation.",
                ),
                "user_skills_prohibited": (
                    "not_applicable",
                    "The data-only relay has no skills tree.",
                ),
                "identity_pin": (
                    "applicable",
                    "_enforce_relay_response_binding",
                    "relay_response_binding_mismatch",
                ),
            }
        ),
    }
)


def _validate_contract(
    name: str,
    adapter: object,
    manifest: Mapping[str, tuple[str, str]] | None = None,
) -> None:
    """Validate one adapter's declared gates against a reviewed manifest."""
    reviewed = _REVIEWED_MANIFESTS.get(name) if manifest is None else manifest
    if reviewed is None:
        raise RunnerRegistrationError(f"runner_manifest_missing:{name}")
    contract: RunnerSafetyContract | None = getattr(adapter, "safety_contract", None)
    for gate in _APPLICABLE_GATES:
        entry = reviewed.get(gate)
        if entry is None:
            raise RunnerRegistrationError(f"runner_contract_incomplete:{gate}")
        expected_kind = entry[0]
        declared = getattr(contract, gate, None) if contract is not None else None
        if expected_kind == "not_applicable":
            if not (
                isinstance(declared, tuple)
                and len(declared) == 2
                and declared[0] == "not_applicable"
                and isinstance(declared[1], str)
                and declared[1].strip()
            ):
                raise RunnerRegistrationError(f"runner_contract_incomplete:{gate}")
            continue
        # Applicable gate: the manifest names the only acceptable enforcer pair.
        if not callable(declared):
            raise RunnerRegistrationError(f"runner_contract_incomplete:{gate}")
        if (
            getattr(declared, "enforcer", None) != entry[1]
            or getattr(declared, "raises", None) != entry[2]
        ):
            raise RunnerRegistrationError(f"runner_gate_enforcer_mismatch:{gate}")
        # The declared name must resolve to the exact module-level callable that the
        # gate is wired to, on the adapter's own module.  Metadata alone would let a
        # runner name a reviewed enforcer while wiring a different object.  The
        # adapter module is used so this check never imports the worker module
        # during the import cycle.  Behavioural tests plus review establish that
        # the resolved object actually enforces the gate.
        adapter_module = sys.modules.get(type(adapter).__module__)
        resolved = getattr(adapter_module, entry[1], None) if adapter_module else None
        if resolved is None or getattr(declared, "enforcer_impl", None) is not resolved:
            raise RunnerRegistrationError(f"runner_gate_enforcer_unresolved:{gate}")
    if contract is None or contract.credential_actions != "forbidden":
        raise RunnerRegistrationError("runner_contract_credential_actions_invalid")


_REGISTRY: Mapping[str, RunnerAdapter] = MappingProxyType(
    {"codex_exec": CODEX_EXEC_ADAPTER, "host_relay": HOST_RELAY_ADAPTER}
)

# Import-time, fail-closed: a registry entry that does not match its reviewed
# manifest stops the module from loading rather than degrading silently.
for _name, _adapter in _REGISTRY.items():
    _validate_contract(_name, _adapter)
del _name, _adapter


def get_runner_adapter(name: str) -> RunnerAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise RunnerRegistrationError(f"runner_is_unknown:{name}") from None


__all__ = ["get_runner_adapter"]
