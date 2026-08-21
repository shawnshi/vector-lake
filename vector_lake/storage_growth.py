"""Bounded daily storage-growth telemetry for Vector Lake operations."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from vector_lake.db_store import peek_db_path
from vector_lake.wiki_utils import atomic_write_text, peek_meta_dir


_CONTRACT = "vector-lake-storage-growth-v1"
_HISTORY_DAYS = 35
_ROW_TABLES = ("claim_versions", "evidence_versions")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_bytes(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.is_file() else 0
    except OSError:
        return 0


def _backup_bytes(root: Path, *, max_entries: int = 20_000) -> tuple[int, int, bool]:
    if not root.is_dir() or root.is_symlink():
        return 0, 0, True
    total = 0
    entries = 0
    complete = True
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        directories[:] = [
            name for name in directories if not (current / name).is_symlink()
        ]
        for name in filenames:
            entries += 1
            if entries > max_entries:
                complete = False
                return total, entries - 1, complete
            path = current / name
            if not path.is_symlink():
                total += _file_bytes(path)
    return total, entries, complete


def collect_storage_sample(*, sampled_at: datetime | None = None) -> dict[str, Any]:
    """Collect one read-only storage sample without creating database state."""
    observed_at = sampled_at or _utc_now()
    db_path = peek_db_path()
    if not db_path.is_file():
        raise FileNotFoundError(f"Vector Lake database not found: {db_path}")

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        connection.execute("PRAGMA query_only = ON")
        known_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        row_counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                or 0
            )
            for table in _ROW_TABLES
            if table in known_tables
        }
        version_payload_bytes = sum(
            int(
                connection.execute(
                    f"SELECT COALESCE(SUM(length(CAST(data_json AS BLOB))), 0) "
                    f"FROM {table}"
                ).fetchone()[0]
                or 0
            )
            for table in _ROW_TABLES
            if table in known_tables
        )
    finally:
        connection.close()

    meta_dir = peek_meta_dir()
    backup_bytes, backup_files, backup_scan_complete = _backup_bytes(
        meta_dir / "backups"
    )
    return {
        "sampled_at": _iso_utc(observed_at),
        "date_utc": observed_at.astimezone(timezone.utc).date().isoformat(),
        "database_bytes": _file_bytes(db_path),
        "wal_bytes": _file_bytes(Path(str(db_path) + "-wal")),
        "row_counts": row_counts,
        "version_payload_bytes": version_payload_bytes,
        "backup_bytes": backup_bytes,
        "backup_files": backup_files,
        "backup_scan_complete": backup_scan_complete,
    }


def _history_path(meta_dir: Path | None = None) -> Path:
    return (meta_dir or peek_meta_dir()) / "storage_growth.json"


def _load_history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("contract") != _CONTRACT or not isinstance(
        payload.get("samples"), list
    ):
        raise ValueError("storage growth history contract is invalid")
    return [item for item in payload["samples"] if isinstance(item, dict)]


def record_storage_growth_sample(
    *,
    sampled_at: datetime | None = None,
    sample: dict[str, Any] | None = None,
    meta_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically upsert the UTC day's sample and retain a bounded history."""
    resolved_meta = meta_dir or peek_meta_dir()
    resolved_meta.mkdir(parents=True, exist_ok=True)
    path = _history_path(resolved_meta)
    observed = sample or collect_storage_sample(sampled_at=sampled_at)
    date_utc = str(observed.get("date_utc") or "")
    if not date_utc:
        raise ValueError("storage sample requires date_utc")

    with FileLock(str(path) + ".lock", timeout=5.0):
        samples = _load_history(path)
        samples = [item for item in samples if item.get("date_utc") != date_utc]
        samples.append(dict(observed))
        samples.sort(key=lambda item: str(item.get("date_utc") or ""))
        samples = samples[-_HISTORY_DAYS:]
        payload = {
            "contract": _CONTRACT,
            "updated_at": str(observed.get("sampled_at") or ""),
            "samples": samples,
        }
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    return storage_growth_status(meta_dir=resolved_meta)


def _delta(latest: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    latest_rows = latest.get("row_counts") or {}
    previous_rows = previous.get("row_counts") or {}
    return {
        "database_bytes": int(latest.get("database_bytes") or 0)
        - int(previous.get("database_bytes") or 0),
        "wal_bytes": int(latest.get("wal_bytes") or 0)
        - int(previous.get("wal_bytes") or 0),
        "version_payload_bytes": int(latest.get("version_payload_bytes") or 0)
        - int(previous.get("version_payload_bytes") or 0),
        "backup_bytes": int(latest.get("backup_bytes") or 0)
        - int(previous.get("backup_bytes") or 0),
        "row_counts": {
            table: int(latest_rows.get(table) or 0)
            - int(previous_rows.get(table) or 0)
            for table in _ROW_TABLES
        },
    }


def storage_growth_status(*, meta_dir: Path | None = None) -> dict[str, Any]:
    """Read the latest daily baseline and its prior-day delta."""
    path = _history_path(meta_dir)
    try:
        samples = _load_history(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "invalid",
            "path": str(path),
            "sample_count": 0,
            "error": f"{type(exc).__name__}:{exc}",
        }
    if not samples:
        return {
            "status": "not_initialized",
            "path": str(path),
            "sample_count": 0,
        }
    latest = samples[-1]
    result: dict[str, Any] = {
        "status": "ready" if len(samples) >= 2 else "baseline_only",
        "path": str(path),
        "sample_count": len(samples),
        "latest": latest,
    }
    if len(samples) >= 2:
        result["previous"] = samples[-2]
        result["delta"] = _delta(latest, samples[-2])
    return result


__all__ = [
    "collect_storage_sample",
    "record_storage_growth_sample",
    "storage_growth_status",
]
