import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from vector_lake.wiki_utils import get_meta_dir

_status_lock = threading.Lock()
log = logging.getLogger("vector-lake-watchdog-status")

_run_id = uuid.uuid4().hex
_run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
_expected_components: tuple[str, ...] = ()


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
    last_exception: PermissionError | None = None
    for _attempt in range(5):
        temp_file = status_file.with_name(f".watchdog_status_{uuid.uuid4().hex}.tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            temp_file.replace(status_file)
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
        existing = {}
        if status_file.exists():
            try:
                existing = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
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
        components[component] = _component_payload(
            state,
            task_queue_size,
            index_queue_size,
            current_action,
            last_error,
            now=now,
        )
        data = _status_document(
            components,
            now=now,
            task_queue_size=task_queue_size,
            index_queue_size=index_queue_size,
        )
        return _publish_locked(status_file, data)
