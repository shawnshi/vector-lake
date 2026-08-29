"""Projection/canonical maintenance helpers.

These tools are intentionally conservative: report first, then bounded apply
with a live backup. They repair recoverable projections without deleting
canonical history.
"""

from __future__ import annotations
import hashlib
import os
import sqlite3

import json
import logging
import re
import shutil
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from vector_lake import db_store, governance_store, indexer
from vector_lake.backup_capacity import (
    assert_legacy_projection_file_size,
    assert_backup_capacity,
    estimate_maintenance_backup_bytes,
    projection_v2_reachable_inventory,
)
from vector_lake.claim_extractor import extract_page_objects
from vector_lake.cancellation import non_interruptible_phase
from vector_lake.durability import sync_directory
from vector_lake.projection_format_v2 import (
    SIDECAR_CONTRACT,
    is_v2_locator,
    validate_root_closure,
    validate_sidecar,
)
from vector_lake.projection_store_v2 import ProjectionStoreV2
from vector_lake.db_store import (
    backup_database,
    enqueue_mutation,
    get_connection,
    get_db_path,
    init_db,
    inspect_schema_migration_connection,
    inspect_schema_migration_state,
    peek_db_path,
    transaction,
)
from vector_lake.evidence_foundation import (
    build_extraction_run,
    evidence_independence,
    resolve_source_artifact,
    source_locator_for,
    version_family_id,
)
from vector_lake.index_snapshot import (
    load_index_snapshot,
    load_legacy_index_snapshot_for_migration,
)
from vector_lake.mutation_coordinator import materialize_markdown_projection
from vector_lake.schema_validator import VALID_H3_SLOTS, validate_schema
from vector_lake.yaml_utils import dump_yaml
from vector_lake.wiki_utils import (
    atomic_write_text,
    get_claim_graph_path,
    get_index_path,
    get_legacy_claim_graph_path,
    get_meta_dir,
    get_projection_manifest_path,
    get_wiki_dir,
    iter_markdown_files,
    normalize_semantic_text,
    peek_meta_dir,
    read_markdown_file,
    split_frontmatter,
)


EXCLUDED_WIKI_FILES = {
    "index.md",
    "log.md",
    "overview.md",
    "orphan_pages.md",
    "wiki_link_stats.md",
    "synthesis_log.md",
}
log = logging.getLogger("vector-lake-projection")

_MAINTENANCE_MANIFEST_V4_KEYS = frozenset(
    {
        "manifest_version",
        "created_at",
        "label",
        "copied",
        "artifact_sha256",
        "artifact_bytes",
        "database_runtime_generations",
        "database_runtime_generation_error",
        "projection_generation",
        "projection_canonical_generation",
        "projection_format",
        "projection_v2",
        "canonical_projection_consistency",
        "restorable_as_consistent_canonical_projection_snapshot",
        "complete",
    }
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROJECTION_GC_CONTRACT = "vector-lake-projection-object-gc/v1"
_PROJECTION_GC_MAX_BATCH = 10_000
_PROJECTION_GC_MAX_SCAN_FILES = 200_000
_TEST_PROJECTION_GC_FAULT_HOOK = None
_PROJECTION_REBUILD_LEGACY_CAPABILITY = object()


def _strip_markdown_suffix(value: str) -> str:
    text = str(value)
    return text[:-3] if text.casefold().endswith(".md") else text


def _wiki_path_map() -> dict[str, Path]:
    return {path.stem: path for path in iter_markdown_files(get_wiki_dir())}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _validate_backup_label(label: str) -> str:
    if (
        not isinstance(label, str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
            label,
        )
        is None
    ):
        raise ValueError("backup label must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    return label


def _stream_sha256_and_sync(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash one staged artifact with bounded memory, then flush it to storage."""
    digest = hashlib.sha256()
    with open(path, "r+b") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return digest.hexdigest()


def _write_manifest_and_sync(path: Path, manifest: dict) -> None:
    """Persist the completion marker only after all artifacts are durable."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def _is_reparse(path: Path, details: os.stat_result | None = None) -> bool:
    observed = details if details is not None else path.lstat()
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(observed.st_mode) or bool(
        int(getattr(observed, "st_file_attributes", 0) or 0) & marker
    )


def _sha256_plain_file(path: Path) -> tuple[str, int]:
    details = path.lstat()
    if _is_reparse(path, details) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"backup_artifact_not_plain_file:{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    after = path.lstat()
    if (
        _is_reparse(path, after)
        or not stat.S_ISREG(after.st_mode)
        or int(after.st_size) != int(details.st_size)
        or int(getattr(after, "st_mtime_ns", 0))
        != int(getattr(details, "st_mtime_ns", 0))
    ):
        raise ValueError(f"backup_artifact_changed:{path.name}")
    return digest.hexdigest(), int(after.st_size)


def _safe_backup_relative(value: object) -> str:
    name = str(value or "")
    if not name or "\\" in name:
        raise ValueError("backup_artifact_name_invalid")
    relative = Path(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("backup_artifact_name_invalid")
    normalized = relative.as_posix()
    if normalized != name:
        raise ValueError("backup_artifact_name_not_canonical")
    return normalized


def validate_maintenance_backup_v4(
    manifest_path: str | Path,
) -> tuple[dict, dict | None]:
    """Strictly validate a v4 maintenance bundle and its v2 object closure."""
    path = Path(manifest_path)
    directory = path.parent
    root_details = directory.lstat()
    if _is_reparse(directory, root_details) or not stat.S_ISDIR(root_details.st_mode):
        raise ValueError("maintenance_backup_directory_invalid")
    if path.name != "manifest.json":
        raise ValueError("maintenance_backup_manifest_name_invalid")
    digest, size = _sha256_plain_file(path)
    if size > 1024 * 1024:
        raise ValueError("maintenance_backup_manifest_too_large")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != set(
        _MAINTENANCE_MANIFEST_V4_KEYS
    ):
        raise ValueError("maintenance_backup_manifest_keys_invalid")
    if manifest.get("manifest_version") != 4 or manifest.get("complete") is not True:
        raise ValueError("maintenance_backup_manifest_contract_invalid")
    copied_raw = manifest.get("copied")
    hashes = manifest.get("artifact_sha256")
    sizes = manifest.get("artifact_bytes")
    if not isinstance(copied_raw, list) or not isinstance(hashes, dict) or not isinstance(
        sizes, dict
    ):
        raise ValueError("maintenance_backup_artifact_inventory_invalid")
    copied = [_safe_backup_relative(item) for item in copied_raw]
    if len(copied) != len(set(copied)) or copied != sorted(copied):
        raise ValueError("maintenance_backup_artifact_inventory_not_canonical")
    if set(hashes) != set(copied) or set(sizes) != set(copied):
        raise ValueError("maintenance_backup_artifact_inventory_mismatch")
    projection_format = manifest.get("projection_format")

    expected_directories = {directory}
    for name in copied:
        relative = Path(name)
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(directory / parent)
            parent = parent.parent
    actual_files: set[str] = set()
    actual_directories = {directory}
    scanned = 0
    for current_root, directory_names, file_names in os.walk(
        directory, followlinks=False
    ):
        current = Path(current_root)
        for name in directory_names:
            candidate = current / name
            details = candidate.lstat()
            if _is_reparse(candidate, details) or not stat.S_ISDIR(details.st_mode):
                raise ValueError("maintenance_backup_reparse_forbidden")
            actual_directories.add(candidate)
        for name in file_names:
            scanned += 1
            if scanned > _PROJECTION_GC_MAX_SCAN_FILES:
                raise ValueError("maintenance_backup_file_limit_exceeded")
            candidate = current / name
            relative = candidate.relative_to(directory).as_posix()
            if relative != "manifest.json":
                actual_files.add(relative)
    if actual_files != set(copied) or actual_directories != expected_directories:
        raise ValueError("maintenance_backup_unknown_or_missing_artifact")

    for name in copied:
        artifact = directory / Path(name)
        if projection_format == 1 and name in {
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        }:
            assert_legacy_projection_file_size(artifact)
        observed_hash, observed_size = _sha256_plain_file(artifact)
        expected_hash = hashes.get(name)
        expected_size = sizes.get(name)
        if (
            not isinstance(expected_hash, str)
            or _HEX_SHA256.fullmatch(expected_hash) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or observed_hash != expected_hash
            or observed_size != expected_size
        ):
            raise ValueError(f"maintenance_backup_artifact_mismatch:{name}")

    projection_metadata = manifest.get("projection_v2")
    inventory = None
    if projection_format == 2:
        if not isinstance(projection_metadata, dict):
            raise ValueError("maintenance_backup_projection_v2_missing")
        inventory = projection_v2_reachable_inventory(wiki_dir=directory)
        if inventory is None:
            raise ValueError("maintenance_backup_projection_v2_invalid")
        expected_objects = {
            (
                Path(".projection-store")
                / "objects"
                / "sha256"
                / Path(item["path"]).parent.name
                / Path(item["path"]).name
            ).as_posix()
            for item in inventory["objects"]
        }
        expected_artifacts = {
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
            *expected_objects,
        }
        if "vector_lake.db" in copied:
            expected_artifacts.add("vector_lake.db")
        recorded_objects = projection_metadata.get("object_artifacts")
        if (
            set(copied) != expected_artifacts
            or
            not isinstance(recorded_objects, list)
            or set(recorded_objects) != expected_objects
            or len(recorded_objects) != len(expected_objects)
            or projection_metadata.get("format_version") != 2
            or projection_metadata.get("roots") != inventory["roots"]
            or projection_metadata.get("sidecar_sha256")
            != inventory["sidecar_sha256"]
            or projection_metadata.get("object_count") != inventory["object_count"]
            or projection_metadata.get("reachable_object_bytes")
            != inventory["reachable_object_bytes"]
            or manifest.get("projection_generation")
            != inventory["projection_generation"]
        ):
            raise ValueError("maintenance_backup_projection_v2_binding_mismatch")
    elif projection_format == 1 and projection_metadata is None:
        expected = {
            "index.json",
            "claim_graph.json",
            "projection_pair_manifest.json",
        }
        if "vector_lake.db" in copied:
            expected.add("vector_lake.db")
        if set(copied) != expected:
            raise ValueError("maintenance_backup_unknown_or_missing_artifact")
        index_path = directory / "index.json"
        claim_graph_path = directory / "claim_graph.json"
        sidecar_path = directory / "projection_pair_manifest.json"
        with index_path.open("r", encoding="utf-8") as handle:
            index_data = json.load(handle)
        with claim_graph_path.open("r", encoding="utf-8") as handle:
            claim_graph_data = json.load(handle)
        generation = indexer.validate_projection_pair(
            index_data,
            claim_graph_data,
        )
        canonical_generation = indexer.projection_canonical_generation(
            index_data,
            claim_graph_data,
        )
        sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar_manifest, sidecar_artifacts = indexer._validate_projection_sidecar(
            sidecar_data
        )
        if (
            sidecar_manifest != index_data.get(indexer.PROJECTION_MANIFEST_KEY)
            or generation != manifest.get("projection_generation")
            or canonical_generation
            != manifest.get("projection_canonical_generation")
        ):
            raise ValueError("maintenance_backup_projection_v1_binding_mismatch")
        for artifact_path in (index_path, claim_graph_path):
            metadata = sidecar_artifacts.get(artifact_path.name)
            if (
                not isinstance(metadata, dict)
                or metadata.get("sha256") != hashes.get(artifact_path.name)
                or metadata.get("bytes") != sizes.get(artifact_path.name)
            ):
                raise ValueError(
                    "maintenance_backup_projection_v1_sidecar_mismatch:"
                    f"{artifact_path.name}"
                )
    elif projection_format is None and projection_metadata is None:
        if set(copied).difference({"vector_lake.db"}):
            raise ValueError("maintenance_backup_unknown_or_missing_artifact")
    else:
        raise ValueError("maintenance_backup_projection_format_invalid")
    return {
        **manifest,
        "_manifest_sha256": digest,
        "_manifest_bytes": size,
    }, inventory


def _projection_gc_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _projection_gc_object_relative(path: Path) -> str:
    return (
        Path(".projection-store")
        / "objects"
        / "sha256"
        / path.parent.name
        / path.name
    ).as_posix()


def _projection_gc_protection() -> tuple[set[str], dict, list[str]]:
    protected: set[str] = set()
    issues: list[str] = []
    binding: dict[str, object] = {
        "live": None,
        "runtime_sidecars": [],
        "maintenance_backups": [],
        "pending_receipts": [],
    }
    try:
        live = projection_v2_reachable_inventory()
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return protected, binding, [f"live_projection_invalid:{type(exc).__name__}"]
    if live is None:
        return protected, binding, []
    protected.update(str(item["sha256"]) for item in live["objects"])
    binding["live"] = {
        "projection_generation": live["projection_generation"],
        "sidecar_sha256": live["sidecar_sha256"],
        "roots": live["roots"],
    }
    store_base = get_wiki_dir()

    def protect_sidecar(value: object, label: str) -> None:
        try:
            sidecar = validate_sidecar(value)
            closure = validate_root_closure(store_base, sidecar)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            issues.append(f"protected_sidecar_invalid:{label}:{type(exc).__name__}")
            return
        protected.update(path.stem for path in closure)
        binding["runtime_sidecars"].append(
            {
                "label": label,
                "projection_generation": sidecar["projection_generation"],
                "roots": {
                    "index": sidecar["index_root_sha256"],
                    "claim_graph": sidecar["claim_graph_root_sha256"],
                },
            }
        )

    database_path = peek_db_path()
    if database_path.is_file():
        connection = None
        try:
            connection = sqlite3.connect(
                f"{database_path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(projection_runtime_v9)")
            }
            if not {"status", "sidecar_json", "previous_sidecar_json"}.issubset(
                columns
            ):
                issues.append("projection_runtime_v9_unavailable")
            else:
                row = connection.execute(
                    "SELECT status, sidecar_json, previous_sidecar_json "
                    "FROM projection_runtime_v9 WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    issues.append("projection_runtime_v9_missing")
                else:
                    status = str(row[0] or "")
                    if row[1] is not None:
                        protect_sidecar(row[1], f"runtime:{status}:current")
                    if row[2] is not None:
                        protect_sidecar(row[2], f"runtime:{status}:previous")
        except (OSError, sqlite3.Error) as exc:
            issues.append(f"projection_runtime_v9_unreadable:{type(exc).__name__}")
        finally:
            if connection is not None:
                connection.close()

    backup_root = peek_meta_dir() / "backups"
    try:
        backup_directories = (
            sorted(path for path in backup_root.iterdir() if path.is_dir())
            if backup_root.exists()
            else []
        )
    except OSError as exc:
        backup_directories = []
        issues.append(f"maintenance_backup_inventory_failed:{type(exc).__name__}")
    if len(backup_directories) > 10_000:
        issues.append("maintenance_backup_directory_limit_exceeded")
        backup_directories = backup_directories[:10_000]
    for directory in backup_directories:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            preview = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            issues.append(f"maintenance_backup_manifest_invalid:{directory.name}")
            continue
        if not isinstance(preview, dict) or preview.get("manifest_version") != 4:
            continue
        try:
            manifest, backup_inventory = validate_maintenance_backup_v4(manifest_path)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            issues.append(
                f"maintenance_backup_v4_invalid:{directory.name}:{type(exc).__name__}"
            )
            continue
        if backup_inventory is not None:
            protected.update(
                str(item["sha256"]) for item in backup_inventory["objects"]
            )
        binding["maintenance_backups"].append(
            {
                "path": str(manifest_path),
                "sha256": manifest["_manifest_sha256"],
                "projection_generation": manifest.get("projection_generation"),
            }
        )

    receipt_roots = (
        peek_meta_dir() / "restore-snapshot-receipts",
        database_path.parent / "schema-migration-receipts",
    )
    for receipt_root in receipt_roots:
        if not receipt_root.exists():
            continue
        try:
            paths = sorted(receipt_root.glob("*.pending.json"))
        except OSError as exc:
            issues.append(f"pending_receipt_inventory_failed:{type(exc).__name__}")
            continue
        if len(paths) > 10_000:
            issues.append("pending_receipt_limit_exceeded")
            paths = paths[:10_000]
        for path in paths:
            try:
                digest, size = _sha256_plain_file(path)
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                issues.append(f"pending_receipt_invalid:{path.name}:{type(exc).__name__}")
                continue
            if not isinstance(payload, dict):
                issues.append(f"pending_receipt_invalid:{path.name}")
                continue

            def visit(value: object) -> None:
                if isinstance(value, dict):
                    if value.get("contract") == SIDECAR_CONTRACT:
                        protect_sidecar(value, f"pending:{path.name}")
                    for nested in value.values():
                        visit(nested)
                elif isinstance(value, list):
                    for nested in value:
                        visit(nested)
                elif isinstance(value, str):
                    candidate = value.removeprefix("sha256:")
                    if _HEX_SHA256.fullmatch(candidate):
                        object_path = ProjectionStoreV2(store_base).object_path(candidate)
                        if object_path.exists():
                            protected.add(candidate)

            visit(payload)
            binding["pending_receipts"].append(
                {"path": str(path), "sha256": digest, "bytes": size}
            )
    for key in binding:
        if isinstance(binding[key], list):
            binding[key] = sorted(
                binding[key],
                key=lambda item: json.dumps(item, sort_keys=True),
            )
    return protected, binding, list(dict.fromkeys(issues))


def _projection_gc_plan(*, retention_days: int, limit: int) -> dict:
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or not 0 <= retention_days <= 3650
    ):
        raise ValueError("retention_days must be between 0 and 3650")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _PROJECTION_GC_MAX_BATCH:
        raise ValueError(f"limit must be between 1 and {_PROJECTION_GC_MAX_BATCH}")
    protected, protection_binding, issues = _projection_gc_protection()
    if protection_binding.get("live") is None:
        issues.append("live_projection_v2_absent")
    cutoff_epoch = int(time.time() // 3600 * 3600) - retention_days * 86_400
    store = ProjectionStoreV2(get_wiki_dir())
    root = store.objects_dir
    candidates: list[dict] = []
    orphan_objects = 0
    orphan_bytes = 0
    scanned = 0
    if root.exists():
        try:
            root_details = root.lstat()
            if _is_reparse(root, root_details) or not stat.S_ISDIR(root_details.st_mode):
                issues.append("projection_object_root_invalid")
            for current_root, directory_names, file_names in os.walk(
                root, followlinks=False
            ):
                current = Path(current_root)
                for name in directory_names:
                    directory = current / name
                    details = directory.lstat()
                    if _is_reparse(directory, details):
                        issues.append("projection_object_reparse_forbidden")
                    if current == root and re.fullmatch(r"[0-9a-f]{2}", name) is None:
                        issues.append(f"projection_object_unknown_directory:{name}")
                    elif current != root:
                        issues.append(f"projection_object_unknown_directory:{name}")
                for name in file_names:
                    scanned += 1
                    if scanned > _PROJECTION_GC_MAX_SCAN_FILES:
                        issues.append("projection_object_scan_limit_exceeded")
                        break
                    path = current / name
                    details = path.lstat()
                    digest = path.stem
                    if (
                        _is_reparse(path, details)
                        or not stat.S_ISREG(details.st_mode)
                        or path.suffix != ".json"
                        or _HEX_SHA256.fullmatch(digest) is None
                        or path.parent.name != digest[:2]
                    ):
                        issues.append(f"projection_object_unknown_file:{name}")
                        continue
                    if digest in protected:
                        continue
                    orphan_objects += 1
                    orphan_bytes += int(details.st_size)
                    if float(details.st_mtime) > cutoff_epoch:
                        continue
                    observed_hash, observed_size = _sha256_plain_file(path)
                    if observed_hash != digest:
                        issues.append(f"projection_object_digest_mismatch:{name}")
                        continue
                    candidates.append(
                        {
                            "relative_path": _projection_gc_object_relative(path),
                            "sha256": digest,
                            "bytes": observed_size,
                            "mtime_ns": int(details.st_mtime_ns),
                        }
                    )
                if scanned > _PROJECTION_GC_MAX_SCAN_FILES:
                    break
        except OSError as exc:
            issues.append(f"projection_object_inventory_failed:{type(exc).__name__}")
    candidates.sort(key=lambda item: (item["mtime_ns"], item["sha256"]))
    selected = candidates[:limit]
    core = {
        "contract": _PROJECTION_GC_CONTRACT,
        "retention_days": retention_days,
        "cutoff_epoch": cutoff_epoch,
        "limit": limit,
        "protection_binding": protection_binding,
        "protected_objects": len(protected),
        "orphan_objects": orphan_objects,
        "orphan_bytes": orphan_bytes,
        "eligible_objects": len(candidates),
        "eligible_bytes": sum(int(item["bytes"]) for item in candidates),
        "candidates": selected,
        "candidate_bytes": sum(int(item["bytes"]) for item in selected),
        "truncated": len(candidates) > len(selected),
        "issues": list(dict.fromkeys(issues)),
        "can_apply": not issues,
    }
    return {**core, "fingerprint": _projection_gc_fingerprint(core)}


def _projection_gc_receipt_valid(payload: dict, *, status: str) -> bool:
    if payload.get("contract") != _PROJECTION_GC_CONTRACT or payload.get("status") != status:
        return False
    fingerprint = payload.get("receipt_fingerprint")
    unsigned = dict(payload)
    unsigned.pop("receipt_fingerprint", None)
    return isinstance(fingerprint, str) and fingerprint == _projection_gc_fingerprint(
        unsigned
    )


def projection_object_gc(
    *,
    dry_run: bool = True,
    retention_days: int = 7,
    limit: int = 1000,
    confirmation: str = "",
) -> dict:
    """Preview or apply bounded, receipt-bound projection object collection."""
    plan = _projection_gc_plan(retention_days=retention_days, limit=limit)
    if dry_run:
        return {**plan, "dry_run": True}
    if not isinstance(confirmation, str) or not confirmation.startswith("sha256:"):
        raise RuntimeError("projection object GC requires preview fingerprint")
    receipt_root = get_meta_dir() / "projection-object-gc-receipts"
    operation_id = confirmation.removeprefix("sha256:")
    completed_path = receipt_root / f"{operation_id}.completed.json"
    pending_path = receipt_root / f"{operation_id}.pending.json"
    if completed_path.is_file():
        completed = json.loads(completed_path.read_text(encoding="utf-8"))
        if not _projection_gc_receipt_valid(completed, status="completed"):
            raise RuntimeError("projection object GC completed receipt is invalid")
        return {
            **completed,
            "dry_run": False,
            "applied": False,
            "no_op": True,
            "resumed": False,
        }

    gc_lock = FileLock(str(receipt_root.parent / ".projection-object-gc.lock"), timeout=0)
    projection_lock = FileLock(str(get_index_path()) + ".lock", timeout=0)
    try:
        gc_lock.acquire()
        projection_lock.acquire()
    except Timeout as exc:
        if gc_lock.is_locked:
            gc_lock.release()
        raise RuntimeError("projection object GC lock is busy") from exc
    try:
        if pending_path.is_file():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            if not _projection_gc_receipt_valid(pending, status="pending"):
                raise RuntimeError("projection object GC pending receipt is invalid")
            if pending.get("plan_fingerprint") != confirmation:
                raise RuntimeError("projection object GC pending fingerprint mismatch")
            _protected, current_binding, protection_issues = _projection_gc_protection()
            if protection_issues or current_binding != pending.get("protection_binding"):
                raise RuntimeError("projection object GC protection binding changed")
            candidates = list(pending.get("candidates") or [])
            resumed = True
        else:
            locked_plan = _projection_gc_plan(
                retention_days=retention_days,
                limit=limit,
            )
            if locked_plan["fingerprint"] != confirmation:
                raise RuntimeError("projection object GC fingerprint changed")
            if not locked_plan["can_apply"]:
                raise RuntimeError(
                    "projection object GC cannot apply: "
                    + ", ".join(locked_plan["issues"])
                )
            candidates = list(locked_plan["candidates"])
            pending = {
                "contract": _PROJECTION_GC_CONTRACT,
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "plan_fingerprint": confirmation,
                "retention_days": retention_days,
                "limit": limit,
                "protection_binding": locked_plan["protection_binding"],
                "candidates": candidates,
            }
            pending["receipt_fingerprint"] = _projection_gc_fingerprint(pending)
            receipt_root.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                pending_path,
                json.dumps(pending, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            resumed = False

        protected, current_binding, protection_issues = _projection_gc_protection()
        if protection_issues or current_binding != pending["protection_binding"]:
            raise RuntimeError("projection object GC protection binding changed")
        store = ProjectionStoreV2(get_wiki_dir())
        deleted = 0
        already_missing = 0
        touched_directories: set[Path] = set()
        with non_interruptible_phase("projection-object-gc-delete"):
            for candidate in candidates:
                digest = str(candidate.get("sha256") or "")
                if digest in protected or _HEX_SHA256.fullmatch(digest) is None:
                    raise RuntimeError("projection object GC candidate became protected")
                path = store.object_path(digest)
                if not path.exists():
                    already_missing += 1
                    continue
                observed_hash, observed_size = _sha256_plain_file(path)
                details = path.lstat()
                if (
                    observed_hash != digest
                    or observed_size != int(candidate.get("bytes") or -1)
                    or int(details.st_mtime_ns) != int(candidate.get("mtime_ns") or -1)
                ):
                    raise RuntimeError("projection object GC candidate changed")
                path.unlink()
                touched_directories.add(path.parent)
                deleted += 1
                if _TEST_PROJECTION_GC_FAULT_HOOK is not None:
                    _TEST_PROJECTION_GC_FAULT_HOOK(deleted, digest)
            for directory in sorted(touched_directories, key=str):
                sync_directory(directory)
        completed = {
            **{key: value for key, value in pending.items() if key != "receipt_fingerprint"},
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "deleted_objects": deleted,
            "already_missing_objects": already_missing,
        }
        completed["receipt_fingerprint"] = _projection_gc_fingerprint(completed)
        atomic_write_text(
            completed_path,
            json.dumps(completed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        pending_path.unlink(missing_ok=True)
        sync_directory(receipt_root)
        return {
            **completed,
            "dry_run": False,
            "applied": deleted > 0,
            "no_op": deleted == 0,
            "resumed": resumed,
        }
    finally:
        if projection_lock.is_locked:
            projection_lock.release()
        if gc_lock.is_locked:
            gc_lock.release()


def _read_backup_runtime_generations(
    database_path: Path,
) -> tuple[dict[str, int] | None, str | None]:
    """Read the generation ledger from the copied database, never the live handle."""
    connection = None
    try:
        connection = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
        connection.execute("PRAGMA query_only = ON")
        schema_state = inspect_schema_migration_connection(
            connection,
            database_path,
        )
        if not schema_state["ready"]:
            detail = "|".join(schema_state["issues"]) or str(schema_state["status"])
            return None, f"backup-schema-invalid:{detail}"
        rows = connection.execute(
            "SELECT surface, generation FROM runtime_generations ORDER BY surface"
        ).fetchall()
        return {str(surface): int(generation) for surface, generation in rows}, None
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return None, f"backup-runtime-generation-read-failed:{exc}"
    finally:
        if connection is not None:
            connection.close()


def _canonical_projection_consistency(
    database_generations: dict[str, int] | None,
    database_error: str | None,
    projection_binding: dict | None,
) -> dict:
    covered = list(indexer.CANONICAL_PROJECTION_SURFACES)
    scope = {
        "verification_scope": "tracked-canonical-projection-surfaces",
        "covered_surfaces": covered,
    }
    if projection_binding is None:
        return {
            "status": "not_applicable",
            "reason": "projection-pair-absent",
            **scope,
        }
    if database_generations is None:
        return {
            "status": "unverifiable",
            "reason": database_error or "backup-runtime-generations-unavailable",
            **scope,
        }
    if projection_binding.get("status") != "verified":
        return {
            "status": "unverifiable",
            "reason": projection_binding.get("reason")
            or "projection-canonical-generation-unverifiable",
            **scope,
        }
    expected = projection_binding["runtime_generations"]
    observed = {
        surface: database_generations.get(surface)
        for surface in indexer.CANONICAL_PROJECTION_SURFACES
    }
    if any(value is None for value in observed.values()):
        return {
            "status": "unverifiable",
            "reason": "backup-runtime-generation-coverage-incomplete",
            **scope,
            "projection_runtime_generations": expected,
            "database_runtime_generations": observed,
        }
    if observed != expected:
        return {
            "status": "unverifiable",
            "reason": "backup-and-projection-runtime-generations-do-not-match",
            **scope,
            "projection_runtime_generations": expected,
            "database_runtime_generations": observed,
        }
    return {
        "status": "verified",
        "reason": "runtime-generations-match",
        **scope,
        "canonical_generation_token": projection_binding["token"],
        "projection_runtime_generations": expected,
        "database_runtime_generations": observed,
    }


def _wiki_keys() -> set[str]:
    return {
        page_key
        for page_key, path in _wiki_path_map().items()
        if path.name.casefold() not in EXCLUDED_WIKI_FILES
        and not path.name.casefold().startswith("system_")
    }


def _canonical_keys(*, allow_initialize: bool = False) -> set[str]:
    path = peek_db_path()
    if not path.exists():
        if not allow_initialize:
            return set()
        init_db()
        conn = get_connection()
        close_after = False
    else:
        schema_state = inspect_schema_migration_state(path)
        if not schema_state["ready"]:
            raise RuntimeError(
                "database schema is not ready: " + "; ".join(schema_state["issues"])
            )
        if allow_initialize:
            init_db()
            conn = get_connection()
            close_after = False
        else:
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            close_after = True
    try:
        return {
            row["page_key"]
            for row in conn.execute(
                "SELECT json_extract(data_json, '$.page_key') AS page_key "
                "FROM entities WHERE json_extract(data_json, '$.page_key') "
                "IS NOT NULL"
            )
            if row["page_key"] and not str(row["page_key"]).startswith("System_")
        }
    finally:
        if close_after:
            conn.close()


def _legacy_projection_file_identity(path: Path) -> tuple[int, ...]:
    details = path.lstat()
    if _is_reparse(path, details) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("legacy_projection_index_not_plain_file")
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
    )


def _load_legacy_index_for_projection_rebuild(
    index_path: Path,
    *,
    capability: object,
) -> dict:
    """Read v1 only for the schema-v9 rebuild-required migration boundary."""
    if capability is not _PROJECTION_REBUILD_LEGACY_CAPABILITY:
        raise RuntimeError("legacy_projection_rebuild_capability_required")
    if is_v2_locator(index_path, "index"):
        raise RuntimeError("legacy_projection_rebuild_expected_v1_index")

    database_path = peek_db_path().resolve()
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0] or 0
        )
        if schema_version != 9:
            raise RuntimeError("legacy_projection_rebuild_requires_schema_v9")
        runtime = db_store.get_projection_runtime_v9(connection)
        if (
            runtime.get("format_version") != 2
            or runtime.get("status") != "rebuild_required"
            or runtime.get("projection_generation") is not None
            or runtime.get("canonical_generation") is not None
            or runtime.get("sidecar_sha256") is not None
            or runtime.get("sidecar") is not None
            or runtime.get("previous_sidecar") is not None
        ):
            raise RuntimeError(
                "legacy_projection_rebuild_requires_pointer_free_rebuild_state"
            )

        canonical_before = indexer.canonical_runtime_generation_snapshot(connection)
        claim_graph_path = get_claim_graph_path()
        if not claim_graph_path.is_file():
            raise RuntimeError("legacy_projection_claim_graph_missing")
        sidecar_path = get_projection_manifest_path()
        paths = (index_path, claim_graph_path)
        identities_before = {
            path: _legacy_projection_file_identity(path) for path in paths
        }
        data = load_legacy_index_snapshot_for_migration(index_path)
        with claim_graph_path.open("r", encoding="utf-8") as handle:
            claim_graph = json.load(handle)
        indexer.validate_projection_pair(data, claim_graph)
        manifest = data[indexer.PROJECTION_MANIFEST_KEY]
        binding = indexer.projection_canonical_generation(data, claim_graph)
        if (
            binding.get("status") != "verified"
            or binding.get("runtime_generations") != canonical_before
        ):
            raise RuntimeError("legacy_projection_canonical_generation_mismatch")

        if sidecar_path.exists():
            sidecar_identity = _legacy_projection_file_identity(sidecar_path)
            with sidecar_path.open("r", encoding="utf-8") as handle:
                sidecar = json.load(handle)
            sidecar_manifest, artifacts = indexer._validate_projection_sidecar(
                sidecar
            )
            if sidecar_manifest != manifest:
                raise RuntimeError("legacy_projection_sidecar_manifest_mismatch")
            for path in paths:
                digest, size = _sha256_plain_file(path)
                metadata = artifacts.get(path.name)
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("sha256") != digest
                    or metadata.get("bytes") != size
                ):
                    raise RuntimeError(
                        f"legacy_projection_sidecar_artifact_mismatch:{path.name}"
                    )
            if _legacy_projection_file_identity(sidecar_path) != sidecar_identity:
                raise RuntimeError("legacy_projection_sidecar_changed_during_read")

        identities_after = {
            path: _legacy_projection_file_identity(path) for path in paths
        }
        if identities_after != identities_before:
            raise RuntimeError("legacy_projection_pair_changed_during_read")

        canonical_after = indexer.canonical_runtime_generation_snapshot(connection)
        runtime_after = db_store.get_projection_runtime_v9(connection)
        if canonical_after != canonical_before:
            raise RuntimeError("legacy_projection_canonical_generation_changed")
        if runtime_after != runtime:
            raise RuntimeError("legacy_projection_runtime_state_changed")
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            raise RuntimeError("legacy_projection_nodes_invalid")
        return data
    finally:
        connection.close()


def _index_keys(*, legacy_rebuild_capability: object | None = None) -> set[str]:
    index_path = get_index_path()
    if not index_path.exists():
        return set()
    if legacy_rebuild_capability is None:
        data = load_index_snapshot(index_path)
    else:
        data = _load_legacy_index_for_projection_rebuild(
            index_path,
            capability=legacy_rebuild_capability,
        )
    return {key for key in data.get("nodes", {}) if not str(key).startswith("System_")}


def _diff_sets(
    *,
    allow_initialize: bool = False,
    legacy_rebuild_capability: object | None = None,
) -> dict[str, set[str]]:
    wiki = _wiki_keys()
    canonical = _canonical_keys(allow_initialize=allow_initialize)
    index = _index_keys(legacy_rebuild_capability=legacy_rebuild_capability)
    return {
        "wiki": wiki,
        "canonical": canonical,
        "index": index,
        "missing_index": canonical - index,
        "extra_index": index - canonical,
        "missing_canonical": wiki - canonical,
        "extra_canonical": canonical - wiki,
    }


def _copy_projection_pair_to_backup(
    backup_dir: Path,
) -> tuple[list[str], str | None, dict | None, dict | None]:
    """Validate and copy one projection generation under the publish lock."""
    index_path = get_index_path()
    claim_graph_path = get_claim_graph_path()
    sidecar_path = get_projection_manifest_path()
    legacy_claim_graph_path = get_legacy_claim_graph_path()
    copied: list[str] = []
    try:
        with FileLock(str(index_path) + ".lock", timeout=15):
            v2_inventory = projection_v2_reachable_inventory()
            if v2_inventory is not None:
                static_sources = (index_path, claim_graph_path, sidecar_path)
                for source in static_sources:
                    target = backup_dir / source.name
                    shutil.copyfile(source, target)
                    copied.append(target.name)
                object_artifacts: list[str] = []
                for item in v2_inventory["objects"]:
                    source = Path(item["path"])
                    relative = Path(".projection-store") / "objects" / "sha256" / source.parent.name / source.name
                    target = backup_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    copied.append(relative.as_posix())
                    object_artifacts.append(relative.as_posix())
                refreshed = projection_v2_reachable_inventory()
                if (
                    refreshed is None
                    or refreshed["projection_generation"]
                    != v2_inventory["projection_generation"]
                    or refreshed["sidecar_sha256"] != v2_inventory["sidecar_sha256"]
                    or [item["sha256"] for item in refreshed["objects"]]
                    != [item["sha256"] for item in v2_inventory["objects"]]
                ):
                    raise indexer.ProjectionPairContractError(
                        "Projection v2 commit marker changed during backup."
                    )
                generations = dict(v2_inventory["canonical_generation"])
                binding = {
                    "status": "verified",
                    "token": indexer._canonical_generation_token(generations),
                    "runtime_generations": generations,
                }
                metadata = {
                    "format_version": 2,
                    "roots": dict(v2_inventory["roots"]),
                    "sidecar_sha256": v2_inventory["sidecar_sha256"],
                    "object_count": int(v2_inventory["object_count"]),
                    "reachable_object_bytes": int(
                        v2_inventory["reachable_object_bytes"]
                    ),
                    "object_artifacts": object_artifacts,
                }
                return (
                    copied,
                    str(v2_inventory["projection_generation"]),
                    binding,
                    metadata,
                )
            index_exists = index_path.exists()
            claim_graph_exists = claim_graph_path.exists()
            if not index_exists and not claim_graph_exists:
                if legacy_claim_graph_path.exists():
                    raise indexer.ProjectionPairContractError(
                        "Legacy claim_topology.json cannot be backed up without a "
                        "generation-bound index/claim-graph pair; run sync first."
                    )
                return copied, None, None, None
            if index_exists != claim_graph_exists:
                raise indexer.ProjectionPairContractError(
                    "Index/claim-graph projection pair is incomplete; run sync before backup."
                )

            assert_legacy_projection_file_size(index_path)
            assert_legacy_projection_file_size(claim_graph_path)
            if sidecar_path.exists():
                assert_legacy_projection_file_size(sidecar_path)

            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
            with open(claim_graph_path, "r", encoding="utf-8") as handle:
                claim_graph_data = json.load(handle)
            generation = indexer.validate_projection_pair(index_data, claim_graph_data)
            manifest = index_data[indexer.PROJECTION_MANIFEST_KEY]
            if "canonical_generation" not in manifest:
                raise indexer.ProjectionPairContractError(
                    "Legacy projection manifest has no canonical_generation; "
                    "run a full rebuild before backup."
                )
            canonical_generation = indexer.projection_canonical_generation(
                index_data,
                claim_graph_data,
            )
            generated_backup_sidecar = not sidecar_path.exists()
            if generated_backup_sidecar:
                if canonical_generation.get("status") != "verified":
                    raise indexer.ProjectionPairContractError(
                        "Projection sidecar is missing and the canonical binding "
                        "is unverifiable; run sync before backup."
                    )
                current_generation = (
                    indexer.canonical_runtime_generation_snapshot()
                )
                if (
                    canonical_generation.get("runtime_generations")
                    != current_generation
                ):
                    raise indexer.ProjectionPairContractError(
                        "Projection sidecar is missing and the projection binding "
                        "is stale; run sync before backup."
                    )
                sidecar_data = indexer._projection_sidecar_payload(
                    manifest,
                    index_stage=str(index_path),
                    claim_graph_stage=str(claim_graph_path),
                    index_data=index_data,
                    claim_graph_data=claim_graph_data,
                )
                del index_data, claim_graph_data
            else:
                # The source projections dominate peak RSS.  Their validated
                # manifest/binding values are self-contained, so release both
                # payloads before loading the (small) sidecar document.
                del index_data, claim_graph_data
                with open(sidecar_path, "r", encoding="utf-8") as handle:
                    sidecar_data = json.load(handle)
            sidecar_manifest, sidecar_artifacts = (
                indexer._validate_projection_sidecar(sidecar_data)
            )
            if sidecar_manifest != manifest:
                raise indexer.ProjectionPairContractError(
                    "Projection sidecar manifest does not match the projection pair."
                )
            for path in (index_path, claim_graph_path):
                digest, _identity = indexer._stable_file_sha256(str(path))
                metadata = sidecar_artifacts[path.name]
                if (
                    digest != metadata["sha256"]
                    or path.stat().st_size != metadata["bytes"]
                ):
                    raise indexer.ProjectionPairContractError(
                        f"Projection sidecar digest does not match {path.name}."
                    )
            for path in (index_path, claim_graph_path):
                target = backup_dir / path.name
                shutil.copy2(path, target)
                copied.append(target.name)
            backup_sidecar_path = backup_dir / sidecar_path.name
            if generated_backup_sidecar:
                backup_sidecar_path.write_text(
                    json.dumps(sidecar_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                shutil.copy2(sidecar_path, backup_sidecar_path)
            copied.append(backup_sidecar_path.name)
            del sidecar_data

            with open(backup_dir / index_path.name, "r", encoding="utf-8") as handle:
                assert_legacy_projection_file_size(backup_dir / index_path.name)
                copied_index = json.load(handle)
            with open(
                backup_dir / claim_graph_path.name,
                "r",
                encoding="utf-8",
            ) as handle:
                assert_legacy_projection_file_size(
                    backup_dir / claim_graph_path.name
                )
                copied_claim_graph = json.load(handle)
            copied_generation = indexer.validate_projection_pair(
                copied_index,
                copied_claim_graph,
            )
            copied_canonical_generation = indexer.projection_canonical_generation(
                copied_index,
                copied_claim_graph,
            )
            del copied_index, copied_claim_graph
            copied_sidecar = json.loads(
                (backup_dir / sidecar_path.name).read_text(encoding="utf-8")
            )
            copied_manifest, copied_artifacts = indexer._validate_projection_sidecar(
                copied_sidecar
            )
            if (
                copied_generation != generation
                or copied_canonical_generation != canonical_generation
                or copied_manifest != manifest
            ):
                raise indexer.ProjectionPairContractError(
                    "Copied projection generation changed during backup."
                )
            for path in (
                backup_dir / index_path.name,
                backup_dir / claim_graph_path.name,
            ):
                digest, _identity = indexer._stable_file_sha256(str(path))
                metadata = copied_artifacts[path.name]
                if (
                    digest != metadata["sha256"]
                    or path.stat().st_size != metadata["bytes"]
                ):
                    raise indexer.ProjectionPairContractError(
                        f"Copied projection digest changed for {path.name}."
                    )
            return copied, generation, copied_canonical_generation, None
    except Timeout as exc:
        raise TimeoutError(
            f"Timeout while acquiring projection publish lock for {index_path}"
        ) from exc


def create_maintenance_backup(label: str = "maintenance") -> str:
    """Publish a complete SQLite/projection backup from a private staging directory."""
    label = _validate_backup_label(label)
    backup_root = get_meta_dir() / "backups"
    assert_backup_capacity(
        estimated_new_bytes=estimate_maintenance_backup_bytes(),
        operation=f"maintenance-backup:{label}",
        disk_anchor=backup_root,
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_name = f"{label}_{_utc_stamp()}"
    backup_dir = backup_root / backup_name
    stage_dir = backup_root / f".{backup_name}.{uuid.uuid4().hex}.tmp"
    stage_dir.mkdir(parents=False, exist_ok=False)
    copied: list[str] = []
    database_generations = None
    database_generation_error = "backup-database-absent"
    try:
        if get_db_path().exists():
            target = stage_dir / "vector_lake.db"
            backup_database(target)
            copied.append(target.name)
            database_generations, database_generation_error = (
                _read_backup_runtime_generations(target)
            )
        (
            projection_files,
            projection_generation,
            projection_canonical_generation,
            projection_v2,
        ) = _copy_projection_pair_to_backup(stage_dir)
        copied.extend(projection_files)
        copied = sorted(dict.fromkeys(copied))
        consistency = _canonical_projection_consistency(
            database_generations,
            database_generation_error,
            projection_canonical_generation,
        )
        artifact_sha256 = {
            name: _stream_sha256_and_sync(stage_dir / name) for name in sorted(copied)
        }
        artifact_bytes = {
            name: int((stage_dir / name).stat().st_size) for name in sorted(copied)
        }
        manifest = {
            "manifest_version": 4,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "copied": copied,
            "artifact_sha256": artifact_sha256,
            "artifact_bytes": artifact_bytes,
            "database_runtime_generations": database_generations,
            "database_runtime_generation_error": database_generation_error,
            "projection_generation": projection_generation,
            "projection_canonical_generation": projection_canonical_generation,
            "projection_format": (
                2 if projection_v2 is not None else (1 if projection_generation else None)
            ),
            "projection_v2": projection_v2,
            "canonical_projection_consistency": consistency,
            "restorable_as_consistent_canonical_projection_snapshot": (
                consistency["status"] == "verified"
            ),
            "complete": True,
        }
        for directory in sorted(
            (path for path in stage_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            sync_directory(directory)
        _write_manifest_and_sync(stage_dir / "manifest.json", manifest)
        sync_directory(stage_dir)
        validate_maintenance_backup_v4(stage_dir / "manifest.json")
        stage_dir.replace(backup_dir)
        sync_directory(backup_root)
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return str(backup_dir)


def projection_diff_report(limit: int = 20) -> str:
    diff = _diff_sets()
    limit = max(0, int(limit))

    lines = [
        "=== Projection / Canonical Diff ===",
        f"Wiki pages: {len(diff['wiki'])}",
        f"SQLite canonical entities: {len(diff['canonical'])}",
        f"index.json nodes: {len(diff['index'])}",
        f"missing_index: {len(diff['missing_index'])}",
        f"extra_index: {len(diff['extra_index'])}",
        f"missing_canonical: {len(diff['missing_canonical'])}",
        f"extra_canonical: {len(diff['extra_canonical'])}",
    ]
    for key in ("missing_index", "extra_index", "missing_canonical", "extra_canonical"):
        sample = sorted(diff[key])[:limit]
        if sample:
            lines.append(f"{key} sample: {', '.join(sample)}")
    return "\n".join(lines)


def _preview_backfill_pages(page_keys: list[str]) -> dict:
    wiki_paths = _wiki_path_map()
    valid = 0
    invalid: list[str] = []
    proposed_entities = 0
    proposed_claims = 0
    for page_key in page_keys:
        path = wiki_paths.get(page_key)
        if path is None:
            invalid.append(f"{page_key}: projection file not found")
            continue
        try:
            frontmatter, body, _ = read_markdown_file(path)
            extracted = extract_page_objects(str(path), frontmatter, body)
        except Exception as exc:
            invalid.append(f"{page_key}: {exc}")
            continue
        entities = extracted.get("entities", [])
        if not entities:
            invalid.append(page_key)
            continue
        valid += 1
        proposed_entities += len(entities)
        proposed_claims += len(extracted.get("claims", []))
    return {
        "valid_pages": valid,
        "invalid_pages": invalid,
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
    }


def canonical_backfill_missing_wiki(dry_run: bool = True, limit: int = 50) -> str:
    """Backfill SQLite canonical rows from Wiki pages missing in canonical."""
    diff = _diff_sets(allow_initialize=not dry_run)
    page_keys = sorted(diff["missing_canonical"])[: max(1, int(limit))]
    preview = _preview_backfill_pages(page_keys)
    if dry_run:
        invalid_sample = preview["invalid_pages"][:10]
        lines = [
            f"[DRY RUN] Would inspect {len(page_keys)} of {len(diff['missing_canonical'])} missing-canonical page(s).",
            f"valid_pages: {preview['valid_pages']}",
            f"invalid_pages: {len(preview['invalid_pages'])}",
            f"proposed_entities: {preview['proposed_entities']}",
            f"proposed_claims: {preview['proposed_claims']}",
        ]
        if invalid_sample:
            lines.append(f"invalid sample: {', '.join(invalid_sample)}")
        return "\n".join(lines)

    if not page_keys:
        return "No missing-canonical wiki pages to backfill."

    backup_dir = create_maintenance_backup("canonical_backfill")
    from vector_lake.mutation_coordinator import execute_mutation_batch

    mutations = []
    wiki_paths = _wiki_path_map()
    for page_key in page_keys:
        path = wiki_paths[page_key]
        content_bytes = path.read_bytes()
        mutations.append(
            {
                "filename": path.name,
                "content": normalize_semantic_text(content_bytes.decode("utf-8")),
                "expected_projection_hash": hashlib.sha256(content_bytes).hexdigest(),
            }
        )
    _, detail = execute_mutation_batch(mutations, validation_mode="schema")
    return (
        f"Backfilled {len(mutations)} wiki page(s) into canonical store. "
        f"{detail}; backup={backup_dir}"
    )


def _canonical_content_drift_candidates(limit: int = 0) -> dict:
    """Return schema-valid Wiki pages whose canonical entity version differs."""
    canonical_versions = governance_store.canonical_page_versions()
    candidates: list[dict] = []
    invalid: list[str] = []
    total_bytes = 0
    total_drift = 0
    selected_limit = max(0, int(limit))
    for path in sorted(iter_markdown_files(get_wiki_dir()), key=lambda path: path.name):
        page_key = path.stem
        if page_key.startswith("System_") or page_key not in canonical_versions:
            continue
        try:
            content_bytes = path.read_bytes()
            content = normalize_semantic_text(content_bytes.decode("utf-8"))
            observed_version = governance_store.canonical_page_version_from_content(
                path.name, content
            )
        except Exception as exc:
            invalid.append(f"{path.name}: parse error: {exc}")
            continue
        if observed_version == canonical_versions[page_key]:
            continue
        total_drift += 1
        total_bytes += len(content_bytes)
        try:
            frontmatter, body = split_frontmatter(content)
            validate_schema(frontmatter, body, path.name)
        except Exception as exc:
            invalid.append(f"{path.name}: schema error: {exc}")
            continue
        if selected_limit == 0 or len(candidates) < selected_limit:
            candidates.append(
                {
                    "filename": path.name,
                    "content": content,
                    "expected_version": canonical_versions[page_key],
                    "expected_projection_hash": hashlib.sha256(
                        content_bytes
                    ).hexdigest(),
                }
            )
    return {
        "total_drift": total_drift,
        "total_bytes": total_bytes,
        "candidates": candidates,
        "invalid": invalid,
    }


def reconcile_canonical_content_from_wiki(
    dry_run: bool = True,
    limit: int = 0,
    batch_size: int = 100,
    backup_reference: str = "",
) -> str:
    """Promote schema-valid richer Wiki content into canonical state without rewriting the pages."""
    preview = _canonical_content_drift_candidates(limit=limit)
    candidates = preview["candidates"]
    lines = [
        "[DRY RUN] Canonical content reconciliation"
        if dry_run
        else "Canonical content reconciliation",
        f"drift_pages: {preview['total_drift']}",
        f"selected_pages: {len(candidates)}",
        f"selected_limit: {max(0, int(limit))}",
        f"total_drift_bytes: {preview['total_bytes']}",
        f"invalid_pages: {len(preview['invalid'])}",
    ]
    if candidates:
        lines.append(
            "sample: " + ", ".join(item["filename"] for item in candidates[:10])
        )
    if preview["invalid"]:
        lines.append("invalid sample: " + ", ".join(preview["invalid"][:10]))
    if dry_run:
        return "\n".join(lines)
    if preview["invalid"]:
        raise RuntimeError(
            "Canonical reconciliation refused because one or more drift pages failed validation: "
            + "; ".join(preview["invalid"][:10])
        )
    if not candidates:
        return "No canonical/Wiki content drift to reconcile."

    if backup_reference:
        backup_path = Path(backup_reference).expanduser().resolve()
        if not backup_path.is_file():
            raise FileNotFoundError(
                f"Verified backup reference does not exist: {backup_path}"
            )
        backup_label = str(backup_path)
    else:
        backup_label = create_maintenance_backup("canonical_content_reconcile")

    from vector_lake.mutation_coordinator import execute_mutation_batch

    chunk_size = max(1, int(batch_size))
    committed = 0
    for offset in range(0, len(candidates), chunk_size):
        batch = candidates[offset : offset + chunk_size]
        execute_mutation_batch(
            batch,
            validation_mode="schema",
            origin="canonical-content-reconcile",
        )
        committed += len(batch)
        log.info(
            "Canonical content reconciliation committed %s/%s pages",
            committed,
            len(candidates),
        )

    from vector_lake.watchdog_app import process_mutation_outbox_batch

    outbox = process_mutation_outbox_batch(limit=max(1, committed))
    after = _canonical_content_drift_candidates(limit=0)
    return (
        f"Reconciled {committed} wiki page(s) into canonical state; "
        f"remaining_drift={after['total_drift']}; "
        f"outbox_completed={outbox['completed']}; outbox_failed={outbox['failed']}; "
        f"backup={backup_label}"
    )


def _canonical_foundation_snapshot() -> dict:
    """Load canonical rows once so a backfill scan does not repeat JSON table scans."""
    conn = get_connection()
    entities_by_page: dict[str, list[dict]] = {}
    claims_by_page: dict[str, list[dict]] = {}
    evidence_by_page: dict[str, list[dict]] = {}
    sources_by_id: dict[str, dict] = {}
    for row in conn.execute("SELECT data_json FROM entities"):
        record = json.loads(row["data_json"])
        page_key = str(record.get("page_key") or "")
        if page_key:
            entities_by_page.setdefault(page_key, []).append(record)
    for row in conn.execute("SELECT data_json FROM claims"):
        record = json.loads(row["data_json"])
        page_key = str((record.get("locator") or {}).get("page_key") or "")
        if page_key:
            claims_by_page.setdefault(page_key, []).append(record)
    for row in conn.execute("SELECT data_json FROM evidence"):
        record = json.loads(row["data_json"])
        page_key = str((record.get("locator") or {}).get("page_key") or "")
        if page_key:
            evidence_by_page.setdefault(page_key, []).append(record)
    for row in conn.execute("SELECT source_id, data_json FROM sources"):
        sources_by_id[str(row["source_id"])] = json.loads(row["data_json"])
    return {
        "entities_by_page": entities_by_page,
        "claims_by_page": claims_by_page,
        "evidence_by_page": evidence_by_page,
        "sources_by_id": sources_by_id,
    }


def _legacy_foundation_payload(
    path: Path,
    frontmatter: dict,
    body: str,
    snapshot: dict,
) -> dict:
    """Derive foundation metadata around legacy IDs without reinterpreting claim text."""
    page_key = path.stem
    entities = list(snapshot["entities_by_page"].get(page_key) or [])
    if not entities:
        raise ValueError("canonical entity row is missing")
    claims = list(snapshot["claims_by_page"].get(page_key) or [])
    evidence = list(snapshot["evidence_by_page"].get(page_key) or [])
    source_ids = {
        str(source_id)
        for claim in claims
        for source_id in (claim.get("source_ids") or [])
        if str(source_id)
    }
    source_ids.update(
        str(record.get("source_id"))
        for record in evidence
        if str(record.get("source_id") or "")
    )
    sources = [
        snapshot["sources_by_id"][source_id]
        for source_id in sorted(source_ids)
        if source_id in snapshot["sources_by_id"]
    ]
    artifacts = [
        resolve_source_artifact(
            str(source.get("raw_ref") or ""),
            source_id=str(source["source_id"]),
            metadata=source,
        )
        for source in sources
        if str(source.get("raw_ref") or "")
    ]
    artifact_by_source = {str(item["source_id"]): item for item in artifacts}
    extraction_run = build_extraction_run(
        page_key=page_key,
        body=body,
        artifact_ids=[str(item["artifact_id"]) for item in artifacts],
        frontmatter=frontmatter,
        extractor_name="vector_lake.foundation_backfill",
        extractor_version="1.0",
    )

    proposed_claims = []
    for claim in claims:
        locator = dict(claim.get("locator") or {})
        proposed_claims.append(
            {
                "claim_id": claim["claim_id"],
                "claim_family_id": version_family_id("claimfamily", page_key, locator),
                "confidence_kind": "legacy_prior",
                "calibrated_probability": None,
                "assessment_status": "unreviewed",
                "extractor_name": "vector_lake.foundation_backfill",
                "extractor_version": "1.0",
                "extraction_run_id": extraction_run["run_id"],
            }
        )

    proposed_evidence = []
    for record in evidence:
        locator = dict(record.get("locator") or {})
        source_id = str(record.get("source_id") or "")
        source = snapshot["sources_by_id"].get(source_id) or {}
        raw_ref = str(source.get("raw_ref") or "")
        artifact = artifact_by_source.get(source_id)
        proposed = {
            "evidence_id": record["evidence_id"],
            "evidence_family_id": version_family_id(
                "evidencefamily",
                page_key,
                {
                    **locator,
                    "source_id": source_id,
                    "kind": record.get("evidence_type"),
                },
            ),
            "projection_locator": locator,
            "extraction_run_id": extraction_run["run_id"],
        }
        if artifact is not None:
            proposed["artifact_id"] = artifact["artifact_id"]
            proposed["source_locator"] = source_locator_for(
                frontmatter,
                raw_ref,
                artifact=artifact,
            )
            proposed.update(
                evidence_independence(
                    raw_ref, path.name, artifact["generation_parent_refs"]
                )
            )
        else:
            proposed.update(
                {
                    "source_locator": {
                        "kind": "unresolved",
                        "source_id": source_id,
                        "reason": "canonical-source-or-raw-reference-missing",
                    },
                    "independence_status": "unknown_missing_source",
                    "lineage_safe": False,
                }
            )
        proposed_evidence.append(proposed)

    proposed_sources = []
    for source in sources:
        artifact = artifact_by_source.get(str(source["source_id"]))
        if artifact is None:
            continue
        proposed_sources.append(
            {
                "source_id": source["source_id"],
                "artifact_id": artifact["artifact_id"],
                "content_hash": artifact.get("content_hash"),
                "hash_algorithm": artifact.get("hash_algorithm"),
                "byte_size": artifact.get("byte_size"),
                "mime_type": artifact.get("mime_type"),
                "storage_uri": artifact.get("storage_uri"),
                "integrity_status": artifact.get("integrity_status"),
                "classification": artifact.get("classification"),
                "retention_policy": artifact.get("retention_policy"),
                "legal_hold": artifact.get("legal_hold"),
                "lineage_id": artifact.get("lineage_id"),
                "generation_parent_refs": artifact.get("generation_parent_refs"),
            }
        )
    return {
        "page_key": page_key,
        "entities": entities,
        "claims": proposed_claims,
        "evidence": proposed_evidence,
        "sources": proposed_sources,
        "source_artifacts": artifacts,
        "extraction_runs": [extraction_run],
    }


def _canonical_only_foundation_payload(page_key: str, snapshot: dict) -> dict:
    """Repair provenance fields when canonical rows have no Wiki projection.

    No extraction run is invented because the original page body is absent.
    """
    claims = list(snapshot["claims_by_page"].get(page_key) or [])
    evidence = list(snapshot["evidence_by_page"].get(page_key) or [])
    source_ids = {
        str(source_id)
        for claim in claims
        for source_id in (claim.get("source_ids") or [])
        if str(source_id)
    }
    source_ids.update(
        str(record.get("source_id"))
        for record in evidence
        if str(record.get("source_id") or "")
    )
    sources = [
        snapshot["sources_by_id"][source_id]
        for source_id in sorted(source_ids)
        if source_id in snapshot["sources_by_id"]
    ]
    artifacts = [
        resolve_source_artifact(
            str(source.get("raw_ref") or ""),
            source_id=str(source["source_id"]),
            metadata=source,
        )
        for source in sources
        if str(source.get("raw_ref") or "")
    ]
    artifact_by_source = {str(item["source_id"]): item for item in artifacts}
    proposed_evidence = []
    for record in evidence:
        source_id = str(record.get("source_id") or "")
        source = snapshot["sources_by_id"].get(source_id) or {}
        raw_ref = str(source.get("raw_ref") or "")
        artifact = artifact_by_source.get(source_id)
        proposed = {"evidence_id": record["evidence_id"]}
        if artifact is not None:
            proposed["artifact_id"] = artifact["artifact_id"]
            proposed["source_locator"] = source_locator_for(
                {},
                raw_ref,
                artifact=artifact,
            )
            proposed.update(
                evidence_independence(
                    raw_ref,
                    f"{page_key}.md",
                    artifact["generation_parent_refs"],
                )
            )
        else:
            proposed.update(
                {
                    "source_locator": {
                        "kind": "unresolved",
                        "source_id": source_id,
                        "reason": "canonical-source-or-raw-reference-missing",
                    },
                    "independence_status": "unknown_missing_source",
                    "lineage_safe": False,
                }
            )
        proposed_evidence.append(proposed)
    proposed_sources = []
    for source in sources:
        artifact = artifact_by_source.get(str(source["source_id"]))
        if artifact is None:
            continue
        proposed_sources.append(
            {
                "source_id": source["source_id"],
                "artifact_id": artifact["artifact_id"],
                "content_hash": artifact.get("content_hash"),
                "hash_algorithm": artifact.get("hash_algorithm"),
                "byte_size": artifact.get("byte_size"),
                "mime_type": artifact.get("mime_type"),
                "storage_uri": artifact.get("storage_uri"),
                "integrity_status": artifact.get("integrity_status"),
                "classification": artifact.get("classification"),
                "retention_policy": artifact.get("retention_policy"),
                "legal_hold": artifact.get("legal_hold"),
                "lineage_id": artifact.get("lineage_id"),
                "generation_parent_refs": artifact.get("generation_parent_refs"),
            }
        )
    return {
        "page_key": page_key,
        "entities": [],
        "claims": [],
        "evidence": proposed_evidence,
        "sources": proposed_sources,
        "source_artifacts": artifacts,
        "extraction_runs": [],
    }


def _payload_needs_foundation_backfill(
    extracted: dict, existing_run_ids: set[str], snapshot: dict
) -> bool:
    runs = list(extracted.get("extraction_runs") or [])
    if runs and str(runs[0]["run_id"]) not in existing_run_ids:
        return True
    record_specs = (
        ("claims", "claim_id", governance_store._CLAIM_FOUNDATION_FIELDS),
        ("evidence", "evidence_id", governance_store._EVIDENCE_FOUNDATION_FIELDS),
        ("sources", "source_id", governance_store._SOURCE_FOUNDATION_FIELDS),
    )
    current_maps = {
        "claims": {
            str(record["claim_id"]): record
            for record in snapshot["claims_by_page"].get(extracted["page_key"], [])
        },
        "evidence": {
            str(record["evidence_id"]): record
            for record in snapshot["evidence_by_page"].get(extracted["page_key"], [])
        },
        "sources": snapshot["sources_by_id"],
    }
    for table_name, key_field, _fields in record_specs:
        for proposed in extracted.get(table_name) or []:
            current = current_maps[table_name].get(str(proposed[key_field])) or {}
            candidate = json.loads(json.dumps(current))
            if governance_store.merge_foundation_record_fields(
                table_name,
                candidate,
                proposed,
            ):
                return True
    return False


def _evidence_foundation_backfill_candidates(limit: int = 500) -> dict:
    """Extract current page revisions and select runs absent from the foundation ledger."""
    init_db()
    conn = get_connection()
    existing_run_ids = {
        str(row["run_id"]) for row in conn.execute("SELECT run_id FROM extraction_runs")
    }
    snapshot = _canonical_foundation_snapshot()
    canonical_keys = set(snapshot["entities_by_page"])
    selected: list[dict] = []
    invalid: list[str] = []
    pending_pages = 0
    current_pages = 0
    projected_page_keys: set[str] = set()
    selected_limit = max(0, int(limit))
    for path in sorted(iter_markdown_files(get_wiki_dir()), key=lambda path: path.name):
        if (
            not path.is_file()
            or path.name.casefold() in EXCLUDED_WIKI_FILES
            or path.name.casefold().startswith("system_")
            or path.stem not in canonical_keys
        ):
            continue
        try:
            frontmatter, body, _ = read_markdown_file(path)
            extracted = _legacy_foundation_payload(path, frontmatter, body, snapshot)
            runs = list(extracted.get("extraction_runs") or [])
            if len(runs) != 1:
                raise ValueError(f"expected one extraction run, received {len(runs)}")
        except Exception as exc:
            invalid.append(f"{path.name}: {exc}")
            continue
        projected_page_keys.add(path.stem)
        if not _payload_needs_foundation_backfill(
            extracted, existing_run_ids, snapshot
        ):
            current_pages += 1
            continue
        pending_pages += 1
        if selected_limit == 0 or len(selected) < selected_limit:
            selected.append(extracted)
    for page_key in sorted(canonical_keys - projected_page_keys):
        try:
            extracted = _canonical_only_foundation_payload(page_key, snapshot)
        except Exception as exc:
            invalid.append(f"{page_key}: {exc}")
            continue
        if not _payload_needs_foundation_backfill(
            extracted,
            existing_run_ids,
            snapshot,
        ):
            current_pages += 1
            continue
        pending_pages += 1
        if selected_limit == 0 or len(selected) < selected_limit:
            selected.append(extracted)
    return {
        "canonical_pages": len(canonical_keys),
        "current_pages": current_pages,
        "pending_pages": pending_pages,
        "selected": selected,
        "invalid": invalid,
    }


def evidence_foundation_backfill(
    dry_run: bool = True,
    limit: int = 500,
    batch_size: int = 100,
    backup_reference: str = "",
) -> str:
    """Backfill auditable foundation metadata without replacing canonical content."""
    preview = _evidence_foundation_backfill_candidates(limit=limit)
    selected = preview["selected"]
    lines = [
        "[DRY RUN] Evidence-foundation backfill"
        if dry_run
        else "Evidence-foundation backfill",
        f"canonical_pages: {preview['canonical_pages']}",
        f"current_pages: {preview['current_pages']}",
        f"pending_pages: {preview['pending_pages']}",
        f"selected_pages: {len(selected)}",
        f"selected_limit: {max(0, int(limit))}",
        f"invalid_pages: {len(preview['invalid'])}",
    ]
    if selected:
        lines.append("sample: " + ", ".join(item["page_key"] for item in selected[:10]))
    if preview["invalid"]:
        lines.append("invalid sample: " + "; ".join(preview["invalid"][:10]))
    if dry_run:
        return "\n".join(lines)
    if preview["invalid"]:
        raise RuntimeError(
            "Evidence-foundation backfill refused because one or more canonical Wiki pages "
            "could not be extracted: " + "; ".join(preview["invalid"][:10])
        )
    if not selected:
        return "No pending evidence-foundation page revisions to backfill."

    if backup_reference:
        backup_path = Path(backup_reference).expanduser().resolve()
        if not backup_path.is_file():
            raise FileNotFoundError(
                f"Verified backup reference does not exist: {backup_path}"
            )
        backup_label = str(backup_path)
    else:
        backup_label = create_maintenance_backup("evidence_foundation_backfill")

    totals = {
        "pages": 0,
        "updated_claims": 0,
        "updated_evidence": 0,
        "updated_sources": 0,
        "source_artifacts": 0,
    }
    chunk_size = max(1, int(batch_size))
    for offset in range(0, len(selected), chunk_size):
        batch = selected[offset : offset + chunk_size]
        with transaction():
            result = governance_store.backfill_evidence_foundation_batch(batch)
        totals["pages"] += int(result["pages"])
        for key in (
            "updated_claims",
            "updated_evidence",
            "updated_sources",
            "source_artifacts",
        ):
            totals[key] += int(result[key])
        log.info(
            "Evidence-foundation backfill committed %s/%s pages",
            totals["pages"],
            len(selected),
        )

    remaining = max(0, int(preview["pending_pages"]) - totals["pages"])
    return (
        f"Backfilled {totals['pages']} page revision(s); "
        f"updated_claims={totals['updated_claims']}; "
        f"updated_evidence={totals['updated_evidence']}; "
        f"updated_sources={totals['updated_sources']}; "
        f"source_artifacts={totals['source_artifacts']}; "
        f"remaining_pending={remaining}; backup={backup_label}"
    )


def _projection_rebuild_root_snapshot() -> dict[str, Path]:
    """Resolve every path provider used by rebuild before business writes."""
    database = peek_db_path().resolve()
    meta = peek_meta_dir().resolve()
    wiki = get_wiki_dir().resolve()
    index = get_index_path().resolve()
    return {
        "database": database,
        "meta": meta,
        "wiki": wiki,
        "index": index,
        "claim_graph": get_claim_graph_path().resolve(),
        "manifest": get_projection_manifest_path().resolve(),
        "legacy_claim_graph": get_legacy_claim_graph_path().resolve(),
        "projection_store": (wiki / ".projection-store").resolve(),
        "backup_root": (meta / "backups").resolve(),
        "publish_lock": Path(str(index) + ".lock").resolve(),
    }


def _assert_projection_rebuild_root_snapshot(
    expected: dict[str, Path],
) -> None:
    observed = _projection_rebuild_root_snapshot()
    if (
        observed.get("database") != expected.get("database")
        or get_db_path().resolve() != expected.get("database")
    ):
        raise RuntimeError("projection_rebuild_database_path_changed")
    if observed != expected:
        raise RuntimeError("projection_rebuild_root_snapshot_changed")


def rebuild_index_projection(dry_run: bool = True) -> str:
    """Rebuild index.json / FTS / claim_graph from SQLite canonical state."""
    if dry_run:
        index_path = get_index_path()
        legacy_capability = (
            _PROJECTION_REBUILD_LEGACY_CAPABILITY
            if index_path.exists() and not is_v2_locator(index_path, "index")
            else None
        )
        diff = _diff_sets(
            allow_initialize=False,
            legacy_rebuild_capability=legacy_capability,
        )
        return (
            "[DRY RUN] Would rebuild index.json, wiki_search_index, and claim_graph.json "
            f"from {len(diff['canonical'])} canonical entity row(s), while preserving existing vec_embeddings. "
            f"Current drift: missing_index={len(diff['missing_index'])}, extra_index={len(diff['extra_index'])}."
        )

    root_snapshot = _projection_rebuild_root_snapshot()
    database_path = root_snapshot["database"]
    with db_store.schema_maintenance_lock(database_path):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)
        _assert_projection_rebuild_root_snapshot(root_snapshot)
        index_path = root_snapshot["index"]
        legacy_capability = (
            _PROJECTION_REBUILD_LEGACY_CAPABILITY
            if index_path.exists() and not is_v2_locator(index_path, "index")
            else None
        )
        _diff_sets(
            # Apply re-runs the authoritative schema/projection preflight only
            # after the schema lock and pending-receipt guard are both held.
            allow_initialize=False,
            legacy_rebuild_capability=legacy_capability,
        )
        _assert_projection_rebuild_root_snapshot(root_snapshot)
        backup_dir = create_maintenance_backup("index_rebuild")
        _assert_projection_rebuild_root_snapshot(root_snapshot)
        output = indexer.generate_index()
        _assert_projection_rebuild_root_snapshot(root_snapshot)
        topology_refreshed = indexer.refresh_graph_topology_if_dirty()
        if not indexer.projection_pair_matches_current_generation():
            raise RuntimeError(
                "Projection rebuild did not publish a topology-current projection pair"
            )
        _assert_projection_rebuild_root_snapshot(root_snapshot)
        after = _diff_sets()
        return (
            f"Rebuilt index projection at {output}. "
            f"missing_index={len(after['missing_index'])}; extra_index={len(after['extra_index'])}; "
            f"topology_refreshed={topology_refreshed}; backup={backup_dir}"
        )


def embedding_backfill_projection(
    dry_run: bool = True, limit: int | None = None, include_existing: bool = False
) -> str:
    """Backfill missing vec_embeddings rows under rate limits without rebuilding index.json."""
    from vector_lake.embedding_scheduler import embedding_backfill

    index_path = get_index_path()
    if not index_path.exists():
        return "index.json not found; run projection-rebuild-index first."
    index_data = indexer.read_committed_index_snapshot(index_path)
    result = embedding_backfill(
        index_data,
        dry_run=dry_run,
        limit=limit,
        include_existing=include_existing,
    )
    before = result.get("coverage_before") or {}
    after = result.get("coverage_after") or before
    lines = [
        "[DRY RUN] Embedding backfill plan"
        if dry_run
        else "Embedding backfill complete",
        f"model: {result.get('model')}",
        f"limits: rpm={result.get('rpm')} tpm={result.get('tpm')} utilization={result.get('utilization')}",
        f"effective_limits: rpm={result.get('effective_rpm')} tpm={result.get('effective_tpm')}",
        f"candidates: {result.get('candidates')}",
        f"estimated_requests: {result.get('estimated_requests')}",
        f"estimated_tokens: {result.get('estimated_tokens')}",
        f"coverage_before: nodes={before.get('nodes')} embedded={before.get('embedded')} missing={before.get('missing')} stale={before.get('stale')}",
    ]
    if not dry_run:
        lines.append(f"embedded_this_run: {result.get('embedded')}")
        lines.append(f"stale_discarded: {result.get('stale_discarded', 0)}")
        lines.append(f"failed_batches: {result.get('failed_batches')}")
        lines.append(
            f"coverage_after: nodes={after.get('nodes')} embedded={after.get('embedded')} missing={after.get('missing')} stale={after.get('stale')}"
        )
    if result.get("skipped"):
        lines.append(f"skipped: {result['skipped']}")
    if result.get("last_error"):
        lines.append(f"last_error: {result['last_error']}")
    return "\n".join(lines)


def _canonical_entity_by_page_key(page_key: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT data_json FROM entities WHERE json_extract(data_json, '$.page_key') = ? LIMIT 1",
        (page_key,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["data_json"])


def _iso_datetime(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    if "T" not in raw and len(raw) == 10:
        return f"{raw}T00:00:00+00:00"
    return raw


def _frontmatter_from_entity(entity: dict) -> dict:
    page_key = _strip_markdown_suffix(
        str(entity.get("page_key") or entity.get("source_page") or "")
    )
    inferred_type = page_key.split("_", 1)[0].lower() if "_" in page_key else "concept"
    entity_type = str(
        entity.get("type") or entity.get("entity_type") or inferred_type or "concept"
    ).lower()
    categories = entity.get("categories") or [entity_type.capitalize()]
    if isinstance(categories, str):
        categories = [categories]
    return {
        "id": entity.get("id") or entity.get("entity_id") or page_key,
        "title": entity.get("title") or entity.get("canonical_name") or page_key,
        "type": entity_type,
        "domain": entity.get("domain") or "General",
        "status": entity.get("status") or "Active",
        "epistemic-status": entity.get("epistemic-status") or "seed",
        "categories": categories,
        "updated": _iso_datetime(entity.get("updated") or entity.get("updated_at")),
        "sources": entity.get("sources") or [],
        "strategic_scope": entity.get("strategic_scope") or "core",
        "evidence_tier": entity.get("evidence_tier") or "primary",
    }


def _body_from_entity(entity: dict, frontmatter: dict) -> str:
    raw_text = str(entity.get("raw_text") or "")
    normalized_raw_text = raw_text.strip()
    entity_type = str(frontmatter.get("type") or "concept").lower()
    title = str(
        frontmatter.get("title")
        or entity.get("canonical_name")
        or entity.get("page_key")
    )
    restored_note = (
        f"{title}：canonical 记录存在，但 Markdown 投影缺失。本页由维护流程从 canonical 元数据恢复，"
        "需要后续补充原始证据与完整编译事实。"
    )
    if entity_type == "source":
        return raw_text if normalized_raw_text else restored_note
    if entity_type == "synthesis":
        return raw_text or (
            "## 核心合成论点 (Core Synthesized Claims)\n\n"
            f"- {restored_note}\n\n"
            "## 支撑拓扑 (Supporting Topology)\n\n"
            "- [mentions:: [[Concept_Agent_Code_Cleanliness]]]\n"
        )
    if (
        normalized_raw_text
        and "## 1. 编译事实" in raw_text
        and "## 2. 证据时间线" in raw_text
    ):
        return raw_text
    slot = (VALID_H3_SLOTS.get(entity_type) or VALID_H3_SLOTS["concept"])[0]
    restored_at = datetime.now(timezone.utc).date().isoformat()
    return (
        "## 1. 编译事实\n\n"
        f"{slot}\n\n"
        f"{restored_note}\n\n"
        "## 2. 证据时间线\n\n"
        f"- [{restored_at}] [Observation] Markdown projection restored from canonical metadata during maintenance.\n"
    )


def restore_missing_wiki_from_canonical(dry_run: bool = True, limit: int = 10) -> str:
    """Restore Markdown projection files for canonical rows whose Wiki page is missing."""
    diff = _diff_sets(allow_initialize=not dry_run)
    page_keys = sorted(diff["extra_canonical"])[: max(1, int(limit))]
    if dry_run:
        return (
            f"[DRY RUN] Would restore {len(page_keys)} of {len(diff['extra_canonical'])} "
            f"canonical-only wiki page(s): {', '.join(page_keys[:10]) if page_keys else '<none>'}"
        )
    if not page_keys:
        return "No canonical-only wiki pages to restore."

    backup_dir = create_maintenance_backup("wiki_restore")
    restored = 0
    skipped: list[str] = []
    unsafe_versions: list[str] = []
    wiki_dir = get_wiki_dir()
    for page_key in page_keys:
        entity = _canonical_entity_by_page_key(page_key)
        if not entity:
            skipped.append(page_key)
            continue
        path = wiki_dir / f"{page_key}.md"
        if path.exists():
            skipped.append(page_key)
            continue
        frontmatter = _frontmatter_from_entity(entity)
        body = _body_from_entity(entity, frontmatter)
        content = f"---\n{dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)}---\n{body}"
        canonical_version = governance_store.canonical_page_versions({page_key}).get(
            page_key
        )
        restored_version = governance_store.canonical_page_version_from_content(
            path.name, content
        )
        if not canonical_version or restored_version != canonical_version:
            unsafe_versions.append(page_key)
            continue
        with transaction():
            enqueue_mutation(
                path.name,
                "update",
                payload_text=content,
                idempotency_key=f"projection-restore:{page_key}:{canonical_version}:{uuid.uuid4().hex}",
                validation_mode="schema",
            )
            materialize_markdown_projection(
                path.name,
                "update",
                content,
                validation_mode="schema",
            )
        restored += 1
    result = f"Restored {restored} missing wiki page(s) from canonical metadata; backup={backup_dir}"
    if skipped:
        result += f"; skipped={', '.join(skipped[:10])}"
    if unsafe_versions:
        result += f"; unsafe-version={','.join(unsafe_versions[:10])}"
    return result
