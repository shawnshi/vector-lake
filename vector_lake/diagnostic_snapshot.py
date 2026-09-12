"""Generation-bound, read-only snapshots shared by diagnostic surfaces."""

from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vector_lake import db_store, indexer
from vector_lake.durability import durability_profile
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_index_path,
    get_projection_manifest_path,
    get_wiki_dir,
    iter_markdown_files,
)

_CONTRACT_VERSION = "vector-lake-diagnostic-snapshot/v1"
_PROJECTION_PATHS = (
    get_index_path,
    get_claim_graph_path,
    get_projection_manifest_path,
)


class DiagnosticSnapshotChanged(RuntimeError):
    """Raised when a non-transactional diagnostic surface drifts."""


class DiagnosticSnapshotUnavailable(RuntimeError):
    """Raised when a fail-closed diagnostic snapshot cannot be captured."""


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _path_identity(path: Path, *, relative_name: str) -> tuple[Any, ...]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (relative_name, "missing")
    return (
        relative_name,
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
        int(stat.st_size),
    )


def _capture_external_identity() -> dict[str, tuple[tuple[Any, ...], ...]]:
    wiki_dir = get_wiki_dir()
    wiki_paths = tuple(
        sorted(iter_markdown_files(wiki_dir), key=lambda path: path.name)
    )
    wiki = tuple(_path_identity(path, relative_name=path.name) for path in wiki_paths)
    projection_paths = tuple(path_factory() for path_factory in _PROJECTION_PATHS)
    projection = tuple(
        _path_identity(path, relative_name=path.name) for path in projection_paths
    )
    return {"wiki": wiki, "projection": projection}


def current_durability_status() -> dict[str, Any]:
    """Return a path- and configuration-value-safe durability diagnostic."""
    try:
        profile = durability_profile()
    except RuntimeError:
        return {
            "profile": "invalid",
            "valid": False,
            "status": "invalid",
        }
    return {
        "profile": profile,
        "valid": True,
        "status": profile,
    }


@dataclass(slots=True)
class DiagnosticSnapshot:
    """Caller-owned logical snapshot; valid only inside its capture context."""

    connection: sqlite3.Connection = field(repr=False)
    db_path: Path = field(repr=False)
    index_data: dict[str, Any] = field(repr=False)
    wiki_paths: tuple[Path, ...] = field(repr=False)
    captured_at: str
    database_runtime_generations: dict[str, int]
    projection_generation: str | None
    projection_status: str
    projection_error: str | None
    wiki_fingerprint: str
    generation_fingerprint: str
    source_fingerprint: str
    durability: dict[str, Any]
    _external_identity: dict[str, tuple[tuple[Any, ...], ...]] = field(repr=False)

    def metadata(self) -> dict[str, Any]:
        """Return the stable, path-private public snapshot contract."""
        return {
            "contract_version": _CONTRACT_VERSION,
            "captured_at": self.captured_at,
            "database": {
                "access": "read_only_transaction",
                "runtime_generations": dict(self.database_runtime_generations),
            },
            "projection": {
                "status": self.projection_status,
                "generation": self.projection_generation,
                "error": self.projection_error,
            },
            "wiki": {
                "file_count": len(self.wiki_paths),
                "identity": self.wiki_fingerprint,
                "captured_at": self.captured_at,
            },
            "durability": dict(self.durability),
            "generation_fingerprint": self.generation_fingerprint,
            "source_fingerprint": self.source_fingerprint,
        }


_READ_ONLY_SNAPSHOT_REASON_PREFIX = "database_read_only_snapshot_unavailable:"

# Closed allowlist of structural sidecar failures whose reason token is safe to
# surface.  Free-form messages can embed filesystem paths (see
# tests/test_diagnostic_snapshot.py "...sanitizes_unknowns"), so anything outside
# this list must keep degrading to the generic code.
_SANITIZED_READ_ONLY_SNAPSHOT_REASONS = frozenset(
    {
        "database_has_uncheckpointed_wal",
        "invalid_wal_frame",
        "invalid_wal_frame_checksum",
        "invalid_wal_frame_salt",
        "invalid_wal_header",
        "invalid_wal_header_checksum",
        "invalid_wal_index",
        "invalid_wal_index_checksum",
        "invalid_wal_index_frame_checksum",
        "invalid_wal_layout",
        "missing_wal_index",
    }
)


def _trusted_snapshot_cause(exc: BaseException) -> BaseException | None:
    cause = getattr(exc, "__cause__", None)
    for _ in range(4):
        if not isinstance(cause, db_store.ReadOnlySnapshotUnavailable):
            return cause
        next_cause = getattr(cause, "__cause__", None)
        if next_cause is None or next_cause is cause:
            return cause
        cause = next_cause
    return cause


def _unavailable_code(exc: BaseException) -> str:
    cause = _trusted_snapshot_cause(exc)
    if isinstance(cause, PermissionError):
        return "snapshot_permission_denied"
    if isinstance(cause, OSError):
        if cause.errno in {errno.EACCES, errno.EPERM}:
            return "snapshot_permission_denied"
        if cause.errno == errno.EIO:
            return "snapshot_io_error"
        if cause.errno in {errno.ETIMEDOUT, errno.EBUSY}:
            return "snapshot_timeout"
    if isinstance(cause, sqlite3.OperationalError):
        message = str(cause).casefold()
        if any(
            token in message for token in ("locked", "busy", "timeout", "timed out")
        ):
            return "snapshot_timeout"
    if isinstance(exc, db_store.ReadOnlySnapshotUnavailable):
        reason = str(exc)
        if reason == "database_changed_during_read_only_snapshot":
            return reason
        if reason.startswith("database_missing"):
            return "database_missing"
        if reason.startswith(_READ_ONLY_SNAPSHOT_REASON_PREFIX):
            token = reason[len(_READ_ONLY_SNAPSHOT_REASON_PREFIX):].split(":", 1)[0].strip()
            if token in _SANITIZED_READ_ONLY_SNAPSHOT_REASONS:
                return token
        return "diagnostic_snapshot_unavailable"
    if isinstance(exc, PermissionError):
        return "snapshot_permission_denied"
    if isinstance(exc, OSError):
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return "snapshot_permission_denied"
        if exc.errno == errno.EIO:
            return "snapshot_io_error"
        if exc.errno in {errno.ETIMEDOUT, errno.EBUSY}:
            return "snapshot_timeout"
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).casefold()
        if any(
            token in message for token in ("locked", "busy", "timeout", "timed out")
        ):
            return "snapshot_timeout"
    return "diagnostic_snapshot_unavailable"


@contextmanager
def capture_diagnostic_snapshot(
    *,
    timeout: float = 5.0,
) -> Iterator[DiagnosticSnapshot]:
    """Capture one DB transaction and one committed projection attempt.

    SQLite supplies the reader barrier for canonical state. Wiki and projection
    artifacts are non-transactional, so their identities are checked both
    around capture and again when the caller releases the snapshot.
    """
    db_path = db_store.peek_db_path().resolve()
    try:
        before = _capture_external_identity()
    except OSError as exc:
        raise DiagnosticSnapshotUnavailable(_unavailable_code(exc)) from exc
    failed = False
    try:
        with db_store.read_only_transaction_snapshot(
            db_path,
            timeout=timeout,
        ) as connection:
            rows = connection.execute(
                "SELECT surface, generation FROM runtime_generations ORDER BY surface"
            ).fetchall()
            runtime_generations = {str(row[0]): int(row[1]) for row in rows}
            if not runtime_generations:
                raise DiagnosticSnapshotUnavailable("diagnostic_snapshot_unavailable")

            index_data: dict[str, Any] = {"nodes": {}}
            projection_status = "unavailable"
            projection_error = None
            try:
                index_data = indexer.read_committed_index_snapshot(
                    get_index_path(),
                    lock_timeout=timeout,
                    connection=connection,
                    _acquire_lock=False,
                )
                projection_status = "committed_current"
            except Exception as exc:
                projection_error = type(exc).__name__

            after = _capture_external_identity()
            if after != before:
                raise DiagnosticSnapshotChanged("snapshot_changed")

            captured_at = datetime.now(timezone.utc).isoformat()
            projection_manifest = dict(
                (index_data or {}).get("projection_manifest") or {}
            )
            projection_generation = projection_manifest.get("generation")
            if not isinstance(projection_generation, str) or not projection_generation:
                projection_generation = None
            durability = current_durability_status()
            generation_fingerprint = _sha256_json(
                {
                    "runtime_generations": runtime_generations,
                    "projection_generation": projection_generation,
                }
            )
            wiki_fingerprint = _sha256_json(before["wiki"])
            source_fingerprint = _sha256_json(
                {
                    "generation_fingerprint": generation_fingerprint,
                    "projection_identity": before["projection"],
                    "projection_status": projection_status,
                    "wiki_identity": before["wiki"],
                }
            )
            wiki_dir = get_wiki_dir()
            wiki_paths = tuple(
                wiki_dir / str(identity[0]) for identity in before["wiki"]
            )
            snapshot = DiagnosticSnapshot(
                connection=connection,
                db_path=db_path,
                index_data=index_data,
                wiki_paths=wiki_paths,
                captured_at=captured_at,
                database_runtime_generations=runtime_generations,
                projection_generation=projection_generation,
                projection_status=projection_status,
                projection_error=projection_error,
                wiki_fingerprint=wiki_fingerprint,
                generation_fingerprint=generation_fingerprint,
                source_fingerprint=source_fingerprint,
                durability=durability,
                _external_identity=before,
            )
            try:
                yield snapshot
            except BaseException:
                failed = True
                raise
            finally:
                if not failed and _capture_external_identity() != before:
                    raise DiagnosticSnapshotChanged("snapshot_changed")
    except DiagnosticSnapshotChanged:
        raise
    except DiagnosticSnapshotUnavailable:
        raise
    except (db_store.ReadOnlySnapshotUnavailable, sqlite3.Error, OSError) as exc:
        raise DiagnosticSnapshotUnavailable(_unavailable_code(exc)) from exc
