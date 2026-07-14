import logging
import os
import queue
import threading
import time
from pathlib import Path
import json
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


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("watchdog_sync")

DEBOUNCE_SECONDS = 3.0
index_queue = queue.Queue()
global_task_lock = threading.Lock()


class WikiIndexHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()

    def queue_path(self, filepath_str: str):
        filepath = os.path.abspath(filepath_str)
        filename = os.path.basename(filepath)
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
            return

        now = time.time()
        with self.lock:
            if len(self.last_triggered) > 1000:
                self.last_triggered = {k: v for k, v in self.last_triggered.items() if (now - v) <= DEBOUNCE_SECONDS * 2}
            
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now

        index_queue.put(filename)

    def update_index(self, event):
        if event.is_directory:
            return
        self.queue_path(event.src_path)

    def on_created(self, event):
        self.update_index(event)

    def on_modified(self, event):
        self.update_index(event)

    def on_deleted(self, event):
        self.update_index(event)

    def on_moved(self, event):
        if event.is_directory:
            return
        self.queue_path(event.src_path)
        self.queue_path(event.dest_path)


class DiaryWatchdogHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()

    def handle_event(self, event):
        if event.is_directory:
            return
        filepath = event.src_path
        filename = os.path.basename(filepath)
        if not filename.endswith(".md"):
            return

        now = time.time()
        with self.lock:
            if len(self.last_triggered) > 2000:
                self.last_triggered.clear()
            
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now

        log.info(f"Diary modified: {filename}. Triggering sync_focus.py...")
        import subprocess
        import sys
        
        try:
            sync_script = os.path.expanduser("~/.gemini/scripts/sync_focus.py")
            if os.path.exists(sync_script):
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                # Use Popen for fire-and-forget instead of blocking run
                subprocess.Popen([sys.executable, sync_script], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.error(f"Failed to trigger sync_focus.py: {e}")

    def on_created(self, event): self.handle_event(event)
    def on_modified(self, event): self.handle_event(event)


class RawWatchdogHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()

    def handle_event(self, event):
        if event.is_directory:
            return
        filepath = event.src_path
        
        # Prevent Double-Trigger: Exclude privacy/Diary (handled by DiaryWatchdogHandler)
        if "privacy" in filepath and "Diary" in filepath:
            return
            
        filename = os.path.basename(filepath)
        # only md, pdf, txt, etc? Let's just say any file that doesn't start with . and doesn't end with .tmp
        if filename.startswith('.') or filename.endswith('.tmp'):
            return

        now = time.time()
        with self.lock:
            if len(self.last_triggered) > 2000:
                self.last_triggered.clear()
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now

        log.info(f"Raw source modified: {filename}. Triggering sync_vector_lake in worker pool...")
        try:
            from vector_lake.tool_sync import sync_vector_lake
            # Run using ThreadPoolExecutor to prevent thread explosion
            if not hasattr(self, "executor"):
                import concurrent.futures
                self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
            future = self.executor.submit(sync_vector_lake)
            future.add_done_callback(lambda f: log.error(f"sync_vector_lake Failed: {f.exception()}") if f.exception() else None)
        except Exception as e:
            log.error(f"Failed to trigger sync_vector_lake: {e}")

    def on_created(self, event): self.handle_event(event)
    def on_modified(self, event): self.handle_event(event)
    def on_moved(self, event): self.handle_event(event)
from vector_lake.watchdog_status import write_status

def index_worker_loop():
    log.info("Outbox Consumer Thread started.")
    consecutive_failures = 0
    max_failures = 5
    backoff_base = 2

    while True:
        try:
            if consecutive_failures >= max_failures:
                write_status("halted", 0, index_queue.qsize(), "Outbox Consumer Halted", "Max consecutive failures reached")
                log.error("Outbox Consumer Halted. Entering 60s cooldown before retry.")
                time.sleep(60)
                consecutive_failures = 0
                continue
                
            import tempfile
            import os
            from pathlib import Path
            tmp_dir = Path(tempfile.gettempdir()) / "vector_lake_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            flag_path = tmp_dir / "outbox_signal.lock"
            
            # Handle manual filesystem modifications from watchdog
            pending_legacy = set()
            wait_for_signal = True
            try:
                while True:
                    pending_legacy.add(index_queue.get_nowait())
                    index_queue.task_done()
            except queue.Empty:
                pass
                
            if pending_legacy:
                from vector_lake.db_store import transaction, get_connection
                from vector_lake.governance_store import sync_pages_to_canonical, _utc_now
                from vector_lake.wiki_utils import get_wiki_dir
                wiki_dir = get_wiki_dir()
                conn = get_connection()
                for fname in pending_legacy:
                    fpath = os.path.join(wiki_dir, fname)
                    try:
                        with transaction():
                            if os.path.exists(fpath):
                                sync_pages_to_canonical([fpath], origin="watchdog", auto_approve=True, summary=f"Manual update: {fname}")
                                conn.execute("INSERT INTO mutation_outbox (filename, mutation_type, created_at) VALUES (?, ?, ?)", (fname, 'update', _utc_now()))
                            else:
                                from vector_lake.db_store import delete_node_cascade
                                node_key = fname[:-3] if fname.endswith(".md") else fname
                                delete_node_cascade(node_key)
                                conn.execute("INSERT INTO mutation_outbox (filename, mutation_type, created_at) VALUES (?, ?, ?)", (fname, 'delete', _utc_now()))
                    except Exception as e:
                        log.error(f"Failed to process manual edit for {fname}: {e}")
                wait_for_signal = False
                
            if wait_for_signal and not os.path.exists(flag_path):
                # Poll SQLite every 10 seconds to catch missed or orphaned items
                if int(time.time()) % 10 != 0:
                    time.sleep(1)
                    continue
                
            if os.path.exists(flag_path):
                try: os.remove(flag_path)
                except OSError: pass

            from vector_lake.db_store import get_connection, transaction
            conn = get_connection()
            rows = conn.execute("SELECT id, filename, mutation_type FROM mutation_outbox WHERE status = 'pending' ORDER BY id ASC LIMIT 50").fetchall()
            
            if not rows:
                if pending_legacy:
                    write_status("idle", 0, index_queue.qsize(), "Legacy queue drained", "")
                continue

            write_status("processing", 0, index_queue.qsize(), f"Consuming {len(rows)} outbox mutations", "")

            from vector_lake import indexer
            from vector_lake import governance_store
            
            with global_task_lock:
                for row in rows:
                    outbox_id = row["id"]
                    filename = row["filename"]
                    mutation_type = row["mutation_type"]
                    try:
                        indexer.update_index_items([filename])
                        with transaction():
                            conn.execute("UPDATE mutation_outbox SET status = 'completed' WHERE id = ?", (outbox_id,))
                    except Exception as e:
                        log.error(f"Failed to process outbox item {outbox_id} for {filename}: {e}")
                        with transaction():
                            conn.execute("UPDATE mutation_outbox SET status = 'failed' WHERE id = ?", (outbox_id,))
                        raise e

                log.info(f"O(1) Batched Index updated for {len(rows)} outbox mutations")

            consecutive_failures = 0

        except Exception as exc:
            consecutive_failures += 1
            log.error(f"Outbox worker error: {exc}")
            write_status("error", 0, index_queue.qsize(), "Outbox consumer exception", str(exc))
            time.sleep(min(backoff_base ** consecutive_failures, 60))
        finally:
            from vector_lake.db_store import close_connection
            close_connection()


def scheduled_lint_loop():
    log.info("Scheduled Lint Worker Thread started.")
    last_run_date_hour = ""
    last_snapshot_minute = -1

    while True:
        try:
            # DB connection opens only inside actual work blocks now
            now = time.localtime()
            
            # Run at 10:00 and 23:00
            if now.tm_hour in (10, 23) and now.tm_min == 0:
                current_date_hour = f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-{now.tm_hour}"
                if current_date_hour != last_run_date_hour:
                    write_status("processing", 0, index_queue.qsize(), "Running Scheduled Auto-Lint", "")
                    log.info("Triggering Scheduled Autonomous Auto-Lint...")
                    
                    import subprocess
                    import sys
                    import os
                    try:
                        env = os.environ.copy()
                        env["PYTHONIOENCODING"] = "utf-8"
                        
                        from vector_lake import get_extension_root
                        from pathlib import Path
                        plugin_dir = get_extension_root()
                        gemini_root = Path("~/.gemini").expanduser()
                        
                    except Exception as e:
                        log.warning(f"Failed to run auxiliary daemons: {e}")
                        write_status("error", 0, index_queue.qsize(), "Auxiliary Daemons Error", str(e))
                    
                    from vector_lake.tool_lint import lint_vector_lake
                    from vector_lake import indexer
                    with global_task_lock:
                        if indexer.refresh_graph_topology_if_dirty():
                            log.info("Graph topology refreshed during scheduled lint.")
                        lint_vector_lake(auto_fix=False)
                        
                        # Truncate WAL to prevent unbounded growth
                        from vector_lake.db_store import get_connection
                        try:
                            conn = get_connection()
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            log.info("SQLite WAL checkpoint (TRUNCATE) completed successfully.")
                        except Exception as e:
                            log.error(f"Failed to truncate WAL: {e}")
                    
                    log.info("Scheduled Autonomous Auto-Lint completed.")
                    last_run_date_hour = current_date_hour
                    write_status("idle", 0, index_queue.qsize(), "Scheduled Lint finished", "")
            
            # Calculate wait time till next minute to avoid tight spinning, or just sleep for 30 seconds
            import datetime
            now_dt = datetime.datetime.now()
            # If we just ran at hour 10 or 23, sleep 60 seconds to push past min 0
            if now.tm_hour in (10, 23) and now.tm_min == 0:
                time.sleep(60)
            else:
                time.sleep(30)
                
        except Exception as exc:
            log.error(f"Scheduled lint worker error: {exc}")
            write_status("error", 0, index_queue.qsize(), "Scheduled lint exception", str(exc))
            time.sleep(60)

def start_watchdog():
    import msvcrt
    import sys
    from vector_lake import get_extension_root
    
    lock_file_path = get_extension_root() / "tmp" / "watchdog_instance.lock"
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    lock_file = open(lock_file_path, 'w')
    try:
        # Request an exclusive, non-blocking lock. If another instance holds it, raises OSError.
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.warning("Watchdog is already running (lock held by another instance). Exiting.")
        sys.exit(0)

    threading.Thread(target=index_worker_loop, daemon=True).start()
    threading.Thread(target=scheduled_lint_loop, daemon=True).start()
    
    from vector_lake.ingest_worker import start_worker
    threading.Thread(target=start_worker, daemon=True).start()

    observer = Observer()

    from vector_lake.wiki_utils import get_wiki_dir

    wiki_dir = str(get_wiki_dir())
    if os.path.exists(wiki_dir):
        wiki_handler = WikiIndexHandler()
        observer.schedule(wiki_handler, wiki_dir, recursive=False)
        log.info(f"Wiki AST monitor active on directory: {wiki_dir}")

    diary_dir = os.path.expanduser("~/.gemini/MEMORY/raw/privacy/Diary")
    if os.path.exists(diary_dir):
        diary_handler = DiaryWatchdogHandler()
        observer.schedule(diary_handler, diary_dir, recursive=False)
        log.info(f"Diary monitor active on directory: {diary_dir}")

    raw_dir = os.path.expanduser("~/.gemini/MEMORY/raw")
    if os.path.exists(raw_dir):
        raw_handler = RawWatchdogHandler()
        observer.schedule(raw_handler, raw_dir, recursive=True)
        log.info(f"Raw source monitor active on directory: {raw_dir}")

    observer.start()
    log.info("Vector Lake Watchdog Agent is now running in Background Index/Lint mode.")
    try:
        last_heartbeat = 0
        while True:
            now = time.time()
            if now - last_heartbeat >= 30:
                write_status("running", 0, index_queue.qsize(), "Watchdog Heartbeat", "")
                last_heartbeat = now
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Termination signal received. Shutting down Watchdog...")
        observer.stop()
    finally:
        try:
            lock_file.close()
        except Exception:
            pass
    observer.join()

if __name__ == "__main__":
    start_watchdog()
