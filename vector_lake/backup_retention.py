"""Bounded retention for the backups under ``.meta/backups``.

``db_store.backup_database`` and ``tool_projection.create_maintenance_backup`` are
called from the repair, projection and ingest paths and never remove anything, so
the backup tree only grows.  On this installation it reached three full copies of a
4 GB database (11 GB) and the ``storage_growth`` samples recorded it doubling from
3.90 GB to 7.80 GB in a single day.  The modules that used to own that policy
(``backup_capacity``, ``tool_backup_retention``) are no longer in this tree.

This module answers one question -- which entries are safe to remove -- and, by
default, removes nothing.  ``dry_run=True`` is the default for the public API; the
scheduled maintenance path is the only caller that passes ``dry_run=False``, and it
uses a bound that never drops below the newest content-verified copy. Legacy,
incomplete and changed backups are protected rather than automatically pruned.

Two shapes live in the backup root and both are treated as one indivisible unit:

* ``vector_lake_<epoch>.db.bak`` plus its ``-wal`` / ``-shm`` sidecars -- a
  single-file SQLite backup written by ``backup_database``.
* ``<label>_<stamp>/`` -- a directory written by ``create_maintenance_backup``,
  holding the database plus the projections and a ``manifest.json``.

Anything else in the root is reported as unrecognised and is never removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
import uuid
from contextlib import contextmanager

from filelock import FileLock
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("vector-lake-backup-retention")

# These bounds apply to verified units, not protected legacy or changed files.
# Keeping a content-verified snapshot is not evidence of a restore rehearsal.
DEFAULT_KEEP_COUNT = 3
DEFAULT_MAX_BYTES = 12 * 1024**3
SIDECAR_SUFFIXES = ("-wal", "-shm")
DATABASE_SUFFIX = ".db.bak"
RECEIPT_SUFFIX = ".verified.json"
BACKUP_LOCK_NAME = ".backup.lock"
VERIFICATION_SECONDS = 60.0

KEEP_COUNT_ENV = "VECTOR_LAKE_BACKUP_KEEP"
MAX_BYTES_ENV = "VECTOR_LAKE_BACKUP_MAX_BYTES"

# A pathological backup tree must not turn a housekeeping task into a full disk
# walk; the scan stops counting and reports the entry as unbounded instead.
_MAX_SCAN_ENTRIES = 200_000


@dataclass(frozen=True)
class BackupEntry:
    """One removable unit: a ``.db.bak`` with its sidecars, or a directory."""

    members: tuple[Path, ...]
    kind: str
    total_bytes: int
    modified_at: float

    @property
    def path(self) -> Path:
        return self.members[0]

    @property
    def name(self) -> str:
        return self.path.name

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "bytes": self.total_bytes,
            "members": [member.name for member in self.members],
        }


def _file_bytes(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _tree_bytes(path: Path) -> int:
    total = 0
    scanned = 0
    for current_root, directories, filenames in os.walk(path, followlinks=False):
        current = Path(current_root)
        directories[:] = [name for name in directories if not (current / name).is_symlink()]
        for name in filenames:
            scanned += 1
            if scanned > _MAX_SCAN_ENTRIES:
                return total
            candidate = current / name
            if not candidate.is_symlink():
                total += _file_bytes(candidate)
    return total


def _mtime(path: Path) -> float:
    """The entry's own timestamp.

    Deliberately not the newest file inside it: a directory backup that later
    receives one more file must not look like a fresh backup, and walking an
    11 GB tree just to read a timestamp is pure waste.
    """
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def backup_lock(root: str | Path) -> FileLock:
    """Serialize backup publication and retention within one backup root."""
    return FileLock(str(Path(root) / BACKUP_LOCK_NAME), timeout=30)


def _receipt_path(path: Path) -> Path:
    return path / "manifest.json" if path.is_dir() else path.with_name(path.name + RECEIPT_SUFFIX)


@contextmanager
def _regular_file(path: Path):
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise ValueError("backup member is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError("backup member changed while opening")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            yield handle
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _digest(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with _regular_file(path) as handle:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("backup verification budget exhausted")
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def seal_backup(path: str | Path, files: list[Path], *, sqlite_integrity: str, database_member: str, metadata: dict | None = None) -> None:
    """Bind a completed, SQLite-validated snapshot to its exact member contents.

    Callers must hold the backup root lock and supply the result of the actual
    SQLite integrity check. This receipt is not evidence of a restore rehearsal.
    """
    if sqlite_integrity != "ok" or not files:
        raise ValueError("a backup requires a successful SQLite integrity check")
    path = Path(path)
    parent = path if path.is_dir() else path.parent
    deadline = time.monotonic() + VERIFICATION_SECONDS
    records = []
    for member in files:
        if member.parent.resolve() != parent.resolve() or member.name == "manifest.json":
            raise ValueError("backup members must be direct children of the backup unit")
        records.append({"name": member.name, "size": member.stat().st_size, "sha256": _digest(member, deadline)})
    if database_member not in {record["name"] for record in records}:
        raise ValueError("validated database must be a member of the backup unit")
    receipt = dict(metadata or {})
    receipt.update({"backup_format": 1, "state": "complete", "sqlite_integrity": "ok", "database_member": database_member, "files": records})
    target = _receipt_path(path)
    temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _verified_members(path: Path, deadline: float) -> tuple[Path, ...]:
    if path.name.endswith(".partial"):
        raise ValueError("backup publication is incomplete")
    receipt_path = _receipt_path(path)
    if time.monotonic() >= deadline:
        raise TimeoutError("backup verification budget exhausted")
    with _regular_file(receipt_path) as handle:
        encoded = handle.read(64 * 1024 + 1)
    if len(encoded) > 64 * 1024:
        raise ValueError("invalid backup receipt")
    receipt = json.loads(encoded.decode("utf-8"))
    if not isinstance(receipt, dict) or receipt.get("backup_format") != 1 or receipt.get("state") != "complete" or receipt.get("sqlite_integrity") != "ok":
        raise ValueError("no completed SQLite validation receipt")
    records = receipt.get("files")
    if not isinstance(records, list) or not records or len(records) > 128:
        raise ValueError("invalid backup membership")
    parent = path if path.is_dir() else path.parent
    members = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid backup member")
        name = record.get("name")
        if not isinstance(name, str) or not name or name in {".", "..", "manifest.json"} or "/" in name or "\\" in name or ":" in name:
            raise ValueError("invalid backup member name")
        member = parent / name
        if member.stat().st_size != record.get("size") or _digest(member, deadline) != record.get("sha256"):
            raise ValueError("backup member content no longer matches validation receipt")
        members.append(member)
    if len(set(members)) != len(members):
        raise ValueError("duplicate backup members")
    if receipt.get("database_member") not in {member.name for member in members}:
        raise ValueError("backup has no validated database member")
    if path.is_dir():
        if set(path.iterdir()) != set(members) | {receipt_path}:
            raise ValueError("backup directory contains unverified members")
        return (path, *members, receipt_path)
    allowed = {path, *(path.with_name(path.name + suffix) for suffix in SIDECAR_SUFFIXES)}
    actual = {member for member in allowed if member.exists()}
    if set(members) != actual or path not in members or receipt.get("database_member") != path.name:
        raise ValueError("database backup membership changed")
    return (*members, receipt_path)


def scan_backups(root: str | Path) -> dict:
    """List verified retention units; protect legacy, incomplete or changed backups."""
    root = Path(root)
    result = {"root": str(root), "entries": [], "unrecognized": [], "protected": [], "total_bytes": 0}
    if not root.is_dir():
        return result
    deadline = time.monotonic() + VERIFICATION_SECONDS
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            result["unrecognized"].append(child.name)
            continue
        if child.name == BACKUP_LOCK_NAME:
            continue
        if child.name.endswith(RECEIPT_SUFFIX) and child.with_name(child.name[:-len(RECEIPT_SUFFIX)]).is_file():
            continue
        if child.name.endswith(SIDECAR_SUFFIXES) and child.with_name(child.name.rsplit("-", 1)[0]).is_file():
            continue
        if not child.is_dir() and not child.name.endswith(DATABASE_SUFFIX):
            result["unrecognized"].append(child.name)
            continue
        try:
            members = _verified_members(child, deadline)
            kind = "directory" if child.is_dir() else "database"
            total = _tree_bytes(child) if kind == "directory" else sum(member.stat().st_size for member in members)
            result["entries"].append(BackupEntry(members, kind, total, _mtime(child)))
        except (OSError, ValueError, TypeError, TimeoutError) as exc:
            result["protected"].append({"name": child.name, "reason": f"{type(exc).__name__}: {exc}"})
    result["total_bytes"] = sum(entry.total_bytes for entry in result["entries"])
    return result


def resolve_bounds(
    keep_count: int | None = None, max_bytes: int | None = None
) -> tuple[int, int]:
    """Explicit arguments win; otherwise the environment; otherwise the defaults.

    ``keep_count`` is clamped to at least 1 so the newest copy is never a retention
    candidate.  A non-positive ``max_bytes`` disables the byte ceiling, leaving
    ``keep_count`` as the only bound.
    """
    if keep_count is None:
        keep_count = _int_env(KEEP_COUNT_ENV, DEFAULT_KEEP_COUNT)
    if max_bytes is None:
        max_bytes = _int_env(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)
    return max(1, int(keep_count)), max(0, int(max_bytes))


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        log.warning("Ignoring %s=%r: not an integer", name, raw)
        return default


def _plan(root: str | Path, keep_count: int, max_bytes: int) -> dict:
    scan = scan_backups(root)
    ordered = sorted(scan["entries"], key=lambda entry: (entry.modified_at, entry.name), reverse=True)

    keep = list(ordered[:keep_count])
    remove: list[BackupEntry] = list(ordered[keep_count:])
    # ``keep_count`` is a ceiling: everything older is gone.  ``max_bytes`` is a
    # second ceiling that can pull the retained set *below* ``keep_count`` when the
    # newest verified copies alone exceed it -- but never below one verified
    # snapshot. A receipt does not prove restore readiness. A non-positive
    # ``max_bytes`` disables the byte ceiling.
    if max_bytes > 0:
        retained = sum(entry.total_bytes for entry in keep)
        while len(keep) > 1 and retained > max_bytes:
            dropped = keep.pop()
            retained -= dropped.total_bytes
            remove.append(dropped)
    retained = sum(entry.total_bytes for entry in keep)

    return {
        "root": scan["root"],
        "keep_count": keep_count,
        "max_bytes": max_bytes,
        "total_bytes": scan["total_bytes"],
        "retained_bytes": retained,
        "removable_bytes": sum(entry.total_bytes for entry in remove),
        "unrecognized": scan["unrecognized"],
        "protected": scan["protected"],
        "keep": keep,
        "remove": remove,
    }


def plan_backup_retention(
    root: str | Path,
    keep_count: int | None = None,
    max_bytes: int | None = None,
) -> dict:
    """Describe what retention would remove, without removing it."""
    keep_count, max_bytes = resolve_bounds(keep_count, max_bytes)
    plan = _plan(root, keep_count, max_bytes)
    return _public(plan, dry_run=True, deleted=[], failures=[])


def prune_backups(
    root: str | Path,
    keep_count: int | None = None,
    max_bytes: int | None = None,
    dry_run: bool = True,
) -> dict:
    """Remove the entries retention rejects, unless ``dry_run`` (the default).

    The newest verified entry is never removable, every candidate is re-checked to be a
    direct child of ``root``, and symlinks are refused, so a mis-resolved root
    cannot turn this into a recursive delete somewhere else.
    """
    if not dry_run and Path(root).is_dir():
        with backup_lock(root):
            return _prune_locked(root, keep_count, max_bytes)
    return plan_backup_retention(root, keep_count, max_bytes)


def _prune_locked(root: str | Path, keep_count: int | None, max_bytes: int | None) -> dict:
    deadline = time.monotonic() + VERIFICATION_SECONDS
    keep_count, max_bytes = resolve_bounds(keep_count, max_bytes)
    plan = _plan(root, keep_count, max_bytes)
    root_resolved = Path(root).resolve()
    deleted: list[str] = []
    failures: list[str] = []
    for entry in plan["remove"]:
        try:
            resolved = entry.path.resolve()
        except OSError as exc:
            failures.append(f"{entry.name}: {exc}")
            continue
        if entry.path.is_symlink() or resolved.parent != root_resolved:
            failures.append(f"{entry.name}: refused, not a direct child of {root_resolved}")
            continue
        try:
            retained_valid = False
            for retained in plan["keep"]:
                try:
                    if _verified_members(retained.path, deadline) == retained.members:
                        retained_valid = True
                        break
                except (OSError, ValueError, TypeError, TimeoutError):
                    continue
            if not retained_valid:
                failures.append("refused: no verified retained backup remains; collection stopped")
                break
            members = _verified_members(entry.path, deadline)
            if members != entry.members:
                raise ValueError("backup membership changed after planning")
            if entry.kind == "directory":
                for member in members[1:]:
                    member.unlink(missing_ok=True)
                entry.path.rmdir()
            else:
                for member in members:
                    member.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, TimeoutError) as exc:
            failures.append(f"{entry.name}: {exc}")
            continue
        deleted.append(entry.name)

    if deleted:
        log.info(
            "Backup retention removed %s entr(ies) (%s bytes) from %s",
            len(deleted),
            sum(entry.total_bytes for entry in plan["remove"] if entry.name in deleted),
            root_resolved,
        )
    for failure in failures:
        log.warning("Backup retention left %s", failure)

    return _public(plan, dry_run=False, deleted=deleted, failures=failures)


def _public(plan: dict, *, dry_run: bool, deleted: list, failures: list) -> dict:
    return {
        "root": plan["root"],
        "dry_run": dry_run,
        "keep_count": plan["keep_count"],
        "max_bytes": plan["max_bytes"],
        "entry_count": len(plan["keep"]) + len(plan["remove"]),
        "total_bytes": plan["total_bytes"],
        "retained_bytes": plan["retained_bytes"],
        "removable_bytes": plan["removable_bytes"],
        "keep": [entry.describe() for entry in plan["keep"]],
        "remove": [entry.describe() for entry in plan["remove"]],
        "unrecognized": plan["unrecognized"],
        "protected": plan["protected"],
        "deleted": deleted,
        "deleted_bytes": sum(entry.total_bytes for entry in plan["remove"] if entry.name in deleted),
        "failures": failures,
    }
