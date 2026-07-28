"""Preview-first lifecycle management for complete maintenance backups."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vector_lake.wiki_utils import peek_meta_dir


_REPARSE_POINT_ATTRIBUTE = 0x400
_DEFAULT_KEEP_LATEST = 5
_DEFAULT_MIN_AGE_DAYS = 30
_DEFAULT_STAGE_TTL_HOURS = 24
_MAX_PROJECTION_VALIDATION_BYTES = 256 * 1024 * 1024
_MAINTENANCE_ARTIFACT_NAMES = frozenset(
    {"vector_lake.db", "index.json", "claim_graph.json"}
)
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


class _RestorableGuardVerificationError(RuntimeError):
    """Raised when destructive retention can no longer prove its recovery guard."""


def _validate_options(
    *,
    keep_latest: int,
    min_age_days: int,
    stage_ttl_hours: int,
) -> dict[str, int]:
    raw_options = {
        "keep_latest": keep_latest,
        "min_age_days": min_age_days,
        "stage_ttl_hours": stage_ttl_hours,
    }
    for name, value in raw_options.items():
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        if value < 1:
            raise ValueError(f"{name} must be positive")
    return dict(raw_options)


def _is_reparse_stat(value: os.stat_result) -> bool:
    return bool(int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE)


def _is_link_or_reparse(path: Path, value: os.stat_result | None = None) -> bool:
    details = value if value is not None else path.lstat()
    return stat.S_ISLNK(details.st_mode) or _is_reparse_stat(details)


def _directory_identity(path: Path) -> tuple[int, int, int]:
    details = path.lstat()
    if _is_link_or_reparse(path, details) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"backup_root_is_not_a_plain_directory:{path}")
    return (int(details.st_dev), int(details.st_ino), int(details.st_mode))


def _assert_direct_child(root: Path, path: Path) -> None:
    if path.parent != root or path.name in {"", ".", ".."}:
        raise RuntimeError(f"backup_path_outside_root:{path}")


def _tree_size_no_follow(path: Path) -> int:
    details = path.lstat()
    if _is_link_or_reparse(path, details):
        raise RuntimeError(f"backup_tree_contains_link_or_reparse:{path}")
    if stat.S_ISREG(details.st_mode):
        return int(details.st_size)
    if not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"backup_tree_contains_special_entry:{path}")

    size = 0
    with os.scandir(path) as entries:
        for entry in entries:
            size += _tree_size_no_follow(path / entry.name)
    return size


def _stable_stat_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _plain_regular_file_stat(path: Path) -> os.stat_result:
    details = path.lstat()
    if _is_link_or_reparse(path, details) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError(f"backup_artifact_is_not_a_plain_file:{path}")
    return details


def _read_plain_file_bytes(path: Path, *, max_bytes: int) -> bytes:
    before = _plain_regular_file_stat(path)
    if int(before.st_size) > max_bytes:
        raise RuntimeError(f"backup_manifest_too_large:{path}")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"backup_file_changed_before_read:{path}")
        raw = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    current = _plain_regular_file_stat(path)
    if len(raw) > max_bytes:
        raise RuntimeError(f"backup_manifest_too_large:{path}")
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"backup_file_changed_while_reading:{path}")
    return raw


def _sha256_plain_file(path: Path) -> str:
    before = _plain_regular_file_stat(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"backup_file_changed_before_hash:{path}")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    current = _plain_regular_file_stat(path)
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"backup_file_changed_while_hashing:{path}")
    return digest.hexdigest()


def _valid_created_at(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _created_at_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("backup created_at must include a timezone")
    return int(parsed.timestamp() * 1_000_000_000)


def _valid_runtime_generations(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(surface, str)
        and bool(surface)
        and type(generation) is int
        and generation >= 0
        for surface, generation in value.items()
    )


def _valid_consistency(
    value: Any,
    *,
    projection_present: bool,
    restorable: bool,
) -> bool:
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    covered = value.get("covered_surfaces")
    allowed_statuses = (
        {"verified", "unverifiable"} if projection_present else {"not_applicable"}
    )
    return (
        status in allowed_statuses
        and isinstance(value.get("reason"), str)
        and bool(value["reason"])
        and value.get("verification_scope") == "tracked-canonical-projection-surfaces"
        and isinstance(covered, list)
        and all(isinstance(surface, str) and bool(surface) for surface in covered)
        and len(covered) == len(set(covered))
        and restorable is (status == "verified")
    )


def _read_complete_manifest(path: Path) -> tuple[dict[str, Any], str] | None:
    manifest_path = path / "manifest.json"
    try:
        raw = _read_plain_file_bytes(manifest_path, max_bytes=1024 * 1024)
        manifest = json.loads(raw)
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _MAINTENANCE_MANIFEST_KEYS
        or manifest.get("manifest_version") != 3
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("label"), str)
        or not manifest["label"].strip()
        or not _valid_created_at(manifest.get("created_at"))
        or not isinstance(manifest.get("copied"), list)
        or not isinstance(manifest.get("artifact_sha256"), dict)
        or not isinstance(manifest.get("canonical_projection_consistency"), dict)
        or type(manifest.get("restorable_as_consistent_canonical_projection_snapshot"))
        is not bool
    ):
        return None
    copied = manifest["copied"]
    artifact_sha256 = manifest["artifact_sha256"]
    copied_names = set(copied)
    projection_names = {"index.json", "claim_graph.json"}
    projection_present = projection_names.issubset(copied_names)
    database_generations = manifest["database_runtime_generations"]
    database_error = manifest["database_runtime_generation_error"]
    projection_generation = manifest["projection_generation"]
    projection_binding = manifest["projection_canonical_generation"]
    restorable = manifest["restorable_as_consistent_canonical_projection_snapshot"]
    database_identity_valid = (
        _valid_runtime_generations(database_generations) and database_error is None
    ) or (
        database_generations is None
        and isinstance(database_error, str)
        and bool(database_error)
    )
    projection_identity_valid = (
        projection_present
        and isinstance(projection_generation, str)
        and bool(projection_generation)
        and isinstance(projection_binding, dict)
        and projection_binding.get("status") in {"verified", "unverifiable"}
    ) or (
        not projection_present
        and projection_generation is None
        and projection_binding is None
    )
    if (
        not copied
        or any(
            not isinstance(name, str) or not name or Path(name).name != name
            for name in copied
        )
        or len(copied) != len(copied_names)
        or "vector_lake.db" not in copied_names
        or not copied_names.issubset(_MAINTENANCE_ARTIFACT_NAMES)
        or bool(copied_names & projection_names) != projection_present
        or not database_identity_valid
        or not projection_identity_valid
        or not _valid_consistency(
            manifest["canonical_projection_consistency"],
            projection_present=projection_present,
            restorable=restorable,
        )
        or any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for name, value in artifact_sha256.items()
        )
        or copied_names != set(artifact_sha256)
    ):
        return None
    try:
        actual_names = {entry.name for entry in path.iterdir()}
        if actual_names != copied_names | {"manifest.json"}:
            return None
        for name in copied:
            _plain_regular_file_stat(path / name)
    except (OSError, RuntimeError):
        return None
    return manifest, hashlib.sha256(raw).hexdigest()


def _verify_complete_backup_artifacts(path: Path, manifest: dict[str, Any]) -> None:
    for name in manifest["copied"]:
        expected = manifest["artifact_sha256"][name]
        actual = _sha256_plain_file(path / name)
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError(f"backup_artifact_hash_mismatch:{path / name}")


def _read_projection_contract_stub(
    path: Path,
    *,
    manifest_key: str,
) -> dict[str, Any] | None:
    """Decode a projection fully while retaining only its pair contract."""
    projection_data = json.loads(
        _read_plain_file_bytes(
            path,
            max_bytes=_MAX_PROJECTION_VALIDATION_BYTES,
        )
    )
    if not isinstance(projection_data, dict):
        return None
    if manifest_key not in projection_data:
        return {}
    return {manifest_key: projection_data[manifest_key]}


def _verify_restorable_backup_snapshot(
    path: Path,
    manifest: dict[str, Any],
) -> None:
    """Prove that a claimed restorable backup is internally consistent."""
    from vector_lake import db_store, indexer

    if (
        manifest.get("restorable_as_consistent_canonical_projection_snapshot")
        is not True
        or manifest.get("database_runtime_generation_error") is not None
        or manifest.get("canonical_projection_consistency", {}).get("status")
        != "verified"
        or set(manifest.get("copied", ()))
        != {"vector_lake.db", "index.json", "claim_graph.json"}
    ):
        raise RuntimeError(f"backup_restorable_claim_is_incomplete:{path}")

    _verify_complete_backup_artifacts(path, manifest)
    database_path = path / "vector_lake.db"
    connection = None
    try:
        connection = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
        connection.execute("PRAGMA query_only = ON")
        db_store._load_sqlite_vec_extension(connection)
        quick_check = [
            str(row[0]) for row in connection.execute("PRAGMA quick_check(1)")
        ]
        if quick_check != ["ok"]:
            raise RuntimeError(
                "backup_database_quick_check_failed:" + "|".join(quick_check)
            )
        schema_state = db_store.inspect_schema_migration_connection(
            connection,
            database_path,
        )
        if schema_state.get("ready") is not True:
            detail = "|".join(schema_state.get("issues") or ()) or str(
                schema_state.get("status")
            )
            raise RuntimeError(f"backup_schema_invalid:{detail}")
        rows = connection.execute(
            "SELECT surface, generation FROM runtime_generations ORDER BY surface"
        ).fetchall()
        database_generations = {
            str(surface): int(generation) for surface, generation in rows
        }
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("backup_"):
            raise
        raise RuntimeError(f"backup_database_verification_failed:{path}:{exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    if database_generations != manifest.get("database_runtime_generations"):
        raise RuntimeError(f"backup_database_generation_mismatch:{path}")

    try:
        index_contract = _read_projection_contract_stub(
            path / "index.json",
            manifest_key=indexer.PROJECTION_MANIFEST_KEY,
        )
        claim_graph_contract = _read_projection_contract_stub(
            path / "claim_graph.json",
            manifest_key=indexer.PROJECTION_MANIFEST_KEY,
        )
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"backup_projection_read_failed:{path}:{exc}") from exc
    if index_contract is None or claim_graph_contract is None:
        raise RuntimeError(f"backup_projection_payload_invalid:{path}")

    try:
        projection_generation = indexer.validate_projection_pair(
            index_contract,
            claim_graph_contract,
        )
        projection_binding = indexer.projection_canonical_generation(
            index_contract,
            claim_graph_contract,
        )
    except Exception as exc:
        raise RuntimeError(f"backup_projection_contract_invalid:{path}:{exc}") from exc
    if projection_generation != manifest.get("projection_generation"):
        raise RuntimeError(f"backup_projection_generation_mismatch:{path}")
    if projection_binding != manifest.get("projection_canonical_generation"):
        raise RuntimeError(f"backup_projection_binding_mismatch:{path}")
    if projection_binding.get("status") != "verified":
        raise RuntimeError(f"backup_projection_binding_unverified:{path}")

    covered_surfaces = list(indexer.CANONICAL_PROJECTION_SURFACES)
    database_projection_generations = {
        surface: database_generations.get(surface) for surface in covered_surfaces
    }
    if any(value is None for value in database_projection_generations.values()):
        raise RuntimeError(f"backup_database_generation_coverage_incomplete:{path}")
    if database_projection_generations != projection_binding["runtime_generations"]:
        raise RuntimeError(f"backup_projection_database_generation_mismatch:{path}")

    expected_consistency = {
        "status": "verified",
        "reason": "runtime-generations-match",
        "verification_scope": "tracked-canonical-projection-surfaces",
        "covered_surfaces": covered_surfaces,
        "canonical_generation_token": projection_binding["token"],
        "projection_runtime_generations": projection_binding["runtime_generations"],
        "database_runtime_generations": database_projection_generations,
    }
    if manifest.get("canonical_projection_consistency") != expected_consistency:
        raise RuntimeError(f"backup_consistency_manifest_mismatch:{path}")


def _is_private_maintenance_stage(name: str) -> bool:
    if not name.startswith(".") or not name.endswith(".tmp"):
        return False
    try:
        prefix, token, suffix = name.rsplit(".", 2)
    except ValueError:
        return False
    _label, separator, stamp = prefix[1:].rpartition("_")
    valid_stamp = (
        bool(separator)
        and len(stamp) == 22
        and stamp[8:9] == "T"
        and stamp[-1:] == "Z"
        and (stamp[:8] + stamp[9:-1]).isdigit()
    )
    return (
        suffix == "tmp"
        and valid_stamp
        and len(token) == 32
        and all(character in "0123456789abcdef" for character in token)
    )


def _retention_tombstone_created_ns(name: str) -> int | None:
    prefix = ".retention-"
    suffix = ".tombstone"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    identity = name[len(prefix) : -len(suffix)]
    stamp, separator, token = identity.partition("-")
    if (
        not separator
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
    ):
        return None
    try:
        created = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return int(created.timestamp() * 1_000_000_000)


def _is_retention_tombstone(name: str) -> bool:
    prefix = ".retention-"
    suffix = ".tombstone"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    identity = name[len(prefix) : -len(suffix)]
    legacy = len(identity) == 32 and all(
        character in "0123456789abcdef" for character in identity
    )
    return legacy or _retention_tombstone_created_ns(name) is not None


def _new_retention_tombstone_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f".retention-{stamp}-{uuid.uuid4().hex}.tombstone"


def _record_directory(
    root: Path,
    path: Path,
    *,
    candidate_type: str,
    manifest_sha256: str | None,
    created_at_ns: int | None = None,
    consistency_status: str | None = None,
    restorable: bool | None = None,
) -> dict[str, Any]:
    _assert_direct_child(root, path)
    details = path.lstat()
    if _is_link_or_reparse(path, details) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"backup_candidate_is_not_a_plain_directory:{path}")
    return {
        "name": path.name,
        "type": candidate_type,
        "size": _tree_size_no_follow(path),
        "mtime_ns": int(details.st_mtime_ns),
        "manifest_sha256": manifest_sha256,
        "created_at_ns": created_at_ns,
        "consistency_status": consistency_status,
        "restorable": restorable,
    }


def _fingerprint(
    candidates: list[dict[str, Any]],
    *,
    restorable_guard: dict[str, Any] | None = None,
    restorable_verification_failures: list[dict[str, Any]] | None = None,
) -> str:
    def normalized(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": item["name"],
            "type": item["type"],
            "size": item["size"],
            "mtime_ns": item["mtime_ns"],
            "manifest_sha256": item["manifest_sha256"],
            "created_at_ns": item["created_at_ns"],
            "consistency_status": item["consistency_status"],
            "restorable": item["restorable"],
            **(
                {"verification_error": item["verification_error"]}
                if "verification_error" in item
                else {}
            ),
        }

    payload = {
        "candidates": [
            normalized(item)
            for item in sorted(candidates, key=lambda value: value["name"])
        ],
        "restorable_guard": (
            normalized(restorable_guard) if restorable_guard is not None else None
        ),
        "restorable_verification_failures": [
            normalized(item)
            for item in sorted(
                restorable_verification_failures or [],
                key=lambda value: value["name"],
            )
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _empty_plan(
    *,
    root: Path,
    options: dict[str, int],
    root_state: str,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    restorable_verification_failures: list[dict[str, Any]] = []
    return {
        "backup_root": str(root),
        "root_state": root_state,
        "options": options,
        "candidates": candidates,
        "candidate_count": 0,
        "candidate_bytes": 0,
        "fingerprint": _fingerprint(
            candidates,
            restorable_guard=None,
            restorable_verification_failures=restorable_verification_failures,
        ),
        "restorable_guard": None,
        "restorable_verification_failures": restorable_verification_failures,
        "protected": [],
        "ignored": [],
    }


def _scan_retention_plan(
    *,
    keep_latest: int,
    min_age_days: int,
    stage_ttl_hours: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    options = _validate_options(
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        stage_ttl_hours=stage_ttl_hours,
    )
    root = peek_meta_dir() / "backups"
    if not root.exists():
        return _empty_plan(root=root, options=options, root_state="absent")
    _directory_identity(root)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_ns = int(current.timestamp() * 1_000_000_000)
    backup_cutoff_ns = int(
        (current - timedelta(days=options["min_age_days"])).timestamp() * 1_000_000_000
    )
    stage_cutoff_ns = int(
        (current - timedelta(hours=options["stage_ttl_hours"])).timestamp()
        * 1_000_000_000
    )

    complete: list[dict[str, Any]] = []
    complete_manifests: dict[str, dict[str, Any]] = {}
    stages: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []
    for path in sorted(root.iterdir(), key=lambda value: value.name):
        _assert_direct_child(root, path)
        try:
            details = path.lstat()
        except OSError as exc:
            ignored.append({"name": path.name, "reason": f"stat_failed:{exc}"})
            continue
        if _is_link_or_reparse(path, details):
            ignored.append({"name": path.name, "reason": "link_or_reparse"})
            continue
        if not stat.S_ISDIR(details.st_mode):
            ignored.append({"name": path.name, "reason": "not_directory"})
            continue

        if _is_retention_tombstone(path.name):
            tombstone_created_ns = _retention_tombstone_created_ns(path.name)
            tombstone_age_ns = (
                tombstone_created_ns
                if tombstone_created_ns is not None
                else int(details.st_mtime_ns)
            )
            if tombstone_age_ns > stage_cutoff_ns:
                ignored.append(
                    {"name": path.name, "reason": "retention_tombstone_not_expired"}
                )
                continue
            manifest = _read_complete_manifest(path)
            try:
                stages.append(
                    _record_directory(
                        root,
                        path,
                        candidate_type="expired_retention_tombstone",
                        manifest_sha256=manifest[1] if manifest is not None else None,
                        created_at_ns=(
                            _created_at_ns(manifest[0]["created_at"])
                            if manifest is not None
                            else None
                        ),
                        consistency_status=(
                            manifest[0]["canonical_projection_consistency"]["status"]
                            if manifest is not None
                            else None
                        ),
                        restorable=(
                            manifest[0][
                                "restorable_as_consistent_canonical_projection_snapshot"
                            ]
                            if manifest is not None
                            else None
                        ),
                    )
                )
                if manifest is not None:
                    complete_manifests[path.name] = manifest[0]
            except (OSError, RuntimeError) as exc:
                ignored.append({"name": path.name, "reason": str(exc)})
            continue

        is_private_stage = _is_private_maintenance_stage(path.name)
        if is_private_stage:
            if int(details.st_mtime_ns) > stage_cutoff_ns:
                ignored.append({"name": path.name, "reason": "staging_not_expired"})
                continue
            try:
                stages.append(
                    _record_directory(
                        root,
                        path,
                        candidate_type="expired_staging",
                        manifest_sha256=None,
                    )
                )
            except (OSError, RuntimeError) as exc:
                ignored.append({"name": path.name, "reason": str(exc)})
            continue

        manifest = _read_complete_manifest(path)
        if manifest is None:
            ignored.append({"name": path.name, "reason": "not_complete_backup"})
            continue
        try:
            record = _record_directory(
                root,
                path,
                candidate_type="complete_backup",
                manifest_sha256=manifest[1],
                created_at_ns=_created_at_ns(manifest[0]["created_at"]),
                consistency_status=manifest[0]["canonical_projection_consistency"][
                    "status"
                ],
                restorable=manifest[0][
                    "restorable_as_consistent_canonical_projection_snapshot"
                ],
            )
            complete.append(record)
            complete_manifests[path.name] = manifest[0]
        except (OSError, RuntimeError) as exc:
            ignored.append({"name": path.name, "reason": str(exc)})

    newest_names = {
        item["name"]
        for item in sorted(
            complete,
            key=lambda value: (value["created_at_ns"], value["name"]),
            reverse=True,
        )[: options["keep_latest"]]
    }
    claimed_restorable = sorted(
        (
            item
            for item in [*complete, *stages]
            if item["restorable"] is True and item["name"] in complete_manifests
        ),
        key=lambda value: (value["created_at_ns"], value["name"]),
        reverse=True,
    )
    restorable_guard: dict[str, Any] | None = None
    restorable_verification_failures: list[dict[str, Any]] = []
    for item in claimed_restorable:
        try:
            _verify_restorable_backup_snapshot(
                root / item["name"],
                complete_manifests[item["name"]],
            )
        except Exception as exc:
            restorable_verification_failures.append(
                {
                    **item,
                    "verification_error": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
        restorable_guard = item
        break

    failed_by_name = {item["name"]: item for item in restorable_verification_failures}
    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for item in [*stages, *complete]:
        reasons = []
        is_complete_backup = item["type"] == "complete_backup"
        if is_complete_backup and item["name"] in newest_names:
            reasons.append("keep_latest")
        if restorable_guard is not None and item["name"] == restorable_guard["name"]:
            reasons.append("latest_restorable")
        if item["name"] in failed_by_name:
            reasons.append("restorable_verification_failed")
        if is_complete_backup and int(item["created_at_ns"]) > backup_cutoff_ns:
            reasons.append("minimum_age")
        if reasons:
            protected_item = {
                "name": item["name"],
                "reason": ",".join(reasons),
                "consistency_status": item["consistency_status"],
                "restorable": item["restorable"],
            }
            if item["name"] in failed_by_name:
                protected_item["verification_error"] = failed_by_name[item["name"]][
                    "verification_error"
                ]
            protected.append(protected_item)
        else:
            candidates.append(item)

    candidates.sort(key=lambda value: (value["type"], value["name"]))
    fingerprint = _fingerprint(
        candidates,
        restorable_guard=restorable_guard,
        restorable_verification_failures=restorable_verification_failures,
    )
    return {
        "backup_root": str(root),
        "root_state": "ready",
        "observed_at_ns": current_ns,
        "options": options,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(item["size"]) for item in candidates),
        "fingerprint": fingerprint,
        "restorable_guard": restorable_guard,
        "restorable_verification_failures": sorted(
            restorable_verification_failures,
            key=lambda value: value["name"],
        ),
        "protected": sorted(protected, key=lambda value: value["name"]),
        "ignored": ignored,
    }


def _record_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(
        expected[key] == actual[key]
        for key in (
            "type",
            "size",
            "mtime_ns",
            "manifest_sha256",
            "created_at_ns",
            "consistency_status",
            "restorable",
        )
    )


def _revalidate_candidate(
    root: Path,
    item: dict[str, Any],
    *,
    verify_artifacts: bool = False,
) -> dict[str, Any]:
    path = root / item["name"]
    manifest_sha256 = None
    created_at_ns = None
    consistency_status = None
    restorable = None
    if item["type"] == "complete_backup":
        manifest = _read_complete_manifest(path)
        if manifest is None:
            raise RuntimeError(f"backup_candidate_no_longer_complete:{path}")
        if verify_artifacts:
            _verify_complete_backup_artifacts(path, manifest[0])
        manifest_sha256 = manifest[1]
        created_at_ns = _created_at_ns(manifest[0]["created_at"])
        consistency_status = manifest[0]["canonical_projection_consistency"]["status"]
        restorable = manifest[0][
            "restorable_as_consistent_canonical_projection_snapshot"
        ]
    elif item["type"] == "expired_retention_tombstone":
        manifest = _read_complete_manifest(path)
        if manifest is not None:
            if verify_artifacts:
                _verify_complete_backup_artifacts(path, manifest[0])
            manifest_sha256 = manifest[1]
            created_at_ns = _created_at_ns(manifest[0]["created_at"])
            consistency_status = manifest[0]["canonical_projection_consistency"][
                "status"
            ]
            restorable = manifest[0][
                "restorable_as_consistent_canonical_projection_snapshot"
            ]
    elif item["type"] != "expired_staging":
        raise RuntimeError(f"unknown_backup_candidate_type:{item['type']}")
    actual = _record_directory(
        root,
        path,
        candidate_type=item["type"],
        manifest_sha256=manifest_sha256,
        created_at_ns=created_at_ns,
        consistency_status=consistency_status,
        restorable=restorable,
    )
    if actual != item:
        raise RuntimeError(f"backup_candidate_changed:{path}")
    return actual


def _revalidate_restorable_guard(
    root: Path,
    item: dict[str, Any] | None,
) -> None:
    if item is None:
        return
    path = root / item["name"]
    try:
        _revalidate_candidate(root, item)
        manifest = _read_complete_manifest(path)
        if manifest is None:
            raise RuntimeError(f"backup_guard_no_longer_complete:{path}")
        if not hmac.compare_digest(manifest[1], item["manifest_sha256"]):
            raise RuntimeError(f"backup_guard_manifest_changed:{path}")
        _verify_complete_backup_artifacts(path, manifest[0])
    except Exception as exc:
        raise _RestorableGuardVerificationError(
            f"restorable_guard_verification_failed:{path}:{exc}"
        ) from exc


def _verify_renamed_candidate_artifacts(
    path: Path,
    item: dict[str, Any],
) -> None:
    verify_complete = item["type"] == "complete_backup" or (
        item["type"] == "expired_retention_tombstone"
        and item["manifest_sha256"] is not None
    )
    if not verify_complete:
        return
    manifest = _read_complete_manifest(path)
    if manifest is None:
        raise RuntimeError(f"renamed_backup_no_longer_complete:{path}")
    if not hmac.compare_digest(manifest[1], item["manifest_sha256"]):
        raise RuntimeError(f"renamed_backup_manifest_changed:{path}")
    _verify_complete_backup_artifacts(path, manifest[0])


def _remove_tree_no_follow(path: Path) -> None:
    details = path.lstat()
    if _is_link_or_reparse(path, details):
        raise RuntimeError(f"refusing_to_delete_link_or_reparse:{path}")
    if not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"refusing_to_delete_non_directory:{path}")
    with os.scandir(path) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        child = path / name
        child_details = child.lstat()
        if _is_link_or_reparse(child, child_details):
            raise RuntimeError(f"refusing_to_delete_link_or_reparse:{child}")
        if stat.S_ISDIR(child_details.st_mode):
            _remove_tree_no_follow(child)
        elif stat.S_ISREG(child_details.st_mode):
            child.unlink()
        else:
            raise RuntimeError(f"refusing_to_delete_special_entry:{child}")
    path.rmdir()


def _apply_retention_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not plan["candidates"]:
        return {
            "applied": True,
            "deleted": [],
            "deleted_count": 0,
            "failed": [],
            "failed_count": 0,
        }
    root = Path(plan["backup_root"])
    root_identity = _directory_identity(root)
    restorable_guard = plan.get("restorable_guard")
    for item in plan["candidates"]:
        if _directory_identity(root) != root_identity:
            raise RuntimeError("backup_root_identity_changed")
        _revalidate_candidate(root, item)

    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    for item in plan["candidates"]:
        if _directory_identity(root) != root_identity:
            failures.append(
                {
                    "name": item["name"],
                    "reason": "backup_root_identity_changed",
                }
            )
            break
        source = root / item["name"]
        existing_tombstone = item["type"] == "expired_retention_tombstone"
        tombstone = (
            source if existing_tombstone else root / _new_retention_tombstone_name()
        )
        _assert_direct_child(root, source)
        _assert_direct_child(root, tombstone)
        try:
            _revalidate_candidate(
                root,
                item,
                verify_artifacts=item["type"] == "complete_backup"
                or (existing_tombstone and item["manifest_sha256"] is not None),
            )
            _revalidate_restorable_guard(root, restorable_guard)
            if not existing_tombstone:
                source.rename(tombstone)
            manifest_sha256 = None
            if item["type"] == "complete_backup" or (
                existing_tombstone and item["manifest_sha256"] is not None
            ):
                manifest = _read_complete_manifest(tombstone)
                if manifest is None:
                    raise RuntimeError("renamed_backup_no_longer_complete")
                manifest_sha256 = manifest[1]
                created_at_ns = _created_at_ns(manifest[0]["created_at"])
                consistency_status = manifest[0]["canonical_projection_consistency"][
                    "status"
                ]
                restorable = manifest[0][
                    "restorable_as_consistent_canonical_projection_snapshot"
                ]
            else:
                created_at_ns = None
                consistency_status = None
                restorable = None
            renamed = _record_directory(
                root,
                tombstone,
                candidate_type=item["type"],
                manifest_sha256=manifest_sha256,
                created_at_ns=created_at_ns,
                consistency_status=consistency_status,
                restorable=restorable,
            )
            if not _record_matches(item, renamed):
                raise RuntimeError("renamed_backup_identity_changed")
            _revalidate_restorable_guard(root, restorable_guard)
            _verify_renamed_candidate_artifacts(tombstone, item)
            _remove_tree_no_follow(tombstone)
            deleted.append(item["name"])
        except _RestorableGuardVerificationError:
            raise
        except (OSError, RuntimeError) as exc:
            failure = {"name": item["name"], "reason": str(exc)}
            if tombstone.exists() or tombstone.is_symlink():
                failure["tombstone"] = tombstone.name
            failures.append(failure)

    return {
        "applied": True,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "failed": failures,
        "failed_count": len(failures),
    }


def backup_retention_maintenance(
    dry_run: bool = True,
    keep_latest: int = _DEFAULT_KEEP_LATEST,
    min_age_days: int = _DEFAULT_MIN_AGE_DAYS,
    stage_ttl_hours: int = _DEFAULT_STAGE_TTL_HOURS,
    confirmation: str = "",
) -> str:
    """Preview or apply retention while preserving the newest restorable backup."""
    plan = _scan_retention_plan(
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        stage_ttl_hours=stage_ttl_hours,
    )
    result = {"dry_run": bool(dry_run), **plan}
    if dry_run:
        result["applied"] = False
        return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)

    if not confirmation or confirmation != plan["fingerprint"]:
        raise ValueError(
            "confirmation must exactly match the current dry-run fingerprint; "
            "no changes made"
        )

    current = _scan_retention_plan(
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        stage_ttl_hours=stage_ttl_hours,
    )
    if current["fingerprint"] != confirmation:
        raise RuntimeError(
            "backup retention candidates changed after confirmation; no changes made"
        )
    result = {"dry_run": False, **current, **_apply_retention_plan(current)}
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
