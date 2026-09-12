"""Fingerprint-gated canonical deletion for exact Wiki pages.

The reviewed mutation primitives already support ``is_delete`` (``delete_source``,
``gc_vector_lake``, ``lint_vector_lake`` and the governance merge path all use
them).  This module exposes that primitive directly for an operator-selected set
of pages, with the same fail-closed shape as :mod:`vector_lake.tool_wiki_batch`:

* the whole batch is validated against current state before anything mutates,
* every operation carries a canonical version and a projection hash,
* the batch is bound to one fingerprint that must be echoed back verbatim,
* one complete maintenance backup is published per committed batch,
* an archive copy of every page is published before deletion,
* a page that any other page links to is refused.

Deletion removes the canonical page and cascades its own claims, evidence and
outgoing graph edges (``db_store.delete_node_cascade``); removed versions stay in
``claim_versions``/``evidence_versions`` as history.  The Markdown projection is
published by the durable mutation outbox, so a successful call reports
``deferred`` entries rather than asserting that the file is already gone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import unicodedata

from vector_lake.wiki_utils import (
    get_wiki_dir,
    iter_markdown_files,
    iter_wiki_link_matches,
    validate_wiki_filename,
)

_MANIFEST_SCHEMA_VERSION = 1
_CONTRACT = "vector-lake-wiki-page-delete/v1"
_MAX_BATCH_ITEMS = 50
_MANIFEST_KEYS = frozenset({"schema_version", "operations", "archive"})
_OPERATION_KEYS = frozenset(
    {"filename", "expected_version", "expected_projection_hash"}
)
_ARCHIVE_DIR_NAME = ".archive"


class SystemPageDeleteNotAuthorized(PermissionError):
    """Raised when a batch targets a protected System page."""


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


def _validated_version(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Wiki delete expected_version must be a string.")
    normalized = value.strip()
    if normalized and (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized.casefold())
    ):
        raise ValueError("Wiki delete expected_version must be empty or a SHA-256 hex digest.")
    return normalized.casefold()


def _validated_manifest(manifest_text: str) -> tuple[list[dict], bool]:
    try:
        manifest = json.loads(manifest_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Wiki delete manifest must be valid JSON.") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("Wiki delete manifest fields do not match schema version 1.")
    if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported Wiki delete manifest schema version.")
    operations = manifest.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("Wiki delete manifest requires at least one operation.")
    if len(operations) > _MAX_BATCH_ITEMS:
        raise ValueError(f"Wiki delete operation count exceeds {_MAX_BATCH_ITEMS}.")
    if any(not isinstance(operation, dict) for operation in operations):
        raise ValueError("Wiki delete operations must be objects.")
    if any(set(operation) != _OPERATION_KEYS for operation in operations):
        raise ValueError("Wiki delete operation fields do not match schema version 1.")
    archive = manifest.get("archive")
    if not isinstance(archive, bool):
        raise ValueError("Wiki delete manifest archive must be a boolean.")
    return operations, archive


def _batch_backlink_index(
    page_keys: set[str],
    excluded_names: set[str],
) -> dict[str, list[str]]:
    """Return inbound Wiki links for the requested page keys in one Wiki pass."""
    excluded_identities = {name.casefold() for name in excluded_names}
    backlinks: dict[str, list[str]] = {page_key: [] for page_key in page_keys}
    for path in iter_markdown_files(get_wiki_dir()):
        if path.name.casefold() in excluded_identities:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            backlinks.setdefault("<unreadable>", []).append(path.name)
            continue
        for match in iter_wiki_link_matches(content):
            target = match.group(1).strip()
            target_key = target[:-3] if target.casefold().endswith(".md") else target
            if target_key in backlinks:
                backlinks[target_key].append(path.name)
    return {key: sorted(set(value)) for key, value in backlinks.items() if value}


def _build_plan(manifest_text: str) -> tuple[dict, list[dict], bool]:
    """Validate a batch and bind its preview to current canonical/projection state."""
    from vector_lake import governance_store
    from vector_lake.mutation_coordinator import resolve_wiki_mutation_path

    operations, archive = _validated_manifest(manifest_text)
    wiki_dir = get_wiki_dir()
    observed: list[dict] = []
    seen: set[str] = set()
    for operation in operations:
        filename = operation["filename"]
        if not isinstance(filename, str) or not filename:
            raise ValueError("Wiki delete filename must be a non-empty string.")
        security_name = unicodedata.normalize("NFKC", filename).casefold()
        if security_name.startswith("system_"):
            raise SystemPageDeleteNotAuthorized(
                "System wiki page deletion is disabled by default."
            )
        expected_version = _validated_version(operation["expected_version"])
        expected_projection_hash = _validated_sha256(
            operation["expected_projection_hash"],
            "expected_projection_hash",
        )
        validate_wiki_filename(filename)
        path = resolve_wiki_mutation_path(filename)
        identity = path.name.casefold()
        if identity in seen:
            raise ValueError(f"Wiki delete manifest repeats {filename}.")
        seen.add(identity)
        if not path.is_file():
            raise ValueError(f"Wiki delete target does not exist: {filename}")
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(current_hash, expected_projection_hash):
            raise ValueError(f"Projection changed for {filename}.")
        observed.append(
            {
                "filename": path.name,
                "page_key": _page_key(path.name),
                "expected_version": expected_version,
                "projection_sha256": current_hash,
            }
        )

    page_keys = {item["page_key"] for item in observed}
    current_versions = governance_store.canonical_page_versions(page_keys)
    for item in observed:
        current_version = str(current_versions.get(item["page_key"]) or "")
        if not hmac.compare_digest(current_version, item["expected_version"]):
            raise ValueError(f"Canonical version changed for {item['filename']}.")
        item["canonical_version"] = current_version

    backlinks = _batch_backlink_index(
        page_keys,
        {item["filename"] for item in observed},
    )
    if backlinks:
        detail = "; ".join(
            f"{key} <- {', '.join(names[:5])}" for key, names in sorted(backlinks.items())
        )
        raise ValueError(f"Wiki delete refused because pages are still linked: {detail}")

    plan_basis = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "contract": _CONTRACT,
        "archive": archive,
        "operations": [
            {
                "filename": item["filename"],
                "expected_version": item["expected_version"],
                "projection_sha256": item["projection_sha256"],
            }
            for item in observed
        ],
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            plan_basis,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    plan = {
        **plan_basis,
        "operation_count": len(observed),
        "wiki_dir": str(wiki_dir),
        "archive_dir": str(wiki_dir / _ARCHIVE_DIR_NAME),
        "confirmation_required": True,
        "fingerprint": fingerprint,
    }
    return plan, observed, archive


def _archive_page(filename: str) -> str:
    """Publish one archive copy before the canonical delete commits."""
    wiki_dir = get_wiki_dir()
    source = wiki_dir / filename
    archive_dir = wiki_dir / _ARCHIVE_DIR_NAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / filename
    shutil.copy2(source, target)
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != (
        hashlib.sha256(source.read_bytes()).hexdigest()
    ):
        raise RuntimeError(f"Wiki delete archive verification failed: {filename}")
    return str(target)


def delete_wiki_batch(
    payload_file: str,
    dry_run: bool = True,
    confirmation: str = "",
) -> dict:
    """Preview or atomically commit one exact Wiki page deletion batch."""
    from vector_lake.mutation_coordinator import execute_mutation_batch

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    if not isinstance(payload_file, str) or not payload_file:
        raise ValueError("payload_file must be a non-empty path")
    with open(payload_file, encoding="utf-8") as stream:
        manifest_text = stream.read()

    plan, observed, archive = _build_plan(manifest_text)
    if dry_run:
        return {
            "schema_version": 1,
            "ok": True,
            "dry_run": True,
            "committed": False,
            "operation_count": plan["operation_count"],
            "confirmation_required": True,
            "fingerprint": plan["fingerprint"],
            "plan": plan,
            "outbox_ids": [],
            "deferred": [],
            "archive_paths": {},
            "message": "Wiki delete preview validated; no changes committed.",
        }
    if not confirmation or not hmac.compare_digest(str(confirmation), plan["fingerprint"]):
        raise ValueError(
            "confirmation must exactly match the current Wiki delete fingerprint."
        )

    from vector_lake.tool_projection import create_maintenance_backup

    def verify_preconditions() -> None:
        current_plan, _current, _archive_flag = _build_plan(manifest_text)
        if not hmac.compare_digest(current_plan["fingerprint"], plan["fingerprint"]):
            raise RuntimeError(
                "Wiki delete state changed after confirmation; regenerate the preview."
            )

    archive_paths = {
        item["filename"]: _archive_page(item["filename"]) for item in observed
    } if archive else {}
    verify_preconditions()
    backup = create_maintenance_backup("wiki_page_delete")
    mutations = [
        {
            "filename": item["filename"],
            "is_delete": True,
            "expected_version": item["expected_version"],
            "expected_projection_hash": item["projection_sha256"],
        }
        for item in observed
    ]
    details = execute_mutation_batch(
        mutations,
        validation_mode="schema",
        origin="wiki_page_delete",
        precondition_callback=verify_preconditions,
        return_details=True,
    )
    if not isinstance(details, dict):
        raise RuntimeError("Mutation coordinator did not return detail fields.")
    committed = bool(details.get("committed"))
    outbox_ids = [int(value) for value in details.get("outbox_ids", [])]
    deferred = [str(value) for value in details.get("deferred", [])]
    warnings = [
        "post_commit_follow_up_warning"
        for _warning in list(details.get("post_commit_warnings") or [])
    ]
    return {
        "schema_version": 1,
        "ok": committed,
        "dry_run": False,
        "committed": committed,
        "operation_count": plan["operation_count"],
        "confirmation_required": False,
        "fingerprint": plan["fingerprint"],
        "backup": backup,
        "archive_paths": archive_paths,
        "outbox_ids": outbox_ids,
        "deferred": deferred,
        "post_commit_warnings": warnings,
        "error_code": None if committed else "mutation_not_committed",
        "message": (
            "Canonical Wiki deletions committed; "
            f"outbox={len(outbox_ids)}; deferred={len(deferred)}; "
            "Markdown projections are published by the mutation outbox."
            if committed
            else "Canonical Wiki deletions were not committed."
        ),
    }


__all__ = ["delete_wiki_batch", "SystemPageDeleteNotAuthorized"]
