import os
import json
from pathlib import Path
from filelock import FileLock, Timeout
from vector_lake import get_extension_root

# Load config
CONFIG_PATH = get_extension_root() / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

EXTENSION_ROOT = get_extension_root()
PROCESSED_FILES_PATH = str(EXTENSION_ROOT / config.get("processed_files_path", "data/processed_files.json"))

# Cross-process lock to prevent concurrent writes to processed_files.json
json_lock = FileLock(f"{PROCESSED_FILES_PATH}.lock", timeout=30)

def get_processed_files() -> dict:
    with json_lock:
        if not os.path.exists(PROCESSED_FILES_PATH):
            return {}
        try:
            with open(PROCESSED_FILES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

def mark_file_processed(filepath: str, file_hash: str, timestamp: str):
    with json_lock:
        os.makedirs(os.path.dirname(PROCESSED_FILES_PATH), exist_ok=True)
        data = {}
        if os.path.exists(PROCESSED_FILES_PATH):
            try:
                with open(PROCESSED_FILES_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data[filepath] = {"hash": file_hash, "processed_at": timestamp}
        with open(PROCESSED_FILES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

