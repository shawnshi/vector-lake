"""Cross-process admission gate for memory-intensive Vector Lake tasks.

The gate is deliberately a capacity control, not a replacement for index,
embedding, or SQLite consistency locks.  One operating-system file lock is
used per canonical meta root so MCP, CLI, and watchdog processes share the
same admission boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import threading
import time
import uuid
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

from vector_lake.wiki_utils import get_meta_dir, peek_meta_dir


log = logging.getLogger("vector-lake-heavy-task-gate")

_LOCK_FILENAME = ".heavy-task.lock"
_STATUS_FILENAME = ".heavy-task-status.json"
_STATUS_SCHEMA_VERSION = 1
_PROCESS_TOKEN = uuid.uuid4().hex
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_MANAGERS_LOCK = threading.Lock()
_MANAGERS: dict[str, "_HeavyTaskGate"] = {}


class HeavyTaskClass(str, Enum):
    """Resource profile used for policy and observability."""

    SCAN = "scan"
    PROJECTION = "projection"
    EMBEDDING = "embedding"
    MAINTENANCE = "maintenance"
    INGEST_SCAN = "ingest_scan"


class HeavyTaskStateError(RuntimeError):
    """The physical gate was acquired but its owner state could not be published."""


class HeavyTaskBusy(TimeoutError):
    """Structured admission failure for a currently occupied heavy-task gate."""

    def __init__(
        self,
        *,
        task_class: str,
        operation: str,
        origin: str,
        wait_timeout_seconds: float,
        gate_status: dict[str, Any],
    ) -> None:
        self.task_class = task_class
        self.operation = operation
        self.origin = origin
        self.wait_timeout_seconds = wait_timeout_seconds
        self.gate_status = gate_status
        owner = gate_status.get("current") or {}
        owner_operation = owner.get("operation") or "unknown"
        super().__init__(
            "Vector Lake heavy-task gate is busy; "
            f"requested={operation!r}, owner={owner_operation!r}, "
            f"wait_timeout_seconds={wait_timeout_seconds:g}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable machine-readable busy response."""

        return {
            "error": "heavy_task_busy",
            "requested": {
                "task_class": self.task_class,
                "operation": self.operation,
                "origin": self.origin,
                "wait_timeout_seconds": self.wait_timeout_seconds,
            },
            "gate": self.gate_status,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validated_text(value: object, *, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    if len(text) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return text


def _validated_timeout(
    value: float | int | None,
    *,
    field: str,
    allow_none: bool,
    minimum: float,
) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(parsed) or parsed < minimum:
        comparator = "non-negative" if minimum == 0 else f">= {minimum:g}"
        raise ValueError(f"{field} must be finite and {comparator}")
    return parsed


def _normalize_task_class(value: HeavyTaskClass | str) -> str:
    try:
        return HeavyTaskClass(value).value
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in HeavyTaskClass)
        raise ValueError(f"task_class must be one of: {allowed}") from exc


def _resolve_meta_root(meta_dir: str | os.PathLike[str] | None) -> Path:
    root = get_meta_dir() if meta_dir is None else Path(meta_dir).expanduser()
    return root.resolve()


def _manager_key(meta_root: Path) -> str:
    return os.path.normcase(str(meta_root))


def _scope_id(meta_root: Path) -> str:
    return hashlib.sha256(_manager_key(meta_root).encode("utf-8")).hexdigest()[:16]


def _read_json_object(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{exc.__class__.__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, "status payload is not a JSON object"
    return payload, None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove heavy-task status stage %s", temporary)


def _status_payload(
    *,
    meta_root: Path,
    current: dict[str, Any] | None,
    last: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": _STATUS_SCHEMA_VERSION,
        "scope_id": _scope_id(meta_root),
        "updated_at": _utc_now().isoformat(),
        "current": current,
        "last": last,
    }


def _computed_status(
    payload: dict[str, Any],
    *,
    meta_root: Path,
    physical_state: str,
    owned_by_current_thread: bool,
    status_error: str | None,
) -> dict[str, Any]:
    result = {
        "schema_version": payload.get(
            "schema_version", _STATUS_SCHEMA_VERSION
        ),
        "scope_id": payload.get("scope_id") or _scope_id(meta_root),
        "physical_state": physical_state,
        "owned_by_current_thread": owned_by_current_thread,
        "stale_metadata": False,
        "owner_unknown": False,
        "current": payload.get("current"),
        "last": payload.get("last"),
        "updated_at": payload.get("updated_at"),
    }
    if status_error:
        result["status_error"] = status_error

    current = result["current"]
    if physical_state == "free" and current:
        result["stale_metadata"] = True
    elif physical_state == "locked" and not current:
        result["owner_unknown"] = True

    if isinstance(current, dict):
        current = dict(current)
        acquired_at = current.get("acquired_at")
        try:
            acquired = datetime.fromisoformat(str(acquired_at))
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            acquired = None
        elapsed = (
            max(0.0, (_utc_now() - acquired).total_seconds())
            if acquired is not None
            else None
        )
        current["elapsed_seconds"] = elapsed
        warn_after = current.get("warn_after_seconds")
        current["overdue"] = bool(
            physical_state == "locked"
            and elapsed is not None
            and isinstance(warn_after, (int, float))
            and elapsed > float(warn_after)
        )
        result["current"] = current
    return result


class _HeavyTaskGate:
    def __init__(self, meta_root: Path) -> None:
        self.meta_root = meta_root
        self.meta_root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.meta_root / _LOCK_FILENAME
        self.status_path = self.meta_root / _STATUS_FILENAME
        self._file_lock = FileLock(str(self.lock_path))
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    def owned_by_current_thread(self) -> bool:
        return bool(getattr(self._local, "depth", 0))

    def status(self) -> dict[str, Any]:
        if self.owned_by_current_thread():
            payload, status_error = _read_json_object(self.status_path)
            return _computed_status(
                payload,
                meta_root=self.meta_root,
                physical_state="locked",
                owned_by_current_thread=True,
                status_error=status_error,
            )

        probe = FileLock(str(self.lock_path))
        try:
            probe.acquire(timeout=0)
        except FileLockTimeout:
            physical_state = "locked"
            payload, status_error = _read_json_object(self.status_path)
        else:
            try:
                payload, status_error = _read_json_object(self.status_path)
                physical_state = "free"
            finally:
                probe.release()
        return _computed_status(
            payload,
            meta_root=self.meta_root,
            physical_state=physical_state,
            owned_by_current_thread=False,
            status_error=status_error,
        )

    def _busy(
        self,
        *,
        task_class: str,
        operation: str,
        origin: str,
        wait_timeout_seconds: float,
    ) -> HeavyTaskBusy:
        return HeavyTaskBusy(
            task_class=task_class,
            operation=operation,
            origin=origin,
            wait_timeout_seconds=wait_timeout_seconds,
            gate_status=self.status(),
        )

    def acquire(
        self,
        *,
        task_class: str,
        operation: str,
        origin: str,
        wait_timeout_seconds: float,
        warn_after_seconds: float | None,
        requested_at: str,
        task_id: str,
    ) -> tuple[bool, str]:
        if self.owned_by_current_thread():
            self._thread_lock.acquire()
            self._local.depth += 1
            return True, str(self._local.task_id)

        started_wait = time.monotonic()
        deadline = started_wait + wait_timeout_seconds
        if not self._thread_lock.acquire(timeout=wait_timeout_seconds):
            raise self._busy(
                task_class=task_class,
                operation=operation,
                origin=origin,
                wait_timeout_seconds=wait_timeout_seconds,
            )

        file_acquired = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                self._file_lock.acquire(timeout=remaining)
            except FileLockTimeout as exc:
                raise self._busy(
                    task_class=task_class,
                    operation=operation,
                    origin=origin,
                    wait_timeout_seconds=wait_timeout_seconds,
                ) from exc
            file_acquired = True

            acquired = _utc_now()
            wait_seconds = max(0.0, time.monotonic() - started_wait)
            current = {
                "task_id": task_id,
                "task_class": task_class,
                "operation": operation,
                "origin": origin,
                "pid": os.getpid(),
                "process_token": _PROCESS_TOKEN,
                "process_started_at": _PROCESS_STARTED_AT,
                "host": platform.node(),
                "thread_name": threading.current_thread().name,
                "thread_ident": threading.get_ident(),
                "requested_at": requested_at,
                "acquired_at": acquired.isoformat(),
                "wait_seconds": wait_seconds,
                "wait_timeout_seconds": wait_timeout_seconds,
                "warn_after_seconds": warn_after_seconds,
                "soft_deadline_at": (
                    (acquired + timedelta(seconds=warn_after_seconds)).isoformat()
                    if warn_after_seconds is not None
                    else None
                ),
            }
            previous, _status_error = _read_json_object(self.status_path)
            last = previous.get("last")
            abandoned = previous.get("current")
            if isinstance(abandoned, dict):
                last = dict(abandoned)
                last.update(
                    {
                        "outcome": "abandoned",
                        "detected_at": acquired.isoformat(),
                    }
                )
            try:
                _atomic_write_json(
                    self.status_path,
                    _status_payload(
                        meta_root=self.meta_root,
                        current=current,
                        last=last if isinstance(last, dict) else None,
                    ),
                )
            except Exception as exc:
                raise HeavyTaskStateError(
                    "Heavy-task gate owner state could not be published"
                ) from exc

            self._local.depth = 1
            self._local.task_id = task_id
            self._local.current = current
            log.info(
                "Heavy task acquired: class=%s operation=%s origin=%s task_id=%s",
                task_class,
                operation,
                origin,
                task_id,
            )
            return False, task_id
        except BaseException:
            try:
                if file_acquired:
                    self._file_lock.release()
            finally:
                # The process-local lock must never remain stranded when the
                # OS-lock cleanup itself reports an error.
                self._thread_lock.release()
            raise

    def release(self, *, nested: bool, outcome: str, error: str | None) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth <= 0:
            raise RuntimeError("Heavy-task gate is not owned by the current thread")
        if nested:
            self._local.depth = depth - 1
            self._thread_lock.release()
            return
        if depth != 1:
            raise RuntimeError("Heavy-task gate nesting was released out of order")

        current = dict(getattr(self._local, "current", {}) or {})
        ended = _utc_now()
        acquired_at = current.get("acquired_at")
        try:
            acquired = datetime.fromisoformat(str(acquired_at))
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            acquired = None
        current.update(
            {
                "outcome": outcome,
                "ended_at": ended.isoformat(),
                "duration_seconds": (
                    max(0.0, (ended - acquired).total_seconds())
                    if acquired is not None
                    else None
                ),
                "error": error,
            }
        )
        try:
            try:
                _atomic_write_json(
                    self.status_path,
                    _status_payload(
                        meta_root=self.meta_root,
                        current=None,
                        last=current,
                    ),
                )
            except Exception:
                log.exception("Could not publish heavy-task completion state")
        finally:
            task_id = getattr(self._local, "task_id", "")
            self._local.depth = 0
            self._local.task_id = None
            self._local.current = None
            try:
                self._file_lock.release()
            finally:
                # Preserve in-process liveness even if FileLock.release()
                # raises after (or while) releasing the physical lock.
                self._thread_lock.release()
            log.info(
                "Heavy task released: task_id=%s outcome=%s",
                task_id,
                outcome,
            )


def _manager_for(
    meta_dir: str | os.PathLike[str] | None,
) -> _HeavyTaskGate:
    meta_root = _resolve_meta_root(meta_dir)
    key = _manager_key(meta_root)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = _HeavyTaskGate(meta_root)
            _MANAGERS[key] = manager
        return manager


@dataclass
class HeavyTaskLease:
    """Single-use context manager returned by :func:`heavy_task`."""

    task_class: str
    operation: str
    origin: str
    wait_timeout_seconds: float
    warn_after_seconds: float | None
    meta_dir: str | os.PathLike[str] | None
    task_id: str
    requested_at: str
    _manager: _HeavyTaskGate | None = None
    _entered: bool = False
    _nested: bool = False
    _used: bool = False

    def __enter__(self) -> "HeavyTaskLease":
        if self._entered or self._used:
            raise RuntimeError("HeavyTaskLease instances are single-use")
        manager = _manager_for(self.meta_dir)
        nested, effective_task_id = manager.acquire(
            task_class=self.task_class,
            operation=self.operation,
            origin=self.origin,
            wait_timeout_seconds=self.wait_timeout_seconds,
            warn_after_seconds=self.warn_after_seconds,
            requested_at=self.requested_at,
            task_id=self.task_id,
        )
        self._manager = manager
        self._nested = nested
        self.task_id = effective_task_id
        self._entered = True
        self._used = True
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if not self._entered or self._manager is None:
            raise RuntimeError("HeavyTaskLease was not entered")
        outcome = "completed" if exc_type is None else "failed"
        error = None if exc is None else f"{exc.__class__.__name__}: {exc}"
        self._manager.release(
            nested=self._nested,
            outcome=outcome,
            error=error,
        )
        self._entered = False
        return False


def heavy_task(
    task_class: HeavyTaskClass | str,
    operation: str,
    *,
    origin: str,
    wait_timeout_seconds: float = 0.0,
    warn_after_seconds: float | None = None,
    meta_dir: str | os.PathLike[str] | None = None,
) -> HeavyTaskLease:
    """Create a single-use admission lease for one heavy operation.

    ``wait_timeout_seconds`` is a hard admission timeout.  The optional
    ``warn_after_seconds`` is observability-only; it never steals an active
    operating-system lock or terminates a running task.
    """

    normalized_class = _normalize_task_class(task_class)
    normalized_operation = _validated_text(
        operation, field="operation", max_length=200
    )
    normalized_origin = _validated_text(origin, field="origin", max_length=64)
    normalized_wait = _validated_timeout(
        wait_timeout_seconds,
        field="wait_timeout_seconds",
        allow_none=False,
        minimum=0.0,
    )
    normalized_warn = _validated_timeout(
        warn_after_seconds,
        field="warn_after_seconds",
        allow_none=True,
        minimum=0.001,
    )
    assert normalized_wait is not None
    return HeavyTaskLease(
        task_class=normalized_class,
        operation=normalized_operation,
        origin=normalized_origin,
        wait_timeout_seconds=float(normalized_wait),
        warn_after_seconds=normalized_warn,
        meta_dir=meta_dir,
        task_id=uuid.uuid4().hex,
        requested_at=_utc_now().isoformat(),
    )


def heavy_task_gate_status(
    *,
    meta_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a non-blocking physical-lock and owner-state snapshot."""
    meta_root = (
        peek_meta_dir().resolve()
        if meta_dir is None
        else Path(meta_dir).expanduser().resolve()
    )
    lock_path = meta_root / _LOCK_FILENAME
    if not lock_path.exists():
        payload, status_error = _read_json_object(meta_root / _STATUS_FILENAME)
        status = _computed_status(
            payload,
            meta_root=meta_root,
            physical_state="free",
            owned_by_current_thread=False,
            status_error=status_error,
        )
        status["initialized"] = meta_root.exists()
        return status
    status = _manager_for(meta_root).status()
    status["initialized"] = True
    return status


__all__ = [
    "HeavyTaskBusy",
    "HeavyTaskClass",
    "HeavyTaskLease",
    "HeavyTaskStateError",
    "heavy_task",
    "heavy_task_gate_status",
]
