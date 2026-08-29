import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from vector_lake.wiki_utils import get_meta_dir
from vector_lake.durability import durable_replace_file, sync_open_file

_status_lock = threading.Lock()
log = logging.getLogger("vector-lake-watchdog-status")

_run_id = uuid.uuid4().hex
_run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
_expected_components: tuple[str, ...] = ()
_cached_status_path: Path | None = None
_cached_status_identity: tuple[int, int, int, int] | None = None
_cached_status_document: dict | None = None
_component_published_at: dict[str, float] = {}


def _monotonic_now() -> float:
    return time.monotonic()


def _heartbeat_interval_seconds() -> float:
    try:
        value = float(
            os.environ.get("VECTOR_LAKE_WATCHDOG_STATUS_HEARTBEAT_SECONDS", "30")
        )
    except (TypeError, ValueError):
        value = 30.0
    if not 1.0 <= value <= 60.0:
        value = 30.0
    return value


def current_watchdog_run_id() -> str:
    """Return the process-local watchdog generation token."""
    return _run_id

def get_status_file() -> Path:
    return get_meta_dir() / ".watchdog_status.json"


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _process_is_alive(process_id: object) -> bool:
    try:
        pid = int(process_id)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    """Query a Windows process without relying on unsupported signal 0."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(handle, ctypes.byref(exit_code)):
            return ctypes.get_last_error() == error_access_denied
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _component_payload(
    state: str,
    task_queue_size: int,
    index_queue_size: int,
    current_action: str,
    last_error: str,
    *,
    now: str,
) -> dict:
    return {
        "status": state,
        "task_queue_size": task_queue_size,
        "index_queue_size": index_queue_size,
        "current_action": current_action,
        "last_error": last_error,
        "updated_at": now,
        "heartbeat_at": now,
        "process_id": os.getpid(),
        "run_id": _run_id,
        "thread_name": threading.current_thread().name,
        "thread_ident": threading.get_ident(),
    }


def _status_document(
    components: dict[str, dict],
    *,
    now: str,
    task_queue_size: int,
    index_queue_size: int,
) -> dict:
    priority = {
        "halted": 6,
        "stopped": 5,
        "error": 4,
        "draining": 3,
        "processing": 2,
        "starting": 1,
        "idle": 0,
        "disabled": -1,
    }
    aggregate = max(
        components.values(),
        key=lambda item: priority.get(str(item.get("status", "idle")), 0),
    )
    return {
        "schema_version": 3,
        "run_id": _run_id,
        "process_id": os.getpid(),
        "started_at": _run_started_at,
        "expected_components": list(_expected_components),
        "status": aggregate.get("status", "idle"),
        "task_queue_size": task_queue_size,
        "index_queue_size": index_queue_size,
        "current_action": aggregate.get("current_action", ""),
        "last_error": aggregate.get("last_error", ""),
        "updated_at": now,
        "components": components,
    }


def _publish_locked(status_file: Path, data: dict) -> bool:
    global _cached_status_document, _cached_status_identity, _cached_status_path

    last_exception: PermissionError | None = None
    for _attempt in range(5):
        temp_file = status_file.with_name(f".watchdog_status_{uuid.uuid4().hex}.tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                sync_open_file(handle)
            durable_replace_file(temp_file, status_file, source_synced=True)
            status = status_file.stat()
            _cached_status_path = status_file
            _cached_status_identity = (
                int(status.st_dev),
                int(status.st_ino),
                int(status.st_size),
                int(status.st_mtime_ns),
            )
            _cached_status_document = data
            return True
        except PermissionError as exc:
            last_exception = exc
            time.sleep(0.1)
        except Exception:
            log.exception("Failed to publish watchdog status to %s", status_file)
            return False
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception as exc:
                    log.warning(
                        "Failed to remove watchdog status temp file %s: %s",
                        temp_file,
                        exc,
                    )
    log.error(
        "Failed to publish watchdog status to %s after retries: %s",
        status_file,
        last_exception,
    )
    return False


def _status_identity(status_file: Path) -> tuple[int, int, int, int] | None:
    try:
        if status_file.is_symlink():
            raise RuntimeError("watchdog status path must not be a symbolic link")
        status = status_file.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(
            f"watchdog status identity is unavailable: {type(exc).__name__}"
        ) from exc
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_size),
        int(status.st_mtime_ns),
    )


def _existing_status_locked(status_file: Path) -> tuple[dict, bool]:
    """Return the status plus whether the durable identity changed externally."""
    global _cached_status_document, _cached_status_identity, _cached_status_path

    identity = _status_identity(status_file)
    if (
        _cached_status_path == status_file
        and _cached_status_document is not None
        and _cached_status_identity == identity
    ):
        return _cached_status_document, False
    existing: dict = {}
    if identity is not None:
        try:
            loaded = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            existing = loaded
    _cached_status_path = status_file
    _cached_status_identity = identity
    _cached_status_document = existing
    _component_published_at.clear()
    return existing, True


def begin_watchdog_run(expected_components: tuple[str, ...] | list[str]) -> str:
    """Atomically fence prior status generations and publish the startup set.

    The singleton lock must already be held by the caller. Publishing the whole
    expected component inventory in one replace prevents a partially-started
    process from looking like a healthy continuation of an older PID.
    """
    global _expected_components, _run_id, _run_started_at

    normalized = tuple(dict.fromkeys(str(item) for item in expected_components))
    if not normalized:
        raise ValueError("Watchdog expected_components cannot be empty")
    status_file = get_status_file()
    status_file.parent.mkdir(parents=True, exist_ok=True)
    with _status_lock:
        existing = {}
        if status_file.exists():
            try:
                existing = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing_pid = existing.get("process_id")
        existing_run = str(existing.get("run_id") or "")
        existing_components = existing.get("components") or {}
        nonterminal_foreign = any(
            str((detail or {}).get("status") or "").casefold()
            not in {"stopped", "halted"}
            for detail in existing_components.values()
            if isinstance(detail, dict)
        )
        if (
            existing_run
            and existing_pid != os.getpid()
            and nonterminal_foreign
            and _process_is_alive(existing_pid)
        ):
            raise RuntimeError(
                "Refusing a new watchdog generation while the prior status owner "
                f"is still alive: run={existing_run} pid={existing_pid}"
            )
        _run_id = uuid.uuid4().hex
        _run_started_at = _now_utc()
        _expected_components = normalized
        components = {
            component: _component_payload(
                "starting",
                0,
                0,
                "Watchdog component awaiting startup",
                "",
                now=_run_started_at,
            )
            for component in normalized
        }
        data = _status_document(
            components,
            now=_run_started_at,
            task_queue_size=0,
            index_queue_size=0,
        )
        if not _publish_locked(status_file, data):
            raise RuntimeError("Could not publish the atomic watchdog startup status")
        published_at = _monotonic_now()
        _component_published_at.clear()
        _component_published_at.update(
            {component: published_at for component in normalized}
        )
    return _run_id

def write_status(
    state: str,
    task_queue_size: int,
    index_queue_size: int,
    current_action: str = "",
    last_error: str = "",
    component: str = "watchdog",
) -> bool:
    status_file = get_status_file()
    status_file.parent.mkdir(parents=True, exist_ok=True)

    with _status_lock:
        if _expected_components and component not in _expected_components:
            log.error(
                "Rejected unexpected watchdog component %s for run %s",
                component,
                _run_id,
            )
            return False
        try:
            existing, durable_identity_changed = _existing_status_locked(status_file)
        except RuntimeError as exc:
            log.error("Rejected unsafe watchdog status path: %s", exc)
            return False
        existing_run = str(existing.get("run_id") or "")
        existing_pid = existing.get("process_id")
        if existing_run and (
            existing_run != _run_id or existing_pid != os.getpid()
        ):
            log.error(
                "Rejected watchdog status write from run %s pid %s because "
                "the durable owner is run %s pid %s",
                _run_id,
                os.getpid(),
                existing_run,
                existing_pid,
            )
            return False
        now = _now_utc()
        components = {
            name: detail
            for name, detail in (
                (existing.get("components") or {}).items()
                if existing_run == _run_id
                else ()
            )
            if isinstance(detail, dict)
            and detail.get("run_id") == _run_id
            and detail.get("process_id") == os.getpid()
            and (not _expected_components or name in _expected_components)
        }
        next_component = _component_payload(
            state,
            task_queue_size,
            index_queue_size,
            current_action,
            last_error,
            now=now,
        )
        previous_component = components.get(component)
        substantive_fields = (
            "status",
            "task_queue_size",
            "index_queue_size",
            "current_action",
            "last_error",
            "process_id",
            "run_id",
            "thread_name",
            "thread_ident",
        )
        unchanged = isinstance(previous_component, dict) and all(
            previous_component.get(field) == next_component.get(field)
            for field in substantive_fields
        )
        now_monotonic = _monotonic_now()
        last_published = _component_published_at.get(component)
        if (
            unchanged
            and not durable_identity_changed
            and last_published is not None
            and now_monotonic - last_published < _heartbeat_interval_seconds()
        ):
            return True
        components[component] = next_component
        data = _status_document(
            components,
            now=now,
            task_queue_size=task_queue_size,
            index_queue_size=index_queue_size,
        )
        published = _publish_locked(status_file, data)
        if published:
            _component_published_at[component] = now_monotonic
        return published
