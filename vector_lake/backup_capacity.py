"""Read-only backup quota telemetry and fail-closed creation preflight."""

from __future__ import annotations

import math
import hashlib
import hmac
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Iterable

from vector_lake.projection_format_v2 import (
    CLAIM_GRAPH_FILENAME,
    INDEX_FILENAME,
    SIDECAR_FILENAME,
    is_v2_locator,
    validate_locator,
    validate_root_closure,
    validate_sidecar,
)
from vector_lake.projection_store_v2 import canonical_json_bytes
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_index_path,
    get_projection_manifest_path,
    get_wiki_dir,
    peek_meta_dir,
)


_DEFAULT_MIN_FREE_BYTES = 10 * 1024**3
_DEFAULT_MIN_FREE_RATIO = 0.10
_DEFAULT_ESTIMATE_HEADROOM_RATIO = 0.05
_MAX_INVENTORY_ENTRIES = 100_000
_REPARSE_POINT_ATTRIBUTE = 0x400
_MAX_PROJECTION_OBJECTS = 131_072
_MAX_PROJECTION_SIDECAR_BYTES = 64 * 1024
LEGACY_PROJECTION_FILE_MAX_BYTES = 128 * 1024 * 1024


class BackupCapacityError(RuntimeError):
    """A backup must not start because capacity cannot be proved safe."""

    def __init__(self, reason: str, status: dict):
        self.reason = str(reason)
        self.status = dict(status)
        super().__init__(f"backup_capacity_preflight_failed:{self.reason}")


def peek_db_path() -> Path:
    """Resolve the database path without importing db_store or creating state."""
    override = str(os.environ.get("VECTOR_LAKE_DB_PATH", "")).strip()
    return Path(override) if override else peek_meta_dir() / "vector_lake.db"


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def backup_capacity_policy() -> dict:
    """Return the effective quota policy without mutating filesystem state."""
    raw_mode = str(os.environ.get("VECTOR_LAKE_BACKUP_QUOTA_MODE", "enforce"))
    quota_mode = raw_mode.strip().lower()
    if quota_mode not in {"enforce", "report"}:
        quota_mode = "enforce"
    return {
        "max_total_bytes": _bounded_env_int(
            "VECTOR_LAKE_BACKUP_MAX_TOTAL_BYTES",
            0,
            minimum=0,
            maximum=2**63 - 1,
        ),
        "min_free_bytes": _bounded_env_int(
            "VECTOR_LAKE_BACKUP_MIN_FREE_BYTES",
            _DEFAULT_MIN_FREE_BYTES,
            minimum=0,
            maximum=2**63 - 1,
        ),
        "min_free_ratio": _bounded_env_float(
            "VECTOR_LAKE_BACKUP_MIN_FREE_RATIO",
            _DEFAULT_MIN_FREE_RATIO,
            minimum=0.0,
            maximum=0.95,
        ),
        "quota_mode": quota_mode,
    }


def _is_link_or_reparse(path: Path, details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or bool(
        int(getattr(details, "st_file_attributes", 0))
        & _REPARSE_POINT_ATTRIBUTE
    )


def _read_plain_json(path: Path, *, max_bytes: int) -> tuple[dict, os.stat_result]:
    """Read one bounded plain JSON file without following redirecting objects."""
    details = path.lstat()
    if _is_link_or_reparse(path, details) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"projection_path_not_plain_file:{path.name}")
    if int(details.st_size) > max_bytes:
        raise ValueError(f"projection_file_too_large:{path.name}")
    payload = path.read_bytes()
    if len(payload) != int(details.st_size):
        raise ValueError(f"projection_file_changed:{path.name}")
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"projection_json_object_required:{path.name}")
    after = path.lstat()
    if (
        _is_link_or_reparse(path, after)
        or not stat.S_ISREG(after.st_mode)
        or int(after.st_size) != int(details.st_size)
        or int(getattr(after, "st_mtime_ns", 0))
        != int(getattr(details, "st_mtime_ns", 0))
    ):
        raise ValueError(f"projection_file_changed:{path.name}")
    return decoded, after


def assert_legacy_projection_file_size(path: Path) -> int:
    """Return a bounded v1 artifact size without decoding its JSON payload."""
    resolved = Path(path)
    details = resolved.lstat()
    if _is_link_or_reparse(resolved, details) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"legacy_projection_path_not_plain_file:{resolved.name}")
    size = int(details.st_size)
    if size > LEGACY_PROJECTION_FILE_MAX_BYTES:
        raise ValueError(f"legacy_projection_file_too_large:{resolved.name}")
    return size


def projection_v2_reachable_inventory(
    *,
    wiki_dir: Path | None = None,
    max_objects: int = _MAX_PROJECTION_OBJECTS,
) -> dict | None:
    """Validate and inventory the exact object closure of a committed v2 pair.

    ``None`` means the live locator pair is not v2.  A partial or malformed v2
    surface raises instead of being misreported as a legacy projection.
    """
    root = Path(wiki_dir) if wiki_dir is not None else get_wiki_dir()
    index_path = root / INDEX_FILENAME
    graph_path = root / CLAIM_GRAPH_FILENAME
    sidecar_path = root / SIDECAR_FILENAME
    if not index_path.exists() and not graph_path.exists() and not sidecar_path.exists():
        return None
    try:
        index_size = assert_legacy_projection_file_size(index_path)
    except FileNotFoundError:
        return None
    # Locator/sidecar JSON stays tiny by contract. A larger bounded index is a
    # legacy-v1 candidate and is validated by the explicit migration/backup
    # reader instead of being decoded as a v2 locator.
    if index_size > _MAX_PROJECTION_SIDECAR_BYTES:
        return None

    locator_details: dict[str, os.stat_result] = {}
    for projection, path in (("index", index_path), ("claim_graph", graph_path)):
        try:
            payload, details = _read_plain_json(path, max_bytes=_MAX_PROJECTION_SIDECAR_BYTES)
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            if projection == "index":
                return None
            raise ValueError("projection_v2_locator_pair_incomplete") from exc
        if not is_v2_locator(path, projection):
            if projection == "index":
                return None
            raise ValueError("projection_v2_locator_pair_mixed")
        validate_locator(path, projection)
        locator_details[path.name] = details

    sidecar_payload, sidecar_details = _read_plain_json(
        sidecar_path,
        max_bytes=_MAX_PROJECTION_SIDECAR_BYTES,
    )
    sidecar_payload = validate_sidecar(sidecar_payload)
    sidecar_bytes = sidecar_path.read_bytes()
    if sidecar_bytes != canonical_json_bytes(sidecar_payload):
        raise ValueError("projection_v2_sidecar_not_canonical")
    sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
    object_paths: dict[str, Path] = {}
    if max_objects != _MAX_PROJECTION_OBJECTS:
        raise ValueError("projection_v2_inventory_bound_is_fixed")
    for object_path in validate_root_closure(root, sidecar_payload):
        object_paths[object_path.stem] = object_path

    objects: list[dict] = []
    reachable_bytes = 0
    for digest, path in sorted(object_paths.items()):
        details = path.lstat()
        if _is_link_or_reparse(path, details) or not stat.S_ISREG(details.st_mode):
            raise ValueError(f"projection_object_not_plain_file:{digest}")
        size = int(details.st_size)
        reachable_bytes += size
        objects.append(
            {
                "sha256": digest,
                "bytes": size,
                "path": str(path),
            }
        )

    # Bind the inventory to the same mutable commit marker that selected roots.
    current_sidecar = sidecar_path.read_bytes()
    if not hmac.compare_digest(sidecar_bytes, current_sidecar):
        raise ValueError("projection_v2_sidecar_changed_during_inventory")
    locator_bytes = sum(int(item.st_size) for item in locator_details.values())
    static_bytes = locator_bytes + int(sidecar_details.st_size)
    return {
        "format_version": 2,
        "wiki_dir": str(root),
        "projection_generation": sidecar_payload["projection_generation"],
        "canonical_generation": sidecar_payload["canonical_generation"],
        "roots": {
            "index": sidecar_payload["index_root_sha256"],
            "claim_graph": sidecar_payload["claim_graph_root_sha256"],
        },
        "sidecar": sidecar_payload,
        "sidecar_sha256": sidecar_sha256,
        "objects": objects,
        "object_count": len(objects),
        "reachable_object_bytes": reachable_bytes,
        "static_bytes": static_bytes,
        "total_projection_bytes": reachable_bytes + static_bytes,
    }


def _inventory_tree_bytes(root: Path) -> tuple[int, int, bool, list[str]]:
    if not root.exists():
        return 0, 0, True, []
    try:
        root_details = root.lstat()
    except OSError as exc:
        return 0, 0, False, [f"backup_root_stat_failed:{type(exc).__name__}"]
    if _is_link_or_reparse(root, root_details) or not stat.S_ISDIR(
        root_details.st_mode
    ):
        return 0, 0, False, ["backup_root_not_plain_directory"]

    total_bytes = 0
    entries = 0
    issues: list[str] = []
    for current_root, directory_names, file_names in os.walk(
        root,
        followlinks=False,
    ):
        current = Path(current_root)
        retained_directories: list[str] = []
        for name in directory_names:
            path = current / name
            try:
                details = path.lstat()
            except OSError as exc:
                issues.append(
                    f"backup_inventory_stat_failed:{path.name}:{type(exc).__name__}"
                )
                continue
            if _is_link_or_reparse(path, details):
                issues.append(f"backup_inventory_link_skipped:{path.name}")
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in file_names:
            entries += 1
            if entries > _MAX_INVENTORY_ENTRIES:
                issues.append("backup_inventory_entry_limit_exceeded")
                return total_bytes, entries - 1, False, issues
            path = current / name
            try:
                details = path.lstat()
            except OSError as exc:
                issues.append(
                    f"backup_inventory_stat_failed:{path.name}:{type(exc).__name__}"
                )
                continue
            if _is_link_or_reparse(path, details):
                issues.append(f"backup_inventory_link_skipped:{path.name}")
                continue
            if stat.S_ISREG(details.st_mode):
                total_bytes += int(details.st_size)
    return total_bytes, entries, not issues, issues


def _default_backup_roots() -> tuple[Path, ...]:
    meta_dir = peek_meta_dir()
    candidates = (
        meta_dir / "backups",
        peek_db_path().parent / "schema-migration-backups",
    )
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.expanduser().resolve()))
        if key not in seen:
            roots.append(candidate)
            seen.add(key)
    return tuple(roots)


def _existing_disk_anchor(paths: Iterable[Path]) -> Path:
    for raw_path in paths:
        candidate = raw_path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        if candidate.exists():
            return candidate
    return Path.cwd()


def backup_capacity_status(
    *,
    estimated_new_bytes: int = 0,
    backup_roots: Iterable[Path] | None = None,
    disk_anchor: Path | None = None,
) -> dict:
    """Assess global backup bytes and projected free-space headroom."""
    estimated_new_bytes = max(0, int(estimated_new_bytes))
    roots = tuple(Path(item) for item in (backup_roots or _default_backup_roots()))
    policy = backup_capacity_policy()
    current_bytes = 0
    file_count = 0
    inventory_complete = True
    issues: list[str] = []
    root_details: list[dict] = []
    for root in roots:
        size, entries, complete, root_issues = _inventory_tree_bytes(root)
        current_bytes += size
        file_count += entries
        inventory_complete = inventory_complete and complete
        issues.extend(root_issues)
        root_details.append(
            {
                "path": str(root),
                "bytes": size,
                "files": entries,
                "scan_complete": complete,
            }
        )

    anchor = (
        _existing_disk_anchor((Path(disk_anchor), *roots))
        if disk_anchor is not None
        else _existing_disk_anchor(roots)
    )
    usage = shutil.disk_usage(anchor)
    minimum_free_required = max(
        int(policy["min_free_bytes"]),
        math.ceil(int(usage.total) * float(policy["min_free_ratio"])),
    )
    projected_free = int(usage.free) - estimated_new_bytes
    projected_total = current_bytes + estimated_new_bytes
    max_total_bytes = int(policy["max_total_bytes"])
    quota_configured = max_total_bytes > 0
    quota_exceeded = quota_configured and projected_total > max_total_bytes
    free_space_insufficient = projected_free < minimum_free_required
    quota_blocks = quota_exceeded and policy["quota_mode"] == "enforce"
    allowed = inventory_complete and not free_space_insufficient and not quota_blocks
    warnings = list(issues)
    if not quota_configured:
        warnings.append("backup_max_total_bytes_unconfigured")
    if quota_exceeded:
        warnings.append("backup_max_total_bytes_exceeded")
    if free_space_insufficient:
        warnings.append("backup_minimum_free_space_not_preserved")
    return {
        "allowed": allowed,
        "policy": policy,
        "backup_roots": root_details,
        "inventory_complete": inventory_complete,
        "current_backup_bytes": current_bytes,
        "backup_files": file_count,
        "estimated_new_bytes": estimated_new_bytes,
        "projected_backup_bytes": projected_total,
        "disk_total_bytes": int(usage.total),
        "disk_free_bytes": int(usage.free),
        "projected_free_bytes": projected_free,
        "minimum_free_required_bytes": minimum_free_required,
        "quota_configured": quota_configured,
        "quota_exceeded": quota_exceeded,
        "free_space_insufficient": free_space_insufficient,
        "warnings": warnings,
    }


def assert_backup_capacity(
    *,
    estimated_new_bytes: int,
    operation: str,
    backup_roots: Iterable[Path] | None = None,
    disk_anchor: Path | None = None,
) -> dict:
    """Fail before staging if the configured recovery headroom is not provable."""
    status = backup_capacity_status(
        estimated_new_bytes=estimated_new_bytes,
        backup_roots=backup_roots,
        disk_anchor=disk_anchor,
    )
    status["operation"] = str(operation)
    if status["allowed"]:
        return status
    if not status["inventory_complete"]:
        reason = "backup_inventory_incomplete"
    elif status["free_space_insufficient"]:
        reason = "minimum_free_space"
    else:
        reason = "max_total_bytes"
    raise BackupCapacityError(reason, status)


def estimate_maintenance_backup_bytes() -> int:
    """Estimate the next SQLite/projection snapshot with explicit headroom."""
    paths = (peek_db_path(), Path(str(peek_db_path()) + "-wal"))
    source_bytes = 0
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink():
                source_bytes += int(path.stat().st_size)
        except OSError:
            continue
    v2_inventory = projection_v2_reachable_inventory()
    if v2_inventory is not None:
        source_bytes += int(v2_inventory["total_projection_bytes"])
    else:
        for path in (
            get_index_path(),
            get_claim_graph_path(),
            get_projection_manifest_path(),
        ):
            try:
                if path.is_file() and not path.is_symlink():
                    source_bytes += int(path.stat().st_size)
            except OSError:
                continue
    return max(
        1024 * 1024,
        math.ceil(source_bytes * (1.0 + _DEFAULT_ESTIMATE_HEADROOM_RATIO)),
    )


def estimate_database_backup_bytes(database_path: Path | None = None) -> int:
    """Estimate one standalone SQLite backup, including active WAL headroom."""
    path = Path(database_path) if database_path is not None else peek_db_path()
    source_bytes = 0
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                source_bytes += int(candidate.stat().st_size)
        except OSError:
            continue
    return max(
        1024 * 1024,
        math.ceil(source_bytes * (1.0 + _DEFAULT_ESTIMATE_HEADROOM_RATIO)),
    )


__all__ = [
    "LEGACY_PROJECTION_FILE_MAX_BYTES",
    "BackupCapacityError",
    "assert_legacy_projection_file_size",
    "assert_backup_capacity",
    "backup_capacity_policy",
    "backup_capacity_status",
    "estimate_database_backup_bytes",
    "estimate_maintenance_backup_bytes",
    "projection_v2_reachable_inventory",
]
