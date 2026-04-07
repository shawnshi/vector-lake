import time
import os
import sys
import logging
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path

# Ensure we can import local modules
EXTENSION_ROOT = Path(__file__).parent
if str(EXTENSION_ROOT) not in sys.path:
    sys.path.insert(0, str(EXTENSION_ROOT))

from db import get_sqlite_db, get_chroma_client
from ingest import process_file
from google import genai
from embedding import GeminiEmbeddingFunction

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-watchdog")

class SyncHandler(FileSystemEventHandler):
    def __init__(self, sqlite_db, collection, genai_client):
        self.sqlite_db = sqlite_db
        self.collection = collection
        self.genai_client = genai_client
        self.supported_exts = {".md", ".txt", ".png", ".jpg", ".jpeg", ".pdf"}

    def on_modified(self, event):
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle_change(event.src_path)

    def _handle_change(self, filepath):
        ext = os.path.splitext(filepath)[1].lower()
        if ext in self.supported_exts:
            filename = os.path.basename(filepath)
            # Skip temporary or hidden files
            if filename.startswith('.') or filename.startswith('~'):
                return
            log.info(f"Change detected: {filepath}")
            try:
                # process_file handles hash checking internally to prevent redundant work
                did_work = process_file(filepath, self.sqlite_db, self.collection, self.genai_client)
                if did_work:
                    log.info(f"Successfully synced: {filepath}")
            except Exception as e:
                log.error(f"Error syncing {filepath}: {e}")

def main():
    log.info("Starting Vector Lake Watchdog Service...")
    
    # Initialize shared resources for the watchdog
    db = get_sqlite_db()
    chroma_client = get_chroma_client()
    embedding_func = GeminiEmbeddingFunction()
    collection = chroma_client.get_or_create_collection(
        name="vector_lake",
        embedding_function=embedding_func
    )
    genai_client = genai.Client()

    # Load targets from config
    CONFIG_PATH = EXTENSION_ROOT / "config.json"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return

    handler = SyncHandler(db, collection, genai_client)
    observer = Observer()
    
    targets = config.get("target_directories", [])
    for t in targets:
        # Resolve target path relative to extension root
        path = (EXTENSION_ROOT / t).resolve()
        if path.exists():
            log.info(f"Watching: {path}")
            observer.schedule(handler, str(path), recursive=True)
        else:
            log.warning(f"Watch target does not exist: {path}")

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    db.close()

if __name__ == "__main__":
    main()
