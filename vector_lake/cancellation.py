"""Cooperative cancellation state for synchronous MCP tool execution."""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


CANCELLATION_CONTRACT_VERSION = "vector-lake-mcp-cancellation-v1"
TERMINAL_OPERATION_STATUSES = frozenset(
    {
        "cancelled",
        "completed",
        "completed_after_cancellation",
        "failed",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RequestDeadline:
    deadline_monotonic: float
    deadline_seconds: float
    deadline_at: str


_REQUEST_DEADLINE: contextvars.ContextVar[RequestDeadline | None] = (
    contextvars.ContextVar("vector_lake_mcp_request_deadline", default=None)
)
_CURRENT_OPERATION: contextvars.ContextVar[CancellationOperation | None] = (
    contextvars.ContextVar("vector_lake_mcp_cancellation_operation", default=None)
)


class CooperativeCancellation(RuntimeError):
    """Raised by a cooperative checkpoint before an atomic phase starts."""

    def __init__(self, operation: CancellationOperation) -> None:
        snapshot = operation.snapshot()
        reason = snapshot["cancellation_reason"]
        if reason == "deadline_exceeded":
            summary = "Vector Lake MCP tool deadline exceeded"
        else:
            summary = "Vector Lake MCP tool cancelled"
        super().__init__(
            f"{summary}; operation_id={snapshot['operation_id']}; "
            f"status={snapshot['status']}"
        )
        self.operation_id = operation.operation_id


class ToolDeadlineExceeded(RuntimeError):
    """Raised to the MCP request while cooperative worker cleanup continues."""

    def __init__(self, operation: CancellationOperation) -> None:
        snapshot = operation.snapshot()
        super().__init__(
            "Vector Lake MCP tool deadline exceeded; "
            f"operation_id={snapshot['operation_id']}; "
            f"status={snapshot['status']}; "
            f"detached={str(snapshot['detached']).lower()}"
        )
        self.operation_id = operation.operation_id


class CancellationOperation:
    """Thread-safe lifecycle and cancellation token for one synchronous tool."""

    def __init__(
        self,
        *,
        tool_name: str,
        lane: str,
        deadline: RequestDeadline | None,
    ) -> None:
        self.operation_id = f"op_{uuid.uuid4().hex}"
        self.tool_name = str(tool_name)
        self.lane = str(lane)
        self.created_at = _utc_now()
        self._created_monotonic = time.monotonic()
        self._deadline = deadline
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._status = "queued"
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._finished_monotonic: float | None = None
        self._cancellation_requested_at: str | None = None
        self._cancellation_reason: str | None = None
        self._detached = False
        self._phase: str | None = None
        self._atomic_depth = 0
        self._atomic_phase_started = False
        self._checkpoints = 0
        self._last_checkpoint: str | None = None
        self._last_checkpoint_at: str | None = None
        self._error_type: str | None = None

    @property
    def terminal(self) -> bool:
        with self._lock:
            return self._status in TERMINAL_OPERATION_STATUSES

    @property
    def finished_monotonic(self) -> float | None:
        with self._lock:
            return self._finished_monotonic

    def remaining_seconds(self) -> float | None:
        if self._deadline is None:
            return None
        return self._deadline.deadline_monotonic - time.monotonic()

    def deadline_expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def mark_running(self) -> None:
        with self._lock:
            if self._status in TERMINAL_OPERATION_STATUSES:
                return
            if self._started_at is None:
                self._started_at = _utc_now()
            if self._cancel_event.is_set():
                self._status = (
                    "cancellation_pending"
                    if self._atomic_depth > 0
                    else "cancel_requested"
                )
            else:
                self._status = "running"

    def request_cancellation(self, reason: str, *, detached: bool) -> None:
        with self._lock:
            if self._status in TERMINAL_OPERATION_STATUSES:
                return
            if self._cancellation_reason is None:
                self._cancellation_reason = str(reason)
                self._cancellation_requested_at = _utc_now()
            self._cancel_event.set()
            self._detached = bool(self._detached or detached)
            self._status = (
                "cancellation_pending"
                if self._atomic_depth > 0
                else "cancel_requested"
            )

    def cancel_before_start(self, reason: str) -> None:
        with self._lock:
            if self._status in TERMINAL_OPERATION_STATUSES:
                return
            if self._started_at is not None:
                self.request_cancellation(reason, detached=True)
                return
            self._cancel_event.set()
            self._cancellation_reason = str(reason)
            self._cancellation_requested_at = _utc_now()
            self._status = "cancelled"
            self._finished_at = _utc_now()
            self._finished_monotonic = time.monotonic()

    def checkpoint(self, label: str = "checkpoint") -> dict:
        if self.deadline_expired() and not self._cancel_event.is_set():
            self.request_cancellation("deadline_exceeded", detached=True)
        with self._lock:
            self._checkpoints += 1
            self._last_checkpoint = str(label)
            self._last_checkpoint_at = _utc_now()
            if not self._cancel_event.is_set():
                return self._snapshot_locked()
            if self._atomic_depth > 0:
                self._status = "cancellation_pending"
                return self._snapshot_locked()
            self._status = "cancelled"
            self._finished_at = self._finished_at or _utc_now()
            self._finished_monotonic = self._finished_monotonic or time.monotonic()
        raise CooperativeCancellation(self)

    def begin_atomic_phase(self, phase: str) -> None:
        with self._lock:
            self.checkpoint(f"before_atomic:{phase}")
            self._atomic_depth += 1
            self._atomic_phase_started = True
            self._phase = str(phase)

    def end_atomic_phase(self) -> None:
        with self._lock:
            self._atomic_depth = max(0, self._atomic_depth - 1)
            if self._atomic_depth == 0:
                self._phase = None
            if self._cancel_event.is_set():
                self._status = "cancellation_pending"

    def mark_completed(self) -> None:
        with self._lock:
            if self._status == "cancelled":
                return
            self._status = (
                "completed_after_cancellation"
                if self._cancel_event.is_set()
                else "completed"
            )
            self._phase = None
            self._finished_at = _utc_now()
            self._finished_monotonic = time.monotonic()

    def mark_failed(self, exc: BaseException) -> None:
        with self._lock:
            if self._status == "cancelled":
                return
            self._status = "failed"
            self._phase = None
            self._error_type = type(exc).__name__
            self._finished_at = _utc_now()
            self._finished_monotonic = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        deadline_seconds = (
            None if self._deadline is None else self._deadline.deadline_seconds
        )
        deadline_at = None if self._deadline is None else self._deadline.deadline_at
        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "lane": self.lane,
            "status": self._status,
            "terminal": self._status in TERMINAL_OPERATION_STATUSES,
            "created_at": self.created_at,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "deadline_seconds": deadline_seconds,
            "deadline_at": deadline_at,
            "cancellation_requested_at": self._cancellation_requested_at,
            "cancellation_reason": self._cancellation_reason,
            "cancellation_pending": self._status == "cancellation_pending",
            "detached": self._detached,
            "phase": self._phase,
            "atomic_phase_started": self._atomic_phase_started,
            "atomic_phase_active": self._atomic_depth > 0,
            "checkpoints": self._checkpoints,
            "last_checkpoint": self._last_checkpoint,
            "last_checkpoint_at": self._last_checkpoint_at,
            "error_type": self._error_type,
        }


class CancellationRegistry:
    """Bounded per-server registry for active and recently finished operations."""

    def __init__(
        self,
        *,
        max_operations: int = 256,
        retention_seconds: float = 3600.0,
    ) -> None:
        self.max_operations = max(16, int(max_operations))
        self.retention_seconds = max(60.0, float(retention_seconds))
        self._lock = threading.Lock()
        self._operations: OrderedDict[str, CancellationOperation] = OrderedDict()

    def _prune_expired_locked(self) -> None:
        cutoff = time.monotonic() - self.retention_seconds
        for operation_id, operation in tuple(self._operations.items()):
            finished = operation.finished_monotonic
            if finished is not None and finished < cutoff:
                self._operations.pop(operation_id, None)

    def _ensure_capacity_locked(self) -> None:
        while len(self._operations) >= self.max_operations:
            removable = next(
                (
                    operation_id
                    for operation_id, operation in self._operations.items()
                    if operation.terminal
                ),
                None,
            )
            if removable is None:
                raise RuntimeError("MCP cancellation operation registry is saturated")
            self._operations.pop(removable, None)

    def create(
        self,
        *,
        tool_name: str,
        lane: str,
        deadline: RequestDeadline | None,
    ) -> CancellationOperation:
        operation = CancellationOperation(
            tool_name=tool_name,
            lane=lane,
            deadline=deadline,
        )
        with self._lock:
            self._prune_expired_locked()
            self._ensure_capacity_locked()
            self._operations[operation.operation_id] = operation
        return operation

    def snapshot(self, operation_id: str = "", *, limit: int = 20) -> dict:
        operation_id = str(operation_id or "").strip()
        limit = max(1, min(100, int(limit)))
        with self._lock:
            self._prune_expired_locked()
            if operation_id:
                operation = self._operations.get(operation_id)
                return {
                    "found": operation is not None,
                    "operation": None if operation is None else operation.snapshot(),
                }
            operations = list(self._operations.values())
        recent = [operation.snapshot() for operation in reversed(operations[-limit:])]
        return {
            "active_count": sum(not operation.terminal for operation in operations),
            "retained_count": len(operations),
            "operations": recent,
        }


def make_request_deadline(deadline_seconds: float | None) -> RequestDeadline | None:
    if deadline_seconds is None:
        return None
    now = datetime.now(timezone.utc)
    return RequestDeadline(
        deadline_monotonic=time.monotonic() + deadline_seconds,
        deadline_seconds=deadline_seconds,
        deadline_at=(now + timedelta(seconds=deadline_seconds)).isoformat(),
    )


@contextmanager
def request_deadline_scope(deadline_seconds: float | None):
    token = _REQUEST_DEADLINE.set(make_request_deadline(deadline_seconds))
    try:
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)


def current_request_deadline() -> RequestDeadline | None:
    return _REQUEST_DEADLINE.get()


@contextmanager
def bind_cancellation_operation(operation: CancellationOperation):
    token = _CURRENT_OPERATION.set(operation)
    try:
        yield operation
    finally:
        _CURRENT_OPERATION.reset(token)


def current_cancellation_operation() -> CancellationOperation | None:
    return _CURRENT_OPERATION.get()


def current_operation_id() -> str:
    operation = current_cancellation_operation()
    return "" if operation is None else operation.operation_id


def cancellation_checkpoint(label: str = "checkpoint") -> dict:
    operation = current_cancellation_operation()
    if operation is None:
        return {
            "operation_id": "",
            "status": "unmanaged",
            "terminal": False,
        }
    return operation.checkpoint(label)


@contextmanager
def non_interruptible_phase(phase: str):
    operation = current_cancellation_operation()
    if operation is None:
        yield
        return
    operation.begin_atomic_phase(phase)
    try:
        yield
    finally:
        operation.end_atomic_phase()
