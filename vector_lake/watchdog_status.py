import json
import logging
import os
import time
import threading
import uuid
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir

_status_lock = threading.Lock()
log = logging.getLogger("vector-lake-watchdog-status")

def get_status_file() -> Path:
    return get_meta_dir() / ".watchdog_status.json"

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
    temp_file = status_file.with_name(f".watchdog_status_{uuid.uuid4().hex}.tmp")
    
    with _status_lock:
        for attempt in range(5):
            try:
                existing = {}
                if status_file.exists():
                    try:
                        existing = json.loads(status_file.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        existing = {}
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                components = dict(existing.get("components") or {})
                components[component] = {
                    "status": state,
                    "task_queue_size": task_queue_size,
                    "index_queue_size": index_queue_size,
                    "current_action": current_action,
                    "last_error": last_error,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "process_id": os.getpid(),
                    "thread_name": threading.current_thread().name,
                    "thread_ident": threading.get_ident(),
                }
                priority = {"halted": 4, "stopped": 3, "error": 2, "processing": 1, "idle": 0}
                aggregate = max(
                    components.values(),
                    key=lambda item: priority.get(str(item.get("status", "idle")), 0),
                )
                data = {
                    "schema_version": 2,
                    "status": aggregate.get("status", "idle"),
                    "task_queue_size": task_queue_size,
                    "index_queue_size": index_queue_size,
                    "current_action": aggregate.get("current_action", ""),
                    "last_error": aggregate.get("last_error", ""),
                    "updated_at": now,
                    "components": components,
                }
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
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
                        log.warning("Failed to remove watchdog status temp file %s: %s", temp_file, exc)
        log.error(
            "Failed to publish watchdog status to %s after retries: %s",
            status_file,
            last_exception,
        )
        return False
