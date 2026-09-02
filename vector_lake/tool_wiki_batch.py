"""Bounded, fingerprint-gated Wiki maintenance batches."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unicodedata
from collections.abc import Callable

from vector_lake import db_store, governance_store, mutation_coordinator
from vector_lake.mutation_coordinator import (
    _prepare_mutations,
    execute_mutation_batch,
    resolve_wiki_mutation_path,
)

_MANIFEST_SCHEMA_VERSION = 1
_MAX_BATCH_ITEMS = 50
_DEFAULT_MAX_BATCH_BYTES = 16 * 1024 * 1024
_HARD_MAX_BATCH_BYTES = 64 * 1024 * 1024
_MANIFEST_KEYS = frozenset(
    {"schema_version", "operations", "schema_maintenance_filenames"}
)
_OPERATION_KEYS = frozenset(
    {
        "filename",
        "payload_file",
        "expected_version",
        "expected_projection_hash",
    }
)


class SchemaMaintenanceNotAuthorized(PermissionError):
    """Raised when a public manifest requests an unapproved validation downgrade."""


class SystemPageWriteNotAuthorized(PermissionError):
    """Raised when a batch targets a protected System page."""


def schema_maintenance_allowlist_from_env(value: str | None) -> frozenset[str]:
    """Parse an exact trusted-host allowlist; an absent value authorizes nothing."""
    if not value:
        return frozenset()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Schema-maintenance host allowlist must be valid JSON.") from exc
    if (
        not isinstance(parsed, list)
        or any(not isinstance(filename, str) or not filename for filename in parsed)
        or len(parsed) != len(set(parsed))
        or any(
            filename != filename.strip()
            or not filename.casefold().endswith(".md")
            or any(marker in filename for marker in ("/", "\\", "\x00"))
            for filename in parsed
        )
    ):
        raise ValueError(
            "Schema-maintenance host allowlist must contain unique Wiki basenames."
        )
    return frozenset(parsed)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_key(filename: str) -> str:
    return filename[:-3] if filename.casefold().endswith(".md") else filename


def _validated_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest.")
    normalized = value.casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest.")
    return normalized


def _validated_manifest(manifest_text: str) -> tuple[list[dict], list[str]]:
    try:
        manifest = json.loads(manifest_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Wiki batch manifest must be valid JSON.") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("Wiki batch manifest fields do not match schema version 1.")
    if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported Wiki batch manifest schema version.")

    operations = manifest.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("Wiki batch manifest requires at least one operation.")
    if len(operations) > _MAX_BATCH_ITEMS:
        raise ValueError(
            f"Wiki batch operation count exceeds {_MAX_BATCH_ITEMS}."
        )
    if any(not isinstance(operation, dict) for operation in operations):
        raise ValueError("Wiki batch operations must be objects.")
    if any(set(operation) != _OPERATION_KEYS for operation in operations):
        raise ValueError("Wiki batch operation fields do not match schema version 1.")

    maintenance = manifest.get("schema_maintenance_filenames")
    if not isinstance(maintenance, list) or any(
        not isinstance(filename, str) for filename in maintenance
    ):
        raise ValueError("schema_maintenance_filenames must be a string list.")
    if len(maintenance) != len(set(maintenance)):
        raise ValueError("schema_maintenance_filenames must be unique.")
    return operations, maintenance


def build_wiki_batch_plan(
    manifest_text: str,
    payload_loader: Callable[[str], str],
    *,
    allow_system_pages: bool = False,
    allowed_schema_maintenance_filenames: frozenset[str] = frozenset(),
) -> tuple[dict, list[dict], list[str]]:
    """Validate a batch and bind its preview to current canonical/projection state."""
    operations, maintenance = _validated_manifest(manifest_text)
    unauthorized_maintenance = set(maintenance) - set(
        allowed_schema_maintenance_filenames
    )
    if unauthorized_maintenance:
        raise SchemaMaintenanceNotAuthorized(
            "Schema-maintenance filenames were not authorized by the trusted host."
        )
    configured_max_bytes = int(
        os.environ.get(
            "VECTOR_LAKE_WIKI_BATCH_MAX_BYTES",
            str(_DEFAULT_MAX_BATCH_BYTES),
        )
    )
    max_batch_bytes = min(max(1, configured_max_bytes), _HARD_MAX_BATCH_BYTES)
    mutations: list[dict] = []
    plan_operations: list[dict] = []
    aggregate_bytes = 0
    metadata: list[tuple[dict, str]] = []

    # Reject the entire batch's metadata and authorization policy before any
    # page payload is opened.
    for operation in operations:
        filename = operation["filename"]
        payload_file = operation["payload_file"]
        expected_version = operation["expected_version"]
        if not isinstance(filename, str) or not filename:
            raise ValueError("Wiki batch filename must be a non-empty string.")
        if not isinstance(payload_file, str) or not payload_file:
            raise ValueError("Wiki batch payload_file must be a non-empty string.")
        if not isinstance(expected_version, str) or not expected_version:
            raise ValueError("Wiki batch expected_version must be non-empty.")
        normalized_filename = unicodedata.normalize("NFKC", filename).casefold()
        if normalized_filename.startswith("system_") and not allow_system_pages:
            raise SystemPageWriteNotAuthorized(
                "System wiki page writes are disabled by default."
            )
        expected_projection_hash = _validated_sha256(
            operation["expected_projection_hash"],
            "expected_projection_hash",
        )
        metadata.append((operation, expected_projection_hash))

    mutation_coordinator.validate_mutation_batch_metadata(
        [
            {
                "filename": operation["filename"],
                "is_delete": False,
                "expected_version": operation["expected_version"],
                "expected_projection_hash": expected_projection_hash,
            }
            for operation, expected_projection_hash in metadata
        ],
        validation_mode="full",
        schema_maintenance_filenames=maintenance,
    )

    for operation, expected_projection_hash in metadata:
        filename = operation["filename"]
        payload_file = operation["payload_file"]
        expected_version = operation["expected_version"]
        content = payload_loader(payload_file)
        if not isinstance(content, str):
            raise ValueError("Wiki batch payload loader must return text.")
        content_bytes = content.encode("utf-8")
        aggregate_bytes += len(content_bytes)
        if aggregate_bytes > max_batch_bytes:
            raise ValueError(
                f"Wiki batch payload bytes exceed {max_batch_bytes}."
            )
        payload_sha256 = hashlib.sha256(content_bytes).hexdigest()
        if hmac.compare_digest(payload_sha256, expected_projection_hash):
            raise ValueError(f"Wiki batch operation is a no-op: {filename}")
        mutations.append(
            {
                "filename": filename,
                "content": content,
                "is_delete": False,
                "expected_version": expected_version,
                "expected_projection_hash": expected_projection_hash,
            }
        )
        plan_operations.append(
            {
                "filename": filename,
                "payload_sha256": payload_sha256,
                "expected_version": expected_version,
                "expected_projection_hash": expected_projection_hash,
                "validation_mode": (
                    "schema" if filename in maintenance else "full"
                ),
            }
        )

    _prepare_mutations(
        mutations,
        validation_mode="full",
        schema_maintenance_filenames=maintenance,
    )
    page_keys = {_page_key(operation["filename"]) for operation in operations}
    db_store.init_db()
    current_versions = governance_store.canonical_page_versions(page_keys)
    for operation in operations:
        filename = operation["filename"]
        expected_version = operation["expected_version"]
        current_version = current_versions.get(_page_key(filename))
        if not hmac.compare_digest(current_version or "", expected_version):
            raise ValueError(f"Canonical version changed for {filename}.")
        current_path = resolve_wiki_mutation_path(
            filename,
            allow_existing_legacy_name=filename in maintenance,
        )
        if not current_path.is_file():
            raise ValueError(f"Wiki batch target does not exist: {filename}")
        current_hash = hashlib.sha256(current_path.read_bytes()).hexdigest()
        expected_hash = operation["expected_projection_hash"].casefold()
        if not hmac.compare_digest(current_hash, expected_hash):
            raise ValueError(f"Projection changed for {filename}.")

    fingerprint_basis = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "operations": plan_operations,
        "schema_maintenance_filenames": maintenance,
    }
    fingerprint = "sha256:" + _sha256_text(
        json.dumps(
            fingerprint_basis,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    plan = {
        **fingerprint_basis,
        "operation_count": len(plan_operations),
        "aggregate_payload_bytes": aggregate_bytes,
        "confirmation_required": True,
        "fingerprint": fingerprint,
    }
    return plan, mutations, maintenance


def run_wiki_batch(
    manifest_text: str,
    payload_loader: Callable[[str], str],
    *,
    dry_run: bool = True,
    confirmation: str = "",
    allow_system_pages: bool = False,
    allowed_schema_maintenance_filenames: frozenset[str] = frozenset(),
) -> dict:
    """Preview or atomically commit canonical state for one exact Wiki batch.

    Markdown projections are published after the canonical transaction and may
    be reported as deferred for watchdog repair.
    """
    plan, mutations, maintenance = build_wiki_batch_plan(
        manifest_text,
        payload_loader,
        allow_system_pages=allow_system_pages,
        allowed_schema_maintenance_filenames=allowed_schema_maintenance_filenames,
    )
    if dry_run:
        return {
            "schema_version": 1,
            "ok": True,
            "dry_run": True,
            "committed": False,
            "operation_count": plan["operation_count"],
            "aggregate_payload_bytes": plan["aggregate_payload_bytes"],
            "schema_maintenance_count": len(maintenance),
            "confirmation_required": True,
            "fingerprint": plan["fingerprint"],
            "operations": plan["operations"],
            "outbox_ids": [],
            "deferred": [],
            "post_commit_warnings": [],
            "error_code": None,
            "message": "Wiki batch preview validated; no changes committed.",
        }
    if not confirmation or not hmac.compare_digest(
        confirmation,
        str(plan["fingerprint"]),
    ):
        raise ValueError(
            "confirmation must exactly match the current Wiki batch fingerprint."
        )

    details = execute_mutation_batch(
        mutations,
        validation_mode="full",
        origin="mcp_write_wiki_batch",
        return_details=True,
        schema_maintenance_filenames=maintenance,
    )
    if not isinstance(details, dict):
        raise RuntimeError("Mutation coordinator did not return detail fields.")
    outbox_ids = [int(value) for value in details.get("outbox_ids", [])]
    deferred = [str(value) for value in details.get("deferred", [])]
    warnings = [
        "post_commit_follow_up_warning"
        for _warning in list(details.get("post_commit_warnings", []))
    ]
    committed = bool(details.get("committed"))
    ok = bool(details.get("ok")) and committed
    return {
        "schema_version": 1,
        "ok": ok,
        "dry_run": False,
        "committed": committed,
        "operation_count": plan["operation_count"],
        "aggregate_payload_bytes": plan["aggregate_payload_bytes"],
        "schema_maintenance_count": len(maintenance),
        "confirmation_required": False,
        "fingerprint": plan["fingerprint"],
        "operations": plan["operations"],
        "outbox_ids": outbox_ids,
        "deferred": deferred,
        "post_commit_warnings": warnings,
        "error_code": None if ok else "mutation_not_committed",
        "message": (
            "Canonical Wiki batch committed; "
            f"outbox={len(outbox_ids)}; deferred={len(deferred)}; "
            f"warnings={len(warnings)}."
            if committed
            else "Canonical Wiki batch was not committed."
        ),
    }
