"""Receipt-bound, crash-resumable recovery of a complete Vector Lake snapshot.

The recovery source is deliberately narrow: a completed, supported maintenance
backup published by :func:`vector_lake.tool_projection.create_maintenance_backup`
(v4 is authoritative; v3 remains read-only compatible). Arbitrary SQLite files
and incomplete or unsupported legacy backup directories are never accepted as
restore sources.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout as FileLockTimeout
import yaml

from vector_lake import db_store, governance_store, indexer, tool_projection
from vector_lake.backup_capacity import (
    assert_legacy_projection_file_size,
    assert_backup_capacity,
    projection_v2_reachable_inventory,
)
from vector_lake.durability import (
    commit_existing_file,
    durable_replace_file,
    sync_directory,
    sync_file,
    sync_open_file,
)
from vector_lake.projection_store_v2 import ProjectionStoreV2
from vector_lake.wiki_utils import (
    _replace_prepared_file_compare_and_swap,
    get_claim_graph_path,
    get_index_path,
    get_projection_manifest_path,
    get_wiki_dir,
    peek_meta_dir,
    validate_wiki_filename,
)
from vector_lake.yaml_utils import dump_yaml


_PLAN_CONTRACT = "vector-lake-snapshot-restore-plan/v1"
_RECEIPT_CONTRACT = "vector-lake-snapshot-restore-receipt/v1"
_FORWARD_CONTRACT = "vector-lake-snapshot-forward-recovery/v1"
_FORWARD_CONTRACT_V2 = "vector-lake-snapshot-forward-recovery/v2"
_MAINTENANCE_MANIFEST_VERSION = 3
_MAINTENANCE_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "created_at",
        "label",
        "copied",
        "artifact_sha256",
        "database_runtime_generations",
        "database_runtime_generation_error",
        "projection_generation",
        "projection_canonical_generation",
        "canonical_projection_consistency",
        "restorable_as_consistent_canonical_projection_snapshot",
        "complete",
    }
)
_SNAPSHOT_ARTIFACTS = (
    "vector_lake.db",
    "index.json",
    "claim_graph.json",
    "projection_pair_manifest.json",
)
_PROJECTION_ARTIFACTS = _SNAPSHOT_ARTIFACTS[1:]
_RESTORE_LOCK_FILENAME = ".snapshot-restore-v1.lock"
_RECEIPT_DIRECTORY = "restore-snapshot-receipts"
_FORWARD_DIRECTORY = "restore-snapshot-forward"
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPERATION_ID = re.compile(r"[0-9a-f]{24}")

# Tests inject process-death boundaries without expanding the public API.
_TEST_FAULT_HOOK: Callable[[str], None] | None = None


def _checkpoint(name: str) -> None:
    hook = _TEST_FAULT_HOOK
    if hook is not None:
        hook(name)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _safe_relative_artifact_name(value: object) -> str:
    name = str(value or "")
    if not name or "\\" in name:
        raise ValueError("artifact_name_invalid")
    relative = Path(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("artifact_name_invalid")
    if relative.as_posix() != name:
        raise ValueError("artifact_name_not_canonical")
    return name


def _is_reparse(path: Path, info: os.stat_result | None = None) -> bool:
    if path.is_symlink():
        return True
    observed = info if info is not None else path.lstat()
    attributes = int(getattr(observed, "st_file_attributes", 0))
    marker = int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(marker and attributes & marker)


def _stable_file_identity(path: Path, *, hash_content: bool = True) -> dict:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    reparse = _is_reparse(path, before)
    identity = {
        "path": str(path),
        "exists": True,
        "device": int(before.st_dev),
        "inode": int(before.st_ino),
        "size": int(before.st_size),
        "mtime_ns": int(before.st_mtime_ns),
        "ctime_ns": int(before.st_ctime_ns),
        "is_file": stat_module.S_ISREG(before.st_mode),
        "reparse": reparse,
    }
    if hash_content and identity["is_file"] and not reparse:
        try:
            identity["sha256"] = _sha256(path)
        except PermissionError:
            identity["unreadable"] = True
            return identity
        after = path.lstat()
        after_identity = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        before_identity = (
            identity["device"],
            identity["inode"],
            identity["size"],
            identity["mtime_ns"],
            identity["ctime_ns"],
        )
        if after_identity != before_identity:
            raise RuntimeError(f"file_changed_while_hashing:{path}")
    return identity


def _database_physical_identity(database_path: Path) -> list[dict]:
    return [
        _stable_file_identity(candidate)
        for candidate in (
            database_path,
            Path(str(database_path) + "-wal"),
            Path(str(database_path) + "-shm"),
        )
    ]


def _identity_matches(actual: dict, expected: dict) -> bool:
    fields = ("path", "exists", "device", "inode", "size", "mtime_ns", "ctime_ns")
    if any(actual.get(field) != expected.get(field) for field in fields):
        return False
    expected_hash = expected.get("sha256")
    return expected_hash is None or hmac.compare_digest(
        str(actual.get("sha256") or ""), str(expected_hash)
    )


def _read_json_stable(path: Path) -> tuple[dict, dict]:
    before = _stable_file_identity(path)
    if (
        not before.get("exists")
        or not before.get("is_file")
        or before.get("reparse")
    ):
        raise ValueError(f"JSON artifact is not a plain regular file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact is not an object: {path}")
    after = _stable_file_identity(path)
    if before != after:
        raise RuntimeError(f"JSON artifact changed while reading: {path}")
    return payload, after


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            sync_open_file(handle)
        durable_replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _projection_content_binding(snapshot: dict) -> dict:
    return db_store._schema_migration_projection_content_binding(snapshot)


def _inspect_database(path: Path) -> dict:
    identity = _stable_file_identity(path)
    result = {
        "path": str(path),
        "identity": identity,
        "sha256": identity.get("sha256", ""),
        "quick_check": "absent" if not identity.get("exists") else "damaged",
        "schema_state": None,
        "runtime_generations": None,
        "error": None,
    }
    if not identity.get("exists"):
        return result
    if not identity.get("is_file") or identity.get("reparse"):
        result["error"] = "database_not_plain_regular_file"
        return result
    connection = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or str(row[0]).casefold() != "ok":
            result["error"] = "database_quick_check_failed"
            return result
        result["quick_check"] = "ok"
        state = db_store.inspect_schema_migration_connection(connection, path)
        result["schema_state"] = state
        if state.get("ready"):
            result["runtime_generations"] = (
                db_store._schema_migration_runtime_generations(connection)
            )
        else:
            result["error"] = "database_schema_not_ready"
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        result["quick_check"] = "damaged"
        result["error"] = f"{type(exc).__name__}:{exc}"
    finally:
        if connection is not None:
            connection.close()
    return result


def _probe_no_writers(database_path: Path) -> dict:
    with db_store._CONNECTIONS_LOCK:
        in_process = len(db_store._CONNECTIONS)
    sidecars = []
    unreadable = []
    for suffix in ("-wal", "-shm"):
        path = Path(str(database_path) + suffix)
        if not path.exists():
            continue
        sidecars.append(suffix)
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except PermissionError:
            unreadable.append(suffix)
    if in_process or unreadable:
        status = "busy"
    elif sidecars:
        status = "quiescent_sidecars_observed"
    elif database_path.exists():
        status = "no_sqlite_sidecars_observed"
    else:
        status = "absent"
    return {
        "in_process_connections": in_process,
        "exclusive_probe": status,
        "sqlite_sidecars": sidecars,
        "unreadable_sidecars": unreadable,
        "observed_no_writer": in_process == 0 and not unreadable,
        "operator_confirmation_required": True,
    }


def _resolve_maintenance_receipt(
    maintenance_receipt: str | Path,
) -> tuple[Path | None, list[str]]:
    issues: list[str] = []
    candidate = Path(maintenance_receipt).expanduser()
    if not candidate.is_absolute():
        issues.append("maintenance_receipt_path_must_be_absolute")
        return None, issues
    if candidate.name != "manifest.json":
        issues.append("maintenance_receipt_must_name_manifest_json")
        return None, issues
    absolute_candidate = Path(os.path.abspath(candidate))
    for path in (absolute_candidate, absolute_candidate.parent):
        if path.exists() and _is_reparse(path):
            issues.append("maintenance_receipt_reparse_forbidden")
            return None, issues
    try:
        receipt_path = candidate.resolve(strict=True)
    except FileNotFoundError:
        issues.append("maintenance_receipt_not_found")
        return None, issues
    backup_root = (peek_meta_dir() / "backups").resolve()
    if receipt_path.parent.parent != backup_root:
        issues.append("maintenance_receipt_outside_authoritative_backup_root")
    for path in (receipt_path, receipt_path.parent, backup_root):
        if path.exists() and _is_reparse(path):
            issues.append("maintenance_receipt_reparse_forbidden")
            break
    return receipt_path, list(dict.fromkeys(issues))


def _validate_maintenance_receipt_v3(
    receipt_path: Path,
) -> tuple[dict | None, list[str]]:
    issues: list[str] = []
    try:
        manifest, manifest_identity = _read_json_stable(receipt_path)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
        return None, ["maintenance_receipt_invalid"]
    if set(manifest) != set(_MAINTENANCE_MANIFEST_KEYS):
        issues.append("maintenance_receipt_shape_unsupported")
    if manifest.get("manifest_version") != _MAINTENANCE_MANIFEST_VERSION:
        issues.append("maintenance_receipt_version_unsupported")
    if manifest.get("complete") is not True:
        issues.append("maintenance_receipt_not_completed")
    if (
        not isinstance(manifest.get("label"), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", manifest["label"])
        is None
    ):
        issues.append("maintenance_receipt_label_invalid")
    if not isinstance(manifest.get("created_at"), str) or not manifest[
        "created_at"
    ].strip():
        issues.append("maintenance_receipt_created_at_invalid")
    if manifest.get("database_runtime_generation_error") is not None:
        issues.append("maintenance_receipt_database_generation_unverified")
    if manifest.get("restorable_as_consistent_canonical_projection_snapshot") is not True:
        issues.append("maintenance_receipt_not_restorable")
    consistency = manifest.get("canonical_projection_consistency")
    if not isinstance(consistency, dict) or consistency.get("status") != "verified":
        issues.append("maintenance_receipt_consistency_unverified")
    copied = manifest.get("copied")
    if (
        not isinstance(copied, list)
        or len(copied) != len(set(copied))
        or set(copied) != set(_SNAPSHOT_ARTIFACTS)
    ):
        issues.append("maintenance_receipt_artifact_set_unsupported")
    expected_hashes = manifest.get("artifact_sha256")
    if not isinstance(expected_hashes, dict) or set(expected_hashes) != set(
        _SNAPSHOT_ARTIFACTS
    ):
        issues.append("maintenance_receipt_hash_set_invalid")
        expected_hashes = {}

    artifacts: dict[str, dict] = {}
    for name in _SNAPSHOT_ARTIFACTS:
        path = receipt_path.parent / name
        if name in _PROJECTION_ARTIFACTS:
            try:
                assert_legacy_projection_file_size(path)
            except (OSError, ValueError):
                issues.append(f"maintenance_projection_artifact_size_invalid:{name}")
                continue
        identity = _stable_file_identity(path)
        expected = expected_hashes.get(name)
        if (
            not identity.get("exists")
            or not identity.get("is_file")
            or identity.get("reparse")
        ):
            issues.append(f"maintenance_artifact_not_plain_file:{name}")
            continue
        if not isinstance(expected, str) or _HEX_SHA256.fullmatch(expected) is None:
            issues.append(f"maintenance_artifact_hash_invalid:{name}")
        elif not hmac.compare_digest(identity["sha256"], f"sha256:{expected}"):
            issues.append(f"maintenance_artifact_hash_mismatch:{name}")
        artifacts[name] = {
            "path": str(path),
            "sha256": identity["sha256"],
            "bytes": identity["size"],
            "identity": identity,
        }

    database = None
    projection = None
    if not issues:
        database_path = receipt_path.parent / "vector_lake.db"
        if any(Path(str(database_path) + suffix).exists() for suffix in ("-wal", "-shm")):
            issues.append("maintenance_database_not_standalone")
        database = _inspect_database(database_path)
        if database["quick_check"] != "ok":
            issues.append("maintenance_database_quick_check_failed")
        elif not (database.get("schema_state") or {}).get("ready"):
            issues.append("maintenance_database_schema_unsupported")
        if database.get("runtime_generations") != manifest.get(
            "database_runtime_generations"
        ):
            issues.append("maintenance_database_generation_mismatch")

        try:
            index_payload, _index_identity = _read_json_stable(
                receipt_path.parent / "index.json"
            )
            graph_payload, _graph_identity = _read_json_stable(
                receipt_path.parent / "claim_graph.json"
            )
            sidecar, _sidecar_identity = _read_json_stable(
                receipt_path.parent / "projection_pair_manifest.json"
            )
            generation = indexer.validate_projection_pair(index_payload, graph_payload)
            canonical_generation = indexer.projection_canonical_generation(
                index_payload, graph_payload
            )
            sidecar_manifest, sidecar_artifacts = indexer._validate_projection_sidecar(
                sidecar
            )
            if sidecar_manifest != index_payload.get(indexer.PROJECTION_MANIFEST_KEY):
                raise ValueError("projection sidecar manifest mismatch")
            for name in ("index.json", "claim_graph.json"):
                metadata = sidecar_artifacts.get(name)
                artifact = artifacts[name]
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("sha256")
                    != artifact["sha256"].removeprefix("sha256:")
                    or metadata.get("bytes") != artifact["bytes"]
                ):
                    raise ValueError(f"projection sidecar artifact mismatch:{name}")
            if generation != manifest.get("projection_generation"):
                raise ValueError("projection generation mismatch")
            if canonical_generation != manifest.get("projection_canonical_generation"):
                raise ValueError("projection canonical generation mismatch")
            if canonical_generation.get("status") != "verified":
                raise ValueError("projection canonical generation unverified")
            projection = {
                "generation": generation,
                "canonical_generation": canonical_generation,
                "artifacts": {
                    name: artifacts[name] for name in _PROJECTION_ARTIFACTS
                },
            }
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            indexer.ProjectionPairContractError,
        ):
            issues.append("maintenance_projection_pair_invalid")

    if issues:
        return None, list(dict.fromkeys(issues))
    binding = {
        "contract": "vector-lake-maintenance-backup/v3",
        "path": str(receipt_path),
        "file_sha256": manifest_identity["sha256"],
        "directory": str(receipt_path.parent),
        "manifest": manifest,
        "artifacts": artifacts,
        "database": database,
        "projection": projection,
    }
    return binding, []


def _validate_maintenance_receipt_v4(
    receipt_path: Path,
) -> tuple[dict | None, list[str]]:
    try:
        manifest_with_identity, inventory = (
            tool_projection.validate_maintenance_backup_v4(receipt_path)
        )
        manifest_identity = _stable_file_identity(receipt_path)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return None, ["maintenance_receipt_invalid"]
    manifest = {
        key: value
        for key, value in manifest_with_identity.items()
        if not str(key).startswith("_")
    }
    issues: list[str] = []
    if (
        not isinstance(manifest.get("label"), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", manifest["label"])
        is None
    ):
        issues.append("maintenance_receipt_label_invalid")
    if not isinstance(manifest.get("created_at"), str) or not manifest[
        "created_at"
    ].strip():
        issues.append("maintenance_receipt_created_at_invalid")
    if manifest.get("database_runtime_generation_error") is not None:
        issues.append("maintenance_receipt_database_generation_unverified")
    if manifest.get("restorable_as_consistent_canonical_projection_snapshot") is not True:
        issues.append("maintenance_receipt_not_restorable")
    consistency = manifest.get("canonical_projection_consistency")
    if not isinstance(consistency, dict) or consistency.get("status") != "verified":
        issues.append("maintenance_receipt_consistency_unverified")
    projection_format = manifest.get("projection_format")
    if not (
        (projection_format == 2 and inventory is not None)
        or (projection_format == 1 and inventory is None)
    ):
        issues.append("maintenance_receipt_projection_format_unsupported")

    artifacts: dict[str, dict] = {}
    copied = manifest.get("copied") or []
    for name in copied:
        path = receipt_path.parent / Path(name)
        identity = _stable_file_identity(path)
        artifacts[name] = {
            "path": str(path),
            "sha256": identity.get("sha256", ""),
            "bytes": identity.get("size", 0),
            "identity": identity,
        }
    if "vector_lake.db" not in artifacts:
        issues.append("maintenance_receipt_database_missing")

    database = None
    if not issues:
        database_path = receipt_path.parent / "vector_lake.db"
        if any(
            Path(str(database_path) + suffix).exists() for suffix in ("-wal", "-shm")
        ):
            issues.append("maintenance_database_not_standalone")
        database = _inspect_database(database_path)
        if database["quick_check"] != "ok":
            issues.append("maintenance_database_quick_check_failed")
        elif not (database.get("schema_state") or {}).get("ready"):
            issues.append("maintenance_database_schema_unsupported")
        if database.get("runtime_generations") != manifest.get(
            "database_runtime_generations"
        ):
            issues.append("maintenance_database_generation_mismatch")

    projection = None
    if not issues and projection_format == 2 and inventory is not None:
        object_artifacts = {
            name: artifacts[name]
            for name in manifest["projection_v2"]["object_artifacts"]
        }
        static_artifacts = {
            name: artifacts[name] for name in _PROJECTION_ARTIFACTS
        }
        canonical_generation = manifest.get("projection_canonical_generation")
        if (
            not isinstance(canonical_generation, dict)
            or canonical_generation.get("status") != "verified"
            or canonical_generation.get("runtime_generations")
            != inventory["canonical_generation"]
        ):
            issues.append("maintenance_projection_canonical_generation_invalid")
        projection = {
            "format_version": 2,
            "generation": inventory["projection_generation"],
            "canonical_generation": canonical_generation,
            "sidecar": inventory["sidecar"],
            "sidecar_sha256": "sha256:" + inventory["sidecar_sha256"],
            "roots": inventory["roots"],
            "artifacts": static_artifacts,
            "object_artifacts": object_artifacts,
            "object_count": inventory["object_count"],
            "reachable_object_bytes": inventory["reachable_object_bytes"],
        }
    elif not issues and projection_format == 1 and inventory is None:
        canonical_generation = manifest.get("projection_canonical_generation")
        if (
            not isinstance(canonical_generation, dict)
            or canonical_generation.get("status") != "verified"
        ):
            issues.append("maintenance_projection_canonical_generation_invalid")
        else:
            projection = {
                "format_version": 1,
                "generation": manifest.get("projection_generation"),
                "canonical_generation": canonical_generation,
                "artifacts": {
                    name: artifacts[name] for name in _PROJECTION_ARTIFACTS
                },
            }
    if issues or database is None or projection is None:
        return None, list(dict.fromkeys(issues))
    return {
        "contract": "vector-lake-maintenance-backup/v4",
        "path": str(receipt_path),
        "file_sha256": manifest_identity["sha256"],
        "directory": str(receipt_path.parent),
        "manifest": manifest,
        "artifacts": artifacts,
        "database": database,
        "projection": projection,
    }, []


def _validate_maintenance_receipt(receipt_path: Path) -> tuple[dict | None, list[str]]:
    try:
        manifest, _identity = _read_json_stable(receipt_path)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
        return None, ["maintenance_receipt_invalid"]
    version = manifest.get("manifest_version")
    if version == 4:
        return _validate_maintenance_receipt_v4(receipt_path)
    if version == 3:
        return _validate_maintenance_receipt_v3(receipt_path)
    return None, ["maintenance_receipt_version_unsupported"]


def _current_projection() -> tuple[dict, list[str]]:
    try:
        snapshot = db_store._schema_migration_projection_snapshot()
    except (OSError, RuntimeError) as exc:
        return {
            "contract": "vector-lake-pre-projection/v1",
            "status": "unreadable",
            "issues": [f"projection_snapshot_failed:{type(exc).__name__}"],
            "artifacts": [],
        }, ["current_projection_changed_during_preview"]
    return snapshot, []


def _wiki_identity() -> tuple[list[dict], list[str]]:
    wiki_dir = get_wiki_dir().resolve()
    issues: list[str] = []
    identities: list[dict] = []
    if not wiki_dir.exists():
        return identities, issues
    before_names = sorted(
        path.name for path in wiki_dir.iterdir() if path.suffix.casefold() == ".md"
    )
    for name in before_names:
        path = wiki_dir / name
        identity = _stable_file_identity(path)
        identities.append({"name": name, **identity})
    after_names = sorted(
        path.name for path in wiki_dir.iterdir() if path.suffix.casefold() == ".md"
    )
    if before_names != after_names:
        issues.append("wiki_inventory_changed_during_preview")
    return identities, issues


def _canonical_wiki_materialization(
    target: dict,
    wiki_identity: list[dict],
    *,
    canonical_database_path: Path | None = None,
) -> tuple[dict, list[str]]:
    issues: list[str] = []
    existing: dict[str, dict] = {}
    for item in wiki_identity:
        key = str(item["name"]).casefold()
        if key in existing:
            issues.append(f"wiki_casefold_collision:{item['name']}")
        else:
            existing[key] = item
    grouped: dict[str, list[dict]] = defaultdict(list)
    source_database = (
        canonical_database_path
        if canonical_database_path is not None
        else Path(target["database"]["path"])
    ).resolve()
    connection = sqlite3.connect(
        f"{source_database.as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT entity_id, data_json FROM entities ORDER BY entity_id"
        ).fetchall()
        for row in rows:
            try:
                entity = json.loads(str(row["data_json"]))
            except (TypeError, ValueError):
                issues.append(f"canonical_entity_json_invalid:{row['entity_id']}")
                continue
            page_key = str(entity.get("page_key") or "")
            if page_key:
                grouped[page_key].append(entity)
        versions = governance_store.canonical_page_versions(connection=connection)
    finally:
        connection.close()

    create: list[dict] = []
    rebuild: list[dict] = []
    preserved: list[str] = []
    for page_key in sorted(grouped):
        filename = f"{page_key}.md"
        try:
            validate_wiki_filename(filename)
        except ValueError:
            issues.append(f"canonical_wiki_filename_invalid:{filename}")
            continue
        if len(grouped[page_key]) != 1:
            issues.append(f"canonical_wiki_page_not_reconstructable:{filename}")
            continue
        entity = grouped[page_key][0]
        frontmatter = tool_projection._frontmatter_from_entity(entity)
        body = tool_projection._body_from_entity(entity, frontmatter)
        content = (
            "---\n"
            + dump_yaml(
                frontmatter,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
            + "---\n"
            + body
        )
        expected_version = versions.get(page_key)
        restored_version = governance_store.canonical_page_version_from_content(
            filename, content
        )
        if not expected_version or restored_version != expected_version:
            issues.append(f"canonical_wiki_rebuild_unverifiable:{filename}")
            continue
        encoded = content.encode("utf-8")
        expected = {
            "filename": filename,
            "canonical_version": expected_version,
            "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }
        observed = existing.get(filename.casefold())
        if observed is None:
            create.append(expected)
            continue
        if (
            observed.get("reparse")
            or not observed.get("is_file")
            or observed.get("unreadable")
        ):
            issues.append(f"canonical_wiki_target_not_plain_file:{filename}")
            continue
        path = get_wiki_dir() / str(observed["name"])
        try:
            raw = path.read_bytes()
        except OSError:
            issues.append(f"canonical_wiki_target_unreadable:{filename}")
            continue
        after = _stable_file_identity(path)
        if not _identity_matches(after, observed):
            issues.append(f"canonical_wiki_target_changed_during_preview:{filename}")
            continue
        try:
            observed_version = governance_store.canonical_page_version_from_content(
                filename, raw.decode("utf-8")
            )
        except (UnicodeError, ValueError, yaml.YAMLError):
            observed_version = ""
        if observed_version == expected_version:
            preserved.append(str(observed["name"]))
            continue
        rebuild.append({**expected, "source_identity": observed})
    if create and rebuild:
        action = "restore_missing_and_rebuild_canonical"
    elif rebuild:
        action = "rebuild_canonical_from_snapshot"
    elif create:
        action = "restore_missing_from_canonical"
    else:
        action = "verify_only"
    return {
        "action": action,
        "missing": [item["filename"] for item in create],
        "create": create,
        "rebuild": rebuild,
        "preserved_existing": preserved,
        "identity": wiki_identity,
    }, list(dict.fromkeys(issues))


def _database_matches_target(current: dict, target: dict) -> bool:
    physical = current.get("physical") or []
    if len(physical) != 3 or any(item.get("exists") for item in physical[1:]):
        return False
    return bool(
        physical[0].get("exists")
        and hmac.compare_digest(
            str(current.get("sha256") or ""),
            str(target["database"]["sha256"]),
        )
        and current.get("quick_check") == "ok"
    )


def _projection_matches_target(current: dict, target: dict) -> bool:
    target_projection = target.get("projection") or {}
    if target_projection.get("format_version") == 2:
        try:
            live = projection_v2_reachable_inventory()
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return False
        if live is None:
            return False
        target_digests = {
            Path(name).stem
            for name in target_projection.get("object_artifacts") or {}
        }
        live_digests = {str(item["sha256"]) for item in live["objects"]}
        return bool(
            current.get("status") == "captured"
            and not current.get("issues")
            and live["projection_generation"] == target_projection.get("generation")
            and live["sidecar_sha256"]
            == str(target_projection.get("sidecar_sha256") or "").removeprefix(
                "sha256:"
            )
            and live["roots"] == target_projection.get("roots")
            and live_digests == target_digests
        )
    current_artifacts = {
        str(item.get("name")): str(item.get("sha256") or "")
        for item in current.get("artifacts") or []
        if item.get("source_identity", {}).get("exists")
    }
    expected = {
        name: str(target["artifacts"][name]["sha256"])
        for name in _PROJECTION_ARTIFACTS
    }
    return (
        current.get("status") == "captured"
        and not current.get("issues")
        and current_artifacts == expected
    )


def _source_binding(current: dict, wiki: dict) -> dict:
    return {
        "database": {
            "physical": current["database"]["physical"],
            "quick_check": current["database"]["quick_check"],
            "sha256": current["database"].get("sha256", ""),
        },
        "projection": _projection_content_binding(current["projection"]),
        "wiki_identity": wiki["identity"],
    }


def _receipt_payload_valid(payload: dict, expected_status: str) -> bool:
    if (
        payload.get("contract") != _RECEIPT_CONTRACT
        or payload.get("status") != expected_status
    ):
        return False
    stored = payload.get("receipt_fingerprint")
    if not isinstance(stored, str):
        return False
    unsigned = dict(payload)
    unsigned.pop("receipt_fingerprint", None)
    return hmac.compare_digest(stored, _fingerprint(unsigned))


def _receipt_wiki_state(payload: dict) -> tuple[list[dict], list[dict], bool]:
    create = payload.get("wiki_create")
    rebuild = payload.get("wiki_rebuild", [])
    if not isinstance(create, list) or not isinstance(rebuild, list):
        return [], [], False
    completed: list[dict] = []
    remaining: list[dict] = []
    seen: set[str] = set()
    for mode, expected in (("create", create), ("rebuild", rebuild)):
        for item in expected:
            if not isinstance(item, dict):
                return [], [], False
            filename = str(item.get("filename") or "")
            digest = str(item.get("sha256") or "")
            try:
                expected_bytes = int(item.get("bytes"))
            except (TypeError, ValueError):
                return [], [], False
            if (
                not filename
                or filename in seen
                or Path(filename).name != filename
                or not str(item.get("canonical_version") or "")
                or not digest.startswith("sha256:")
                or _HEX_SHA256.fullmatch(digest.removeprefix("sha256:")) is None
                or expected_bytes < 0
            ):
                return [], [], False
            seen.add(filename)
            path = get_wiki_dir() / filename
            identity = _stable_file_identity(path)
            target_matches = bool(
                identity.get("exists")
                and identity.get("is_file")
                and not identity.get("reparse")
                and not identity.get("unreadable")
                and hmac.compare_digest(
                    str(identity.get("sha256") or ""), digest
                )
                and expected_bytes == identity.get("size")
            )
            action = {**item, "mode": mode}
            source: dict | None = None
            if mode == "rebuild":
                raw_source = item.get("source_identity")
                if not isinstance(raw_source, dict):
                    return [], [], False
                source = raw_source
                source_digest = str(source.get("sha256") or "")
                try:
                    source_path = Path(str(source.get("path") or "")).resolve()
                    source_bytes = int(source.get("size"))
                except (OSError, RuntimeError, TypeError, ValueError):
                    return [], [], False
                bound_wiki = (payload.get("source_binding") or {}).get(
                    "wiki_identity"
                )
                if (
                    source_path != path.resolve()
                    or not isinstance(bound_wiki, list)
                    or source not in bound_wiki
                    or not source.get("exists")
                    or not source.get("is_file")
                    or source.get("reparse")
                    or source.get("unreadable")
                    or source_bytes < 0
                    or not source_digest.startswith("sha256:")
                    or _HEX_SHA256.fullmatch(
                        source_digest.removeprefix("sha256:")
                    )
                    is None
                ):
                    return [], [], False
            if target_matches:
                completed.append(action)
                continue
            if mode == "create":
                if identity.get("exists"):
                    return [], [], False
                remaining.append(action)
                continue
            if source is None or not _identity_matches(identity, source):
                return [], [], False
            remaining.append(action)
    return completed, remaining, True


def _validate_forward_bundle(
    binding: dict,
    *,
    expected_source_binding: dict | None = None,
) -> tuple[dict | None, list[str]]:
    issues: list[str] = []
    try:
        raw_directory = Path(str(binding["directory"]))
        raw_manifest_path = Path(str(binding["manifest_path"]))
        if (
            (raw_directory.exists() and _is_reparse(raw_directory))
            or (raw_manifest_path.exists() and _is_reparse(raw_manifest_path))
        ):
            raise ValueError("forward recovery reparse is forbidden")
        directory = raw_directory.resolve(strict=True)
        manifest_path = raw_manifest_path.resolve(strict=True)
    except (KeyError, OSError, RuntimeError, ValueError):
        return None, ["forward_recovery_bundle_invalid"]
    expected_root = (peek_meta_dir() / _FORWARD_DIRECTORY).resolve()
    if directory.parent != expected_root or manifest_path != directory / "manifest.json":
        return None, ["forward_recovery_bundle_invalid"]
    if _is_reparse(directory) or _is_reparse(manifest_path):
        return None, ["forward_recovery_bundle_invalid"]
    try:
        manifest, manifest_identity = _read_json_stable(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
        return None, ["forward_recovery_bundle_invalid"]
    if not hmac.compare_digest(
        str(binding.get("manifest_sha256") or ""),
        str(manifest_identity.get("sha256") or ""),
    ):
        issues.append("forward_recovery_bundle_invalid")
    forward_contract = manifest.get("contract")
    if forward_contract not in {_FORWARD_CONTRACT, _FORWARD_CONTRACT_V2} or manifest.get(
        "status"
    ) != "complete":
        issues.append("forward_recovery_bundle_invalid")
    stored_fingerprint = manifest.get("manifest_fingerprint")
    unsigned = dict(manifest)
    unsigned.pop("manifest_fingerprint", None)
    if not isinstance(stored_fingerprint, str) or not hmac.compare_digest(
        stored_fingerprint, _fingerprint(unsigned)
    ):
        issues.append("forward_recovery_bundle_invalid")
    if expected_source_binding is not None and manifest.get(
        "source_binding"
    ) != expected_source_binding:
        issues.append("forward_recovery_bundle_invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        issues.append("forward_recovery_bundle_invalid")
        artifacts = []
    names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            issues.append("forward_recovery_bundle_invalid")
            continue
        name = str(artifact.get("name") or "")
        try:
            safe_name = _safe_relative_artifact_name(name)
        except ValueError:
            issues.append("forward_recovery_bundle_invalid")
            continue
        if (
            name in names
            or (
                forward_contract == _FORWARD_CONTRACT
                and Path(safe_name).name != safe_name
            )
        ):
            issues.append("forward_recovery_bundle_invalid")
            continue
        names.add(name)
        path = directory / Path(name)
        identity = _stable_file_identity(path)
        if (
            not identity.get("exists")
            or not identity.get("is_file")
            or identity.get("reparse")
            or int(artifact.get("bytes") or -1) != identity.get("size")
            or not hmac.compare_digest(
                str(artifact.get("sha256") or ""),
                str(identity.get("sha256") or ""),
            )
        ):
            issues.append("forward_recovery_bundle_invalid")
    if forward_contract == _FORWARD_CONTRACT_V2:
        actual_files: set[str] = set()
        expected_directories = {directory}
        for name in names:
            parent = Path(name).parent
            while parent != Path("."):
                expected_directories.add(directory / parent)
                parent = parent.parent
        actual_directories = {directory}
        scanned = 0
        try:
            for current_root, directory_names, file_names in os.walk(
                directory, followlinks=False
            ):
                current = Path(current_root)
                for name in directory_names:
                    child = current / name
                    details = child.lstat()
                    if _is_reparse(child, details):
                        raise ValueError("forward reparse")
                    actual_directories.add(child)
                for name in file_names:
                    scanned += 1
                    if scanned > 200_000:
                        raise ValueError("forward file limit")
                    child = current / name
                    relative = child.relative_to(directory).as_posix()
                    if relative != "manifest.json":
                        actual_files.add(relative)
            if actual_files != names or actual_directories != expected_directories:
                raise ValueError("forward unknown artifact")
        except (OSError, ValueError):
            issues.append("forward_recovery_bundle_invalid")
    return (manifest if not issues else None), list(dict.fromkeys(issues))


def _scan_restore_receipts(
    target: dict,
    database_path: Path,
    current: dict,
    wiki: dict,
) -> tuple[dict | None, dict | None, list[str]]:
    root = peek_meta_dir() / _RECEIPT_DIRECTORY
    if not root.exists():
        return None, None, []
    issues: list[str] = []
    valid_completed: dict[str, tuple[dict, dict, Path]] = {}
    completed_paths = sorted(root.glob("*.completed.json"))
    for path in completed_paths:
        try:
            payload, identity = _read_json_stable(path)
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
            issues.append("completed_restore_receipt_invalid")
            continue
        target_receipt = payload.get("target_receipt")
        if not isinstance(target_receipt, dict):
            issues.append("completed_restore_receipt_invalid")
            continue
        if target_receipt.get("file_sha256") != target["file_sha256"] or payload.get(
            "database_path"
        ) != str(database_path):
            continue
        operation_id = str(payload.get("operation_id") or "")
        if (
            _OPERATION_ID.fullmatch(operation_id) is None
            or path.name != f"{operation_id}.completed.json"
            or not _receipt_payload_valid(payload, "completed")
        ):
            issues.append("completed_restore_receipt_invalid")
            continue
        _manifest, forward_issues = _validate_forward_bundle(
            payload.get("forward_recovery") or {},
            expected_source_binding=payload.get("source_binding"),
        )
        if forward_issues:
            issues.extend(forward_issues)
            continue
        _wiki_existing, wiki_remaining, wiki_valid = _receipt_wiki_state(payload)
        if not wiki_valid or wiki_remaining:
            issues.append("completed_restore_wiki_invalid")
            continue
        valid_completed[operation_id] = (
            payload,
            identity,
            path,
        )

    db_matches = _database_matches_target(current["database"], target)
    projection_matches = _projection_matches_target(current["projection"], target)
    if (
        db_matches
        and projection_matches
        and not wiki["missing"]
        and not wiki["rebuild"]
        and valid_completed
    ):
        operation_id = sorted(valid_completed)[-1]
        payload, identity, path = valid_completed[operation_id]
        return {
            "path": str(path),
            "file_sha256": identity["sha256"],
            "receipt": payload,
        }, None, list(dict.fromkeys(issues))

    pending_matches: list[dict] = []
    for path in sorted(root.glob("*.pending.json")):
        try:
            payload, identity = _read_json_stable(path)
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
            issues.append("pending_restore_receipt_invalid")
            continue
        target_receipt = payload.get("target_receipt")
        if not isinstance(target_receipt, dict):
            issues.append("pending_restore_receipt_invalid")
            continue
        if target_receipt.get("file_sha256") != target["file_sha256"] or payload.get(
            "database_path"
        ) != str(database_path):
            continue
        operation_id = str(payload.get("operation_id") or "")
        if operation_id in valid_completed:
            continue
        if (
            _OPERATION_ID.fullmatch(operation_id) is None
            or path.name != f"{operation_id}.pending.json"
            or not _receipt_payload_valid(payload, "pending")
        ):
            issues.append("pending_restore_receipt_invalid")
            continue
        _manifest, forward_issues = _validate_forward_bundle(
            payload.get("forward_recovery") or {},
            expected_source_binding=payload.get("source_binding"),
        )
        if forward_issues:
            issues.extend(forward_issues)
            continue
        _wiki_existing, _wiki_remaining, wiki_valid = _receipt_wiki_state(payload)
        if not wiki_valid:
            issues.append("pending_restore_wiki_invalid")
            continue
        pending_matches.append(
            {
                "path": str(path),
                "file_sha256": identity["sha256"],
                "receipt": payload,
            }
        )
    if len(pending_matches) > 1:
        issues.append("multiple_pending_restore_receipts")
        return None, None, list(dict.fromkeys(issues))
    return None, (pending_matches[0] if pending_matches else None), list(
        dict.fromkeys(issues)
    )


def _invalid_preview(
    maintenance_receipt: str | Path,
    database_path: Path,
    issues: list[str],
) -> dict:
    core = {
        "contract": _PLAN_CONTRACT,
        "maintenance_receipt": str(maintenance_receipt),
        "database_path": str(database_path),
        "issues": list(dict.fromkeys(issues)),
        "can_apply": False,
        "confirm_no_writers_required": True,
    }
    return {**core, "dry_run": True, "fingerprint": _fingerprint(core)}


def preview_restore_snapshot(
    maintenance_receipt: str | Path,
    db_path: str | Path | None = None,
) -> dict:
    """Return an exact, physically bound recovery plan without writing state."""
    database_path = Path(db_path) if db_path is not None else db_store.peek_db_path()
    database_path = database_path.expanduser().resolve()
    receipt_path, issues = _resolve_maintenance_receipt(maintenance_receipt)
    if receipt_path is None or issues:
        return _invalid_preview(maintenance_receipt, database_path, issues)
    target, target_issues = _validate_maintenance_receipt(receipt_path)
    if target is None:
        return _invalid_preview(receipt_path, database_path, target_issues)

    before_physical = _database_physical_identity(database_path)
    database = _inspect_database(database_path)
    no_writers = _probe_no_writers(database_path)
    after_physical = _database_physical_identity(database_path)
    current_issues: list[str] = []
    if before_physical != after_physical:
        current_issues.append("current_database_changed_during_preview")
    database["physical"] = after_physical
    database["sha256"] = after_physical[0].get("sha256", "")
    if no_writers["in_process_connections"]:
        current_issues.append("in_process_database_connections_open")
    if no_writers["exclusive_probe"] == "busy":
        current_issues.append("active_sqlite_writer_detected")
    if any(item.get("unreadable") for item in after_physical):
        current_issues.append("active_sqlite_writer_detected")

    projection, projection_issues = _current_projection()
    wiki_identity, wiki_identity_issues = _wiki_identity()
    wiki, wiki_issues = _canonical_wiki_materialization(target, wiki_identity)
    current = {"database": database, "projection": projection}
    completed, pending, receipt_issues = _scan_restore_receipts(
        target, database_path, current, wiki
    )
    issues = list(
        dict.fromkeys(
            target_issues
            + current_issues
            + projection_issues
            + wiki_identity_issues
            + wiki_issues
            + receipt_issues
        )
    )
    db_matches = _database_matches_target(database, target)
    projection_matches = _projection_matches_target(projection, target)
    if completed is not None:
        recovery_action = "already_completed"
        operation_id = completed["receipt"]["operation_id"]
    elif pending is not None:
        operation_id = pending["receipt"]["operation_id"]
        if not db_matches:
            recovery_action = "resume_database_and_projection_restore"
        elif not projection_matches:
            recovery_action = "resume_projection_restore"
        elif wiki["missing"] or wiki["rebuild"]:
            recovery_action = "resume_wiki_restore"
        else:
            recovery_action = "publish_completed_receipt"
    else:
        seed = {
            "target_receipt": {
                "path": target["path"],
                "file_sha256": target["file_sha256"],
            },
            "database_path": str(database_path),
            "source_binding": _source_binding(current, wiki),
        }
        operation_id = _fingerprint(seed).removeprefix("sha256:")[:24]
        recovery_action = (
            "record_completed_noop"
            if (
                db_matches
                and projection_matches
                and not wiki["missing"]
                and not wiki["rebuild"]
            )
            else "restore_database_projection_and_wiki"
        )

    meta_dir = peek_meta_dir()
    receipt_root = meta_dir / _RECEIPT_DIRECTORY
    forward_root = meta_dir / _FORWARD_DIRECTORY
    paths = {
        "forward_directory": str((forward_root / operation_id).resolve()),
        "pending_receipt_path": str(
            (receipt_root / f"{operation_id}.pending.json").resolve()
        ),
        "completed_receipt_path": str(
            (receipt_root / f"{operation_id}.completed.json").resolve()
        ),
    }
    projection_action = (
        "preserve_verified_target_pair"
        if projection_matches
        else "restore_committed_pair"
    )
    core = {
        "contract": _PLAN_CONTRACT,
        "maintenance_receipt": {
            "path": target["path"],
            "file_sha256": target["file_sha256"],
            "manifest_version": target["manifest"]["manifest_version"],
            "projection_generation": target["projection"]["generation"],
            "projection_format": target["projection"].get("format_version", 1),
            "projection_roots": target["projection"].get("roots"),
            "projection_object_count": target["projection"].get("object_count", 0),
            "database_sha256": target["database"]["sha256"],
            "artifact_sha256": {
                name: target["artifacts"][name]["sha256"]
                for name in _SNAPSHOT_ARTIFACTS
            },
        },
        "database_path": str(database_path),
        "operation_id": operation_id,
        "current": current,
        "source_binding": _source_binding(current, wiki),
        "no_writers": no_writers,
        "database_action": "preserve_verified_target_database"
        if db_matches
        else "restore_database",
        "projection_action": projection_action,
        "wiki_action": wiki,
        "recovery_action": recovery_action,
        "paths": paths,
        "completed_restore_receipt": completed,
        "pending_restore_receipt": pending,
        "issues": issues,
        "can_apply": not issues,
        "confirm_no_writers_required": True,
    }
    return {**core, "dry_run": True, "fingerprint": _fingerprint(core)}


def _create_forward_bundle(plan: dict) -> dict:
    final_directory = Path(plan["paths"]["forward_directory"])
    manifest_path = final_directory / "manifest.json"
    if final_directory.exists():
        identity = _stable_file_identity(manifest_path)
        binding = {
            "directory": str(final_directory),
            "manifest_path": str(manifest_path),
            "manifest_sha256": identity.get("sha256", ""),
        }
        _manifest, issues = _validate_forward_bundle(
            binding, expected_source_binding=plan["source_binding"]
        )
        if issues:
            raise RuntimeError("Existing forward recovery bundle is invalid")
        return binding

    source_artifacts: list[tuple[str, Path, dict]] = []
    physical = plan["current"]["database"]["physical"]
    for name, identity in zip(
        ("vector_lake.db", "vector_lake.db-wal", "vector_lake.db-shm"),
        physical,
        strict=True,
    ):
        if identity.get("exists"):
            source_artifacts.append((name, Path(identity["path"]), identity))
    projection = {
        str(item.get("name")): item
        for item in plan["current"]["projection"].get("artifacts") or []
    }
    for name in _PROJECTION_ARTIFACTS:
        item = projection.get(name)
        identity = (item or {}).get("source_identity") or {}
        if identity.get("exists"):
            source = Path(str(item.get("source_path") or identity.get("path") or ""))
            expected_sha256 = str(item.get("sha256") or "")
            if expected_sha256 and not expected_sha256.startswith("sha256:"):
                expected_sha256 = "sha256:" + expected_sha256
            source_artifacts.append(
                (
                    name,
                    source,
                    {
                        **identity,
                        "path": str(source),
                        "sha256": expected_sha256,
                    },
                )
            )
    if plan["current"]["projection"].get("format_version") == 2:
        try:
            live_inventory = projection_v2_reachable_inventory()
        except (OSError, RuntimeError, UnicodeError, ValueError):
            live_inventory = None
        if (
            live_inventory is None
            and plan["current"]["projection"].get("status") == "captured"
            and not plan["current"]["projection"].get("issues")
        ):
            raise RuntimeError("Current projection v2 closure is unavailable")
        if live_inventory is None:
            source_artifacts.extend(
                _capture_projection_store_for_forward_recovery()
            )
        for item in (live_inventory or {}).get("objects", []):
            source = Path(item["path"])
            relative = (
                Path(".projection-store")
                / "objects"
                / "sha256"
                / source.parent.name
                / source.name
            ).as_posix()
            identity = _stable_file_identity(source)
            if (
                not identity.get("exists")
                or not identity.get("is_file")
                or identity.get("reparse")
                or identity.get("sha256") != "sha256:" + str(item["sha256"])
            ):
                raise RuntimeError("Current projection object changed during capture")
            source_artifacts.append((relative, source, identity))
    for item in plan["wiki_action"]["rebuild"]:
        filename = str(item["filename"])
        identity = item["source_identity"]
        artifact_name = (
            "wiki."
            + hashlib.sha256(filename.encode("utf-8")).hexdigest()
            + ".md"
        )
        source_artifacts.append(
            (artifact_name, Path(identity["path"]), identity)
        )
    estimated = sum(int(identity.get("size") or 0) for _, _, identity in source_artifacts)
    assert_backup_capacity(
        estimated_new_bytes=estimated,
        operation="snapshot-restore:forward-recovery",
        backup_roots=(
            peek_meta_dir() / "backups",
            Path(plan["database_path"]).parent / "schema-migration-backups",
            final_directory.parent,
        ),
        disk_anchor=final_directory.parent,
    )
    target, target_issues = _validate_maintenance_receipt(
        Path(plan["maintenance_receipt"]["path"])
    )
    if target is None or target_issues:
        raise RuntimeError("Snapshot restore target changed before capacity preflight")
    if plan["database_action"] != "preserve_verified_target_database":
        assert_backup_capacity(
            estimated_new_bytes=int(target["database"]["identity"].get("size") or 0),
            operation="snapshot-restore:database-stage",
            backup_roots=(
                peek_meta_dir() / "backups",
                Path(plan["database_path"]).parent / "schema-migration-backups",
                final_directory.parent,
            ),
            disk_anchor=Path(plan["database_path"]).parent,
        )
    missing_object_bytes = 0
    if target["projection"].get("format_version") == 2:
        for artifact in target["projection"]["object_artifacts"].values():
            source = Path(artifact["path"])
            destination = (
                get_wiki_dir()
                / ".projection-store"
                / "objects"
                / "sha256"
                / source.parent.name
                / source.name
            )
            if not destination.exists():
                missing_object_bytes += int(artifact["bytes"])
    static_bytes = sum(
        int(target["artifacts"][name]["bytes"])
        for name in _PROJECTION_ARTIFACTS
    )
    assert_backup_capacity(
        estimated_new_bytes=missing_object_bytes + static_bytes,
        operation="snapshot-restore:projection-stage",
        backup_roots=(
            peek_meta_dir() / "backups",
            Path(plan["database_path"]).parent / "schema-migration-backups",
            final_directory.parent,
        ),
        disk_anchor=get_wiki_dir(),
    )
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    stage = final_directory.with_name(
        f".{final_directory.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    stage.mkdir(parents=False, exist_ok=False)
    try:
        artifacts: list[dict] = []
        seen_artifact_names: set[str] = set()
        for name, source, expected_identity in source_artifacts:
            if name in seen_artifact_names:
                raise RuntimeError(f"Forward recovery duplicate artifact: {name}")
            seen_artifact_names.add(name)
            destination = stage / Path(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            sync_file(destination)
            copied_identity = _stable_file_identity(destination)
            if (
                copied_identity.get("size") != expected_identity.get("size")
                or not hmac.compare_digest(
                    str(copied_identity.get("sha256") or ""),
                    str(expected_identity.get("sha256") or ""),
                )
                or not _identity_matches(
                    _stable_file_identity(source), expected_identity
                )
            ):
                raise RuntimeError(f"Forward recovery source changed: {source}")
            artifacts.append(
                {
                    "name": name,
                    "source_path": str(source),
                    "sha256": copied_identity["sha256"],
                    "bytes": copied_identity["size"],
                }
            )
        current_wiki, wiki_issues = _wiki_identity()
        if wiki_issues or current_wiki != plan["source_binding"]["wiki_identity"]:
            raise RuntimeError("Wiki identity changed while forward recovery was created")
        manifest = {
            "contract": _FORWARD_CONTRACT_V2,
            "status": "complete",
            "created_at": _utc_now(),
            "operation_id": plan["operation_id"],
            "source_binding": plan["source_binding"],
            "artifacts": artifacts,
        }
        manifest["manifest_fingerprint"] = _fingerprint(manifest)
        for directory in sorted(
            (path for path in stage.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            sync_directory(directory)
        _atomic_json(stage / "manifest.json", manifest)
        sync_directory(stage)
        os.replace(stage, final_directory)
        sync_directory(final_directory.parent)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    binding = {
        "directory": str(final_directory),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
    }
    _manifest, issues = _validate_forward_bundle(
        binding, expected_source_binding=plan["source_binding"]
    )
    if issues:
        raise RuntimeError("Forward recovery bundle validation failed")
    return binding


def _pending_payload(plan: dict, forward: dict) -> dict:
    payload = {
        "contract": _RECEIPT_CONTRACT,
        "status": "pending",
        "created_at": _utc_now(),
        "operation_id": plan["operation_id"],
        "database_path": plan["database_path"],
        "plan_fingerprint": plan["fingerprint"],
        "target_receipt": {
            "path": plan["maintenance_receipt"]["path"],
            "file_sha256": plan["maintenance_receipt"]["file_sha256"],
        },
        "source_binding": plan["source_binding"],
        "forward_recovery": forward,
        "wiki_create": plan["wiki_action"]["create"],
        "wiki_rebuild": plan["wiki_action"]["rebuild"],
    }
    payload["receipt_fingerprint"] = _fingerprint(payload)
    return payload


def _capture_projection_store_for_forward_recovery() -> list[tuple[str, Path, dict]]:
    """Capture even an unreferencable v2 store for exact forward recovery."""
    object_root = ProjectionStoreV2(get_wiki_dir()).objects_dir
    if not object_root.exists():
        return []
    captured: list[tuple[str, Path, dict]] = []
    scanned = 0
    for current_root, directory_names, file_names in os.walk(
        object_root, followlinks=False
    ):
        current = Path(current_root)
        for name in directory_names:
            child = current / name
            details = child.lstat()
            if _is_reparse(child, details):
                raise RuntimeError("Current projection object store contains a reparse")
            if current == object_root:
                if re.fullmatch(r"[0-9a-f]{2}", name) is None:
                    raise RuntimeError("Current projection object directory is invalid")
            else:
                raise RuntimeError("Current projection object store has unknown depth")
        for name in file_names:
            scanned += 1
            if scanned > 200_000:
                raise RuntimeError("Current projection object file limit exceeded")
            source = current / name
            digest = source.stem
            if (
                source.suffix != ".json"
                or _HEX_SHA256.fullmatch(digest) is None
                or current.name != digest[:2]
            ):
                raise RuntimeError("Current projection object filename is invalid")
            identity = _stable_file_identity(source)
            if (
                not identity.get("is_file")
                or identity.get("reparse")
                or identity.get("unreadable")
            ):
                raise RuntimeError("Current projection object is not a plain file")
            relative = (
                Path(".projection-store")
                / "objects"
                / "sha256"
                / current.name
                / source.name
            ).as_posix()
            captured.append((relative, source, identity))
    return sorted(captured, key=lambda item: item[0])


def _stage_target_database(plan: dict, target: dict) -> Path:
    live = Path(plan["database_path"])
    stage = live.with_name(
        f".{live.name}.snapshot-restore.{plan['operation_id']}."
        f"{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    if stage.exists():
        raise RuntimeError("Snapshot database staging path already exists")
    try:
        shutil.copyfile(Path(target["database"]["path"]), stage)
        sync_file(stage)
        inspected = _inspect_database(stage)
        if (
            inspected["quick_check"] != "ok"
            or inspected.get("runtime_generations")
            != target["database"].get("runtime_generations")
            or not hmac.compare_digest(
                inspected.get("sha256", ""), target["database"]["sha256"]
            )
        ):
            raise RuntimeError("Snapshot database staging validation failed")
        return stage
    except BaseException:
        stage.unlink(missing_ok=True)
        Path(str(stage) + "-wal").unlink(missing_ok=True)
        Path(str(stage) + "-shm").unlink(missing_ok=True)
        raise


def _stage_target_projection(plan: dict, target: dict) -> dict[str, object]:
    live_paths = {
        path.name: path.resolve()
        for path in (
            get_index_path(),
            get_claim_graph_path(),
            get_projection_manifest_path(),
        )
    }
    staged: dict[str, object] = {}
    try:
        for name in _PROJECTION_ARTIFACTS:
            live = live_paths[name]
            stage = live.with_name(
                f".{live.name}.snapshot-restore.{plan['operation_id']}."
                f"{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            if stage.exists():
                raise RuntimeError("Snapshot projection staging path already exists")
            shutil.copyfile(Path(target["artifacts"][name]["path"]), stage)
            sync_file(stage)
            if not hmac.compare_digest(
                _sha256(stage), target["artifacts"][name]["sha256"]
            ):
                raise RuntimeError(f"Snapshot projection staging mismatch: {name}")
            staged[name] = stage
        if target["projection"].get("format_version") == 2:
            store = ProjectionStoreV2(get_wiki_dir())
            object_stages: list[dict[str, object]] = []
            for name, artifact in sorted(
                target["projection"]["object_artifacts"].items()
            ):
                digest = Path(name).stem
                if _HEX_SHA256.fullmatch(digest) is None:
                    raise RuntimeError("Snapshot projection object digest is invalid")
                destination = store.object_path(digest)
                expected_sha256 = str(artifact["sha256"])
                if destination.exists():
                    identity = _stable_file_identity(destination)
                    if (
                        not identity.get("is_file")
                        or identity.get("reparse")
                        or not hmac.compare_digest(
                            str(identity.get("sha256") or ""), expected_sha256
                        )
                    ):
                        raise RuntimeError(
                            f"Snapshot projection object collision: {digest}"
                        )
                    continue
                store._ensure_layout(destination.parent)
                temporary = destination.with_name(
                    f".{destination.name}.snapshot-restore.{plan['operation_id']}."
                    f"{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                object_stage = {
                    "temporary": temporary,
                    "destination": destination,
                    "sha256": expected_sha256,
                }
                object_stages.append(object_stage)
                staged["__objects__"] = object_stages
                shutil.copyfile(Path(artifact["path"]), temporary)
                sync_file(temporary)
                if not hmac.compare_digest(_sha256(temporary), expected_sha256):
                    raise RuntimeError(
                        f"Snapshot projection object staging mismatch: {digest}"
                    )
            staged["__format_version__"] = 2
            staged["__objects__"] = object_stages
        return staged
    except BaseException:
        _cleanup_projection_stages(staged)
        raise


def _cleanup_projection_stages(staged: dict[str, object]) -> None:
    for name in _PROJECTION_ARTIFACTS:
        path = staged.get(name)
        if isinstance(path, Path):
            path.unlink(missing_ok=True)
    for item in staged.get("__objects__") or []:
        if isinstance(item, dict) and isinstance(item.get("temporary"), Path):
            item["temporary"].unlink(missing_ok=True)


def _isolate_database_sidecars(plan: dict, pending: dict) -> None:
    forward, issues = _validate_forward_bundle(
        pending["forward_recovery"],
        expected_source_binding=pending["source_binding"],
    )
    if issues or forward is None:
        raise RuntimeError("Forward recovery bundle changed before sidecar isolation")
    forward_artifacts = {
        str(item["name"]): item for item in forward.get("artifacts") or []
    }
    physical = plan["current"]["database"]["physical"]
    for suffix, identity in zip(("-wal", "-shm"), physical[1:], strict=True):
        sidecar = Path(str(plan["database_path"]) + suffix)
        if _stable_file_identity(sidecar) != identity:
            raise RuntimeError("SQLite sidecar changed before isolation")
        if identity.get("exists"):
            artifact = forward_artifacts.get(f"vector_lake.db{suffix}")
            if (
                artifact is None
                or not hmac.compare_digest(
                    str(artifact.get("sha256") or ""),
                    str(identity.get("sha256") or ""),
                )
            ):
                raise RuntimeError("SQLite sidecar is absent from forward recovery")
            sidecar.unlink()
            sync_directory(sidecar.parent)


def _replace_database(plan: dict, stage: Path, pending: dict) -> None:
    _isolate_database_sidecars(plan, pending)
    expected = str(plan["current"]["database"].get("sha256") or "").removeprefix(
        "sha256:"
    )
    _replace_prepared_file_compare_and_swap(
        Path(plan["database_path"]), stage, expected
    )
    if stage.exists():
        stage.unlink()
    database_path = Path(plan["database_path"])
    commit_existing_file(database_path)


def _publish_projection(staged: dict[str, object]) -> None:
    live_paths = {
        path.name: path.resolve()
        for path in (
            get_index_path(),
            get_claim_graph_path(),
            get_projection_manifest_path(),
        )
    }
    if staged.get("__format_version__") == 2:
        for item in staged.get("__objects__") or []:
            temporary = item["temporary"]
            destination = item["destination"]
            expected_sha256 = str(item["sha256"])
            try:
                os.link(temporary, destination)
                sync_directory(destination.parent)
            except FileExistsError:
                identity = _stable_file_identity(destination)
                if (
                    not identity.get("is_file")
                    or identity.get("reparse")
                    or not hmac.compare_digest(
                        str(identity.get("sha256") or ""), expected_sha256
                    )
                ):
                    raise RuntimeError(
                        f"Snapshot projection object collision: {destination.name}"
                    )
            temporary.unlink(missing_ok=True)
        _checkpoint("after_projection_object_merge")
        for name in ("claim_graph.json", "index.json"):
            stage = staged[name]
            if not isinstance(stage, Path):
                raise RuntimeError("Snapshot projection locator stage is invalid")
            durable_replace_file(stage, live_paths[name])
        sidecar_stage = staged["projection_pair_manifest.json"]
        if not isinstance(sidecar_stage, Path):
            raise RuntimeError("Snapshot projection sidecar stage is invalid")
        durable_replace_file(
            sidecar_stage,
            live_paths["projection_pair_manifest.json"],
        )
        return
    manifest = live_paths["projection_pair_manifest.json"]
    manifest.unlink(missing_ok=True)
    sync_directory(manifest.parent)
    for name in ("claim_graph.json", "index.json", "projection_pair_manifest.json"):
        stage = staged[name]
        if not isinstance(stage, Path):
            raise RuntimeError("Snapshot projection stage is invalid")
        durable_replace_file(stage, live_paths[name])


def _wiki_contents(
    target: dict,
    canonical_database_path: Path,
    expected_actions: list[dict],
) -> dict[str, tuple[str, str, str]]:
    if not expected_actions:
        return {}
    identity, _issues = _wiki_identity()
    wiki, issues = _canonical_wiki_materialization(
        target,
        identity,
        canonical_database_path=canonical_database_path,
    )
    if issues:
        raise RuntimeError("Canonical Wiki materialization is no longer safe")
    observed_actions = {
        item["filename"]: {**item, "mode": mode}
        for mode in ("create", "rebuild")
        for item in wiki[mode]
    }
    wanted = {str(item["filename"]): item for item in expected_actions}
    if observed_actions != wanted:
        raise RuntimeError("Canonical Wiki restore set changed after snapshot restore")
    result: dict[str, tuple[str, str, str]] = {}
    connection = sqlite3.connect(
        f"{canonical_database_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT data_json FROM entities ORDER BY entity_id"
        ).fetchall()
    finally:
        connection.close()
    by_page: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        entity = json.loads(str(row["data_json"]))
        page_key = str(entity.get("page_key") or "")
        if page_key:
            by_page[page_key].append(entity)
    for filename, metadata in wanted.items():
        page_key = filename[:-3]
        entity = by_page[page_key][0]
        frontmatter = tool_projection._frontmatter_from_entity(entity)
        body = tool_projection._body_from_entity(entity, frontmatter)
        content = (
            "---\n"
            + dump_yaml(
                frontmatter,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
            + "---\n"
            + body
        )
        digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, metadata["sha256"]):
            raise RuntimeError(f"Canonical Wiki materialization changed: {filename}")
        result[filename] = (
            content,
            metadata["canonical_version"],
            str(metadata["mode"]),
        )
    return result


def _restore_wiki_actions(
    plan: dict, target: dict, expected_actions: list[dict]
) -> list[dict]:
    if not expected_actions:
        return []
    expected = {str(item["filename"]): item for item in expected_actions}
    contents = _wiki_contents(
        target, Path(plan["database_path"]), expected_actions
    )
    wiki_dir = get_wiki_dir().resolve()
    applied: list[dict] = []
    for filename in sorted(contents):
        content, canonical_version, mode = contents[filename]
        path = wiki_dir / filename
        if mode == "create":
            if path.exists() or path.is_symlink():
                raise RuntimeError(f"Refusing to overwrite Wiki file: {filename}")
        elif mode == "rebuild":
            source_identity = expected[filename].get("source_identity") or {}
            if not _identity_matches(_stable_file_identity(path), source_identity):
                raise RuntimeError(f"Canonical Wiki source changed: {filename}")
        else:
            raise RuntimeError(f"Unsupported Wiki restore action: {mode}")
        temporary = wiki_dir / (
            f".{filename}.{plan['operation_id']}.{os.getpid()}."
            f"{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                sync_open_file(handle)
            if not hmac.compare_digest(_sha256(temporary), expected[filename]["sha256"]):
                raise RuntimeError(f"Wiki restore staging mismatch: {filename}")
            if mode == "create":
                try:
                    os.link(temporary, path)
                except FileExistsError as exc:
                    raise RuntimeError(
                        f"Refusing to overwrite Wiki file: {filename}"
                    ) from exc
                commit_existing_file(path)
            else:
                if not _identity_matches(
                    _stable_file_identity(path),
                    expected[filename]["source_identity"],
                ):
                    raise RuntimeError(f"Canonical Wiki source changed: {filename}")
                durable_replace_file(temporary, path, source_synced=True)
        finally:
            temporary.unlink(missing_ok=True)
        applied.append(
            {
                "filename": filename,
                "mode": mode,
                "canonical_version": canonical_version,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return applied


def _verify_restored_target(
    target: dict,
    applied_wiki: list[dict],
    database_path: Path,
) -> dict:
    database_path = database_path.resolve()
    database = _inspect_database(database_path)
    physical = _database_physical_identity(database_path)
    database["physical"] = physical
    database["sha256"] = physical[0].get("sha256", "")
    if not _database_matches_target(database, target):
        raise RuntimeError("Restored database does not match maintenance snapshot")
    projection, issues = _current_projection()
    if issues or not _projection_matches_target(projection, target):
        raise RuntimeError("Restored projection pair does not match maintenance snapshot")
    for item in applied_wiki:
        path = get_wiki_dir() / item["filename"]
        if not path.is_file() or not hmac.compare_digest(
            _sha256(path), item["sha256"]
        ):
            raise RuntimeError(f"Restored Wiki file changed: {item['filename']}")
    identity, identity_issues = _wiki_identity()
    wiki, wiki_issues = _canonical_wiki_materialization(
        target,
        identity,
        canonical_database_path=database_path,
    )
    if identity_issues or wiki_issues or wiki["missing"] or wiki["rebuild"]:
        raise RuntimeError("Restored canonical Wiki coverage is incomplete")
    return {
        "database": {
            "sha256": database["sha256"],
            "quick_check": database["quick_check"],
            "runtime_generations": database["runtime_generations"],
            "sqlite_sidecars": {
                "-wal": physical[1].get("exists", False),
                "-shm": physical[2].get("exists", False),
            },
        },
        "projection": _projection_content_binding(projection),
        "wiki": {
            "missing": wiki["missing"],
            "created": [
                item for item in applied_wiki if item.get("mode") == "create"
            ],
            "rebuilt": [
                item for item in applied_wiki if item.get("mode") == "rebuild"
            ],
            "preserved_existing": wiki["preserved_existing"],
        },
    }


def _completed_payload(pending: dict, post: dict) -> dict:
    payload = dict(pending)
    payload.pop("receipt_fingerprint", None)
    payload.update({"status": "completed", "completed_at": _utc_now(), "post": post})
    payload["receipt_fingerprint"] = _fingerprint(payload)
    return payload


def restore_snapshot_maintenance(
    *,
    maintenance_receipt: str | Path,
    apply: bool = False,
    confirmation: str = "",
    confirm_no_writers: bool = False,
    db_path: str | Path | None = None,
) -> dict:
    """Preview or apply a receipt-bound, resumable full snapshot recovery."""
    initial = preview_restore_snapshot(maintenance_receipt, db_path)
    if not apply:
        return initial
    if not confirm_no_writers:
        raise RuntimeError("Snapshot restore requires --confirm-no-writers")
    if not hmac.compare_digest(str(confirmation), str(initial["fingerprint"])):
        raise RuntimeError(
            "Snapshot restore fingerprint mismatch; run a new read-only preview"
        )
    if not initial.get("can_apply"):
        raise RuntimeError(
            "Snapshot restore cannot apply: "
            + ", ".join(initial.get("issues") or ["unsupported_restore_state"])
        )

    database_path = Path(initial["database_path"])
    maintenance_lock = FileLock(
        str(database_path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )
    restore_lock = FileLock(
        str(database_path.parent / _RESTORE_LOCK_FILENAME), timeout=0
    )
    projection_lock = None
    database_stage: Path | None = None
    projection_stages: dict[str, object] = {}
    try:
        try:
            maintenance_lock.acquire()
            restore_lock.acquire()
        except FileLockTimeout as exc:
            raise RuntimeError("Snapshot restore maintenance lock is busy") from exc
        projection_lock = FileLock(str(get_index_path()) + ".lock", timeout=0)
        try:
            projection_lock.acquire()
        except FileLockTimeout as exc:
            raise RuntimeError("Snapshot restore projection lock is busy") from exc

        plan = preview_restore_snapshot(maintenance_receipt, database_path)
        if not hmac.compare_digest(str(confirmation), str(plan["fingerprint"])):
            raise RuntimeError(
                "Snapshot restore fingerprint mismatch after lock acquisition"
            )
        if not plan.get("can_apply"):
            raise RuntimeError(
                "Snapshot restore cannot apply: "
                + ", ".join(plan.get("issues") or ["unsupported_restore_state"])
            )
        with db_store._CONNECTIONS_LOCK:
            if db_store._CONNECTIONS:
                raise RuntimeError(
                    "Snapshot restore requires all in-process database connections to be closed"
                )
        if _probe_no_writers(database_path)["exclusive_probe"] == "busy":
            raise RuntimeError("Snapshot restore detected an active SQLite writer")

        target, target_issues = _validate_maintenance_receipt(
            Path(plan["maintenance_receipt"]["path"])
        )
        if target is None or target_issues:
            raise RuntimeError("Snapshot restore source receipt changed")
        completed_binding = plan.get("completed_restore_receipt")
        if plan["recovery_action"] == "already_completed":
            return {
                "contract": _RECEIPT_CONTRACT,
                "dry_run": False,
                "applied": False,
                "no_op": True,
                "recovery_action": "already_completed",
                "plan_fingerprint": plan["fingerprint"],
                "receipt_path": completed_binding["path"],
                "receipt_fingerprint": completed_binding["receipt"][
                    "receipt_fingerprint"
                ],
                "post": completed_binding["receipt"]["post"],
            }

        pending_binding = plan.get("pending_restore_receipt")
        if pending_binding is not None:
            pending_path = Path(pending_binding["path"])
            if not hmac.compare_digest(
                _sha256(pending_path), pending_binding["file_sha256"]
            ):
                raise RuntimeError("Snapshot restore pending receipt changed")
            pending, _identity = _read_json_stable(pending_path)
            if not _receipt_payload_valid(pending, "pending"):
                raise RuntimeError("Snapshot restore pending receipt is invalid")
            _manifest, forward_issues = _validate_forward_bundle(
                pending["forward_recovery"],
                expected_source_binding=pending["source_binding"],
            )
            if forward_issues:
                raise RuntimeError("Snapshot restore forward bundle changed")
            resumed = True
        else:
            forward = _create_forward_bundle(plan)
            pending = _pending_payload(plan, forward)
            pending_path = Path(plan["paths"]["pending_receipt_path"])
            resumed = False

        database_matches = _database_matches_target(plan["current"]["database"], target)
        projection_matches = _projection_matches_target(
            plan["current"]["projection"], target
        )
        if not database_matches:
            database_stage = _stage_target_database(plan, target)
        if not projection_matches:
            projection_stages = _stage_target_projection(plan, target)
        refreshed_target, refreshed_target_issues = _validate_maintenance_receipt(
            Path(plan["maintenance_receipt"]["path"])
        )
        if (
            refreshed_target is None
            or refreshed_target_issues
            or refreshed_target["file_sha256"]
            != plan["maintenance_receipt"]["file_sha256"]
        ):
            raise RuntimeError("Snapshot restore source changed before pending receipt")
        if pending_binding is None:
            _atomic_json(pending_path, pending)
            pending_identity = _stable_file_identity(pending_path)
            if not pending_identity.get("exists") or not _receipt_payload_valid(
                pending, "pending"
            ):
                raise RuntimeError("Snapshot restore pending receipt publication failed")

        if database_stage is not None:
            _replace_database(plan, database_stage, pending)
            database_stage = None
            _checkpoint("after_database_replace")
        if projection_stages:
            if database_matches:
                if _database_physical_identity(database_path) != plan["current"][
                    "database"
                ]["physical"]:
                    raise RuntimeError(
                        "Snapshot database changed before projection publication"
                    )
            else:
                restored_database = _inspect_database(database_path)
                restored_physical = _database_physical_identity(database_path)
                restored_database["physical"] = restored_physical
                restored_database["sha256"] = restored_physical[0].get(
                    "sha256", ""
                )
                if not _database_matches_target(restored_database, target):
                    raise RuntimeError(
                        "Snapshot database changed after atomic replacement"
                    )
            observed_projection, observed_projection_issues = _current_projection()
            if observed_projection_issues or _projection_content_binding(
                observed_projection
            ) != _projection_content_binding(plan["current"]["projection"]):
                raise RuntimeError("Snapshot projection changed before publication")
            _publish_projection(projection_stages)
            projection_stages = {}
            _checkpoint("after_projection_publish")
        _completed_before, wiki_remaining, wiki_valid = _receipt_wiki_state(pending)
        if not wiki_valid:
            raise RuntimeError("Snapshot restore Wiki receipt binding is invalid")
        applied_now = _restore_wiki_actions(plan, target, wiki_remaining)
        if applied_now:
            _checkpoint("after_wiki_publish")
        completed_wiki, wiki_remaining, wiki_valid = _receipt_wiki_state(pending)
        if not wiki_valid or wiki_remaining:
            raise RuntimeError("Snapshot restore Wiki receipt binding is incomplete")
        wiki_mutations = {item["filename"]: item for item in completed_wiki}
        expected_wiki = {
            str(item["filename"])
            for field in ("wiki_create", "wiki_rebuild")
            for item in pending.get(field, [])
        }
        if set(wiki_mutations) != expected_wiki:
            raise RuntimeError("Snapshot restore Wiki receipt set changed")
        post = _verify_restored_target(
            target, list(wiki_mutations.values()), database_path
        )
        completed = _completed_payload(pending, post)
        completed_path = Path(plan["paths"]["completed_receipt_path"])
        _atomic_json(completed_path, completed)
        db_store._INITIALIZED_DB_PATHS.discard(str(database_path.resolve()))
        return {
            "contract": _RECEIPT_CONTRACT,
            "dry_run": False,
            "applied": bool(
                not database_matches or not projection_matches or wiki_mutations
            ),
            "no_op": bool(
                database_matches and projection_matches and not wiki_mutations
            ),
            "recovery_action": "resumed_and_completed_restore"
            if resumed
            else "completed_restore",
            "plan_fingerprint": plan["fingerprint"],
            "forward_recovery": pending["forward_recovery"],
            "pending_receipt_path": str(pending_path),
            "receipt_path": str(completed_path),
            "receipt_fingerprint": completed["receipt_fingerprint"],
            "post": post,
        }
    finally:
        if database_stage is not None:
            database_stage.unlink(missing_ok=True)
            Path(str(database_stage) + "-wal").unlink(missing_ok=True)
            Path(str(database_stage) + "-shm").unlink(missing_ok=True)
        _cleanup_projection_stages(projection_stages)
        if projection_lock is not None:
            projection_lock.release()
        if restore_lock.is_locked:
            restore_lock.release()
        if maintenance_lock.is_locked:
            maintenance_lock.release()


__all__ = ["preview_restore_snapshot", "restore_snapshot_maintenance"]
