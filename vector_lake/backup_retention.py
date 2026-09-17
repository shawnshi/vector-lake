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
uses a bound that deliberately never drops below the newest copy.

Two shapes live in the backup root and both are treated as one indivisible unit:

* ``vector_lake_<epoch>.db.bak`` plus its ``-wal`` / ``-shm`` sidecars -- a
  single-file SQLite backup written by ``backup_database``.
* ``<label>_<stamp>/`` -- a directory written by ``create_maintenance_backup``,
  holding the database plus the projections and a ``manifest.json``.

Anything else in the root is reported as unrecognised and is never removed.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("vector-lake-backup-retention")

# Keep the three newest copies and never exceed 12 GiB.  Both bounds are checked;
# the newest copy is retained unconditionally, so retention can never leave the
# installation without a recoverable backup.
DEFAULT_KEEP_COUNT = 3
DEFAULT_MAX_BYTES = 12 * 1024**3
SIDECAR_SUFFIXES = ("-wal", "-shm")
DATABASE_SUFFIX = ".db.bak"

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


def scan_backups(root: str | Path) -> dict:
    """List the removable units in ``root`` without touching anything."""
    root = Path(root)
    result: dict = {
        "root": str(root),
        "entries": [],
        "unrecognized": [],
        "total_bytes": 0,
    }
    if not root.is_dir():
        return result

    entries: list[BackupEntry] = []
    unrecognized: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            unrecognized.append(child.name)
            continue
        if child.is_dir():
            entries.append(
                BackupEntry((child,), "directory", _tree_bytes(child), _mtime(child))
            )
            continue
        if not child.is_file():
            unrecognized.append(child.name)
            continue
        if child.name.endswith(SIDECAR_SUFFIXES):
            # Owned by the .db.bak next to it; never a unit on its own.
            continue
        if not child.name.endswith(DATABASE_SUFFIX):
            unrecognized.append(child.name)
            continue
        members = [child]
        for suffix in SIDECAR_SUFFIXES:
            sidecar = child.with_name(child.name + suffix)
            if sidecar.is_file():
                members.append(sidecar)
        entries.append(
            BackupEntry(
                tuple(members),
                "database",
                sum(_file_bytes(member) for member in members),
                child.stat().st_mtime,
            )
        )

    result["entries"] = entries
    result["unrecognized"] = unrecognized
    result["total_bytes"] = sum(entry.total_bytes for entry in entries)
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
    # newest copies alone exceed it -- but never below one, so retention cannot
    # leave the installation without a recoverable backup.  A non-positive
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

    The newest entry is never removable, every candidate is re-checked to be a
    direct child of ``root``, and symlinks are refused, so a mis-resolved root
    cannot turn this into a recursive delete somewhere else.
    """
    keep_count, max_bytes = resolve_bounds(keep_count, max_bytes)
    plan = _plan(root, keep_count, max_bytes)
    if dry_run:
        return _public(plan, dry_run=True, deleted=[], failures=[])

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
            if entry.kind == "directory":
                shutil.rmtree(entry.path)
            else:
                for member in entry.members:
                    member.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"{entry.name}: {exc}")
            continue
        deleted.append(entry.name)

    if deleted:
        log.info(
            "Backup retention removed %s entr(ies) (%s bytes) from %s",
            len(deleted),
            plan["removable_bytes"],
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
        "deleted": deleted,
        "failures": failures,
    }
