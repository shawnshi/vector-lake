import json
import time
import threading
import uuid
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir

_status_lock = threading.Lock()

def get_status_file() -> Path:
    return get_meta_dir() / ".watchdog_status.json"

def write_status(state: str, task_queue_size: int, index_queue_size: int, current_action: str = "", last_error: str = ""):
    status_file = get_status_file()
    data = {
        "status": state,
        "task_queue_size": task_queue_size,
        "index_queue_size": index_queue_size,
        "current_action": current_action,
        "last_error": last_error,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    status_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = status_file.with_name(f".watchdog_status_{uuid.uuid4().hex}.tmp")
    
    with _status_lock:
        for attempt in range(5):
            try:
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                temp_file.replace(status_file)
                break
            except PermissionError:
                time.sleep(0.1)
            except Exception:
                break
            finally:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
