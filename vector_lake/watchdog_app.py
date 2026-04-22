import logging
import os
import queue
import threading
import time
from pathlib import Path

import json
from pathlib import Path
from vector_lake import get_extension_root

# Load config
CONFIG_PATH = get_extension_root() / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

EXCLUDE_PATHS = config.get("exclude_paths", [])

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print("Error: `watchdog` library is not installed. Please run `pip install watchdog`.", flush=True)
    import sys
    sys.exit(1)

from vector_lake.ingest import TARGET_DIRS, SUPPORTED_EXTS, calculate_hash, process_file_batch
from vector_lake.db import get_processed_files


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("watchdog_sync")

DEBOUNCE_SECONDS = 3.0
task_queue = queue.Queue()
index_queue = queue.Queue()


class WikiIndexHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()

    def update_index(self, event):
        if event.is_directory:
            return

        filepath = os.path.abspath(event.src_path)
        filename = os.path.basename(filepath)
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
            return

        now = time.time()
        with self.lock:
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now

        index_queue.put(filename)

    def on_created(self, event):
        self.update_index(event)

    def on_modified(self, event):
        self.update_index(event)


class VectorLakeIngestHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()

    def _should_ignore(self, filepath):
        filename = os.path.basename(filepath)
        if filename.startswith("~") or filename.startswith("."):
            return True
            
        # Ignore specific directories configured in config.json
        path_str = filepath.replace("\\", "/")
        for exclude in EXCLUDE_PATHS:
            if exclude in path_str:
                return True
            
        _, ext = os.path.splitext(filename)
        return ext.lower() not in SUPPORTED_EXTS

    def process_event(self, event):
        if event.is_directory:
            return

        filepath = os.path.abspath(event.src_path)
        if self._should_ignore(filepath):
            return

        now = time.time()
        with self.lock:
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now

        log.info(f"Triggered ({event.event_type}): {filepath}")

        def delayed_commit():
            time.sleep(DEBOUNCE_SECONDS)
            file_hash = calculate_hash(filepath)
            if not file_hash:
                return

            processed = get_processed_files()
            if filepath in processed and processed[filepath].get("hash") == file_hash:
                filename = os.path.basename(filepath)
                log.info(f"File '{filename}' content hasn't changed (hash match). Skipping ingestion.")
                return

            task_queue.put(filepath)

        threading.Thread(target=delayed_commit, daemon=True).start()

    def on_created(self, event):
        self.process_event(event)

    def on_modified(self, event):
        self.process_event(event)


def worker_loop():
    log.info("Ingestion Worker Thread started.")
    batch_limit = 5
    while True:
        try:
            item = task_queue.get()
            batch = [item]
            while len(batch) < batch_limit:
                try:
                    next_item = task_queue.get_nowait()
                    if next_item not in batch:
                        batch.append(next_item)
                except queue.Empty:
                    break

            log.info(f"Worker dequeued a batch of {len(batch)} files for ingestion.")
            success = process_file_batch(batch)
            if success:
                log.info("Batch processed successfully.")
            else:
                log.error("Failed to process batch.")
            for _ in batch:
                task_queue.task_done()
        except Exception as exc:
            log.error(f"Worker thread error: {exc}")
            time.sleep(2)


def index_worker_loop():
    log.info("Index Update Worker Thread started.")
    while True:
        try:
            filename = index_queue.get()
            time.sleep(DEBOUNCE_SECONDS)

            pending_filenames = {filename}
            while not index_queue.empty():
                try:
                    peek = index_queue.get_nowait()
                    pending_filenames.add(peek)
                    index_queue.task_done()
                except queue.Empty:
                    break

            from vector_lake import indexer

            for fname in pending_filenames:
                indexer.update_index_item(fname)
                log.info(f"O(1) Index updated for modified wiki node: {fname}")
            if indexer.refresh_graph_topology_if_dirty():
                log.info("Graph topology refreshed after partial wiki updates.")

            index_queue.task_done()
        except Exception as exc:
            log.error(f"Index worker error: {exc}")
            time.sleep(1)


def start_watchdog():
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=index_worker_loop, daemon=True).start()

    event_handler = VectorLakeIngestHandler()
    observer = Observer()

    watch_count = 0
    for target_dir in TARGET_DIRS:
        folder = Path(target_dir)
        if folder.exists() and folder.is_dir():
            observer.schedule(event_handler, str(folder), recursive=True)
            log.info(f"Sentry active on directory: {folder}")
            watch_count += 1
        else:
            log.warning(f"Target directory does not exist or is not a folder: {folder}")

    if watch_count == 0:
        log.error("No valid TARGET_DIRS to monitor. Sentinel terminating.")
        return

    from vector_lake.wiki_utils import get_wiki_dir

    wiki_dir = str(get_wiki_dir())
    if os.path.exists(wiki_dir):
        wiki_handler = WikiIndexHandler()
        observer.schedule(wiki_handler, wiki_dir, recursive=False)
        log.info(f"Wiki AST monitor active on directory: {wiki_dir}")

    observer.start()
    log.info("Vector Lake Watchdog Agent is now running in YOLO background mode.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Termination signal received. Shutting down Watchdog...")
        observer.stop()
    observer.join()
