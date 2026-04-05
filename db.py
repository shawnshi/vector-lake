import os
import json
from pathlib import Path
from threading import Lock

# Load config
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

EXTENSION_ROOT = Path(__file__).parent
CHROMA_DB_PATH = str(EXTENSION_ROOT / config.get("db_path_chroma", "data/vector_lake_db"))
PROCESSED_FILES_PATH = str(EXTENSION_ROOT / config.get("processed_files_path", "data/processed_files.json"))

_processed_lock = Lock()

def get_chroma_client():
    """Gets a connection to the ChromaDB Vector Lake."""
    import chromadb
    os.makedirs(CHROMA_DB_PATH, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    return client

def get_processed_files() -> dict:
    with _processed_lock:
        if not os.path.exists(PROCESSED_FILES_PATH):
            return {}
        try:
            with open(PROCESSED_FILES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

def mark_file_processed(filepath: str, file_hash: str, timestamp: str):
    with _processed_lock:
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
