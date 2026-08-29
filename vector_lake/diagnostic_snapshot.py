"""Generation-bound, read-only snapshots shared by diagnostic surfaces."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

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
    except OSError:
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
    wiki_paths = tuple(sorted(iter_markdown_files(wiki_dir), key=lambda path: path.name))
    wiki = tuple(
        _path_identity(path, relative_name=path.name)
        for path in wiki_paths
    )
    projection_paths = tuple(path_factory() for path_factory in _PROJECTION_PATHS)
    projection = tuple(
        _path_identity(path, relative_name=path.name)
        for path in projection_paths
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
    _external_identity: dict[str, tuple[tuple[Any, ...], ...]] = field(
        repr=False
    )

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


def _unavailable_code(exc: BaseException) -> str:
    message = str(exc).casefold()
    if any(token in message for token in ("locked", "busy", "timeout", "timed out")):
        return "snapshot_timeout"
    if "database_missing" in message:
        return "database_missing"
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
                "SELECT surface, generation FROM runtime_generations "
                "ORDER BY surface"
            ).fetchall()
            runtime_generations = {
                str(row[0]): int(row[1])
                for row in rows
            }
            if not runtime_generations:
                raise DiagnosticSnapshotUnavailable(
                    "diagnostic_snapshot_unavailable"
                )

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
                wiki_dir / str(identity[0])
                for identity in before["wiki"]
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
