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

        try:
            from vector_lake import db_store

            if os.path.exists(filepath):
                payload_text = Path(filepath).read_text(encoding="utf-8")
                if db_store.is_managed_projection_state(filename, "update", payload_text):
                    return
            elif db_store.is_managed_projection_state(filename, "delete"):
                return
        except Exception as exc:
            log.warning("Could not classify projection event for %s: %s", filename, exc)

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
        
        def run_sync():
            try:
                sync_script = os.path.expanduser("~/.gemini/scripts/sync_focus.py")
                if os.path.exists(sync_script):
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    subprocess.run([sys.executable, sync_script], capture_output=True, env=env, timeout=180)
            except Exception as e:
                log.error(f"Failed to trigger sync_focus.py: {e}")
                
        if not hasattr(self, "executor"):
            import concurrent.futures
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self.executor.submit(run_sync)

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
        # only md, pdf, txt, etc? Let's just say any file that doesn't start with .
        if filename.startswith('.'):
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
            self.executor.submit(sync_vector_lake)
        except Exception as e:
            log.error(f"Failed to trigger sync_vector_lake: {e}")

    def on_created(self, event): self.handle_event(event)
    def on_modified(self, event): self.handle_event(event)
    def on_moved(self, event): self.handle_event(event)
from vector_lake.watchdog_status import write_status


def process_mutation_outbox_batch(
    limit: int = 50,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
) -> dict:
    """Process one durable outbox batch; per-row failures never abort peers."""
    from vector_lake import db_store, indexer
    from vector_lake.mutation_coordinator import materialize_markdown_projection
    from vector_lake.wiki_utils import get_wiki_dir

    rows = db_store.claim_mutation_outbox(limit=limit)
    stats = {"claimed": len(rows), "completed": 0, "retrying": 0, "failed": 0}
    ready_for_index = []
    for row in rows:
        outbox_id = int(row["id"])
        filename = row["filename"]
        try:
            target = get_wiki_dir() / filename
            already_materialized = (
                not target.exists()
                if row["mutation_type"] == "delete"
                else (
                    row.get("payload_text") is not None
                    and target.exists()
                    and target.read_text(encoding="utf-8") == row.get("payload_text")
                )
            )
            if not already_materialized:
                materialize_markdown_projection(
                    filename,
                    row["mutation_type"],
                    row.get("payload_text"),
                    validation_mode=row.get("validation_mode") or "full",
                )
            ready_for_index.append((outbox_id, filename))
        except Exception as exc:
            status = db_store.fail_mutation_outbox(
                outbox_id,
                str(exc),
                max_attempts=max_attempts,
                backoff_base=backoff_base,
            )
            stats["failed" if status == "failed" else "retrying"] += 1
            log.error(f"Outbox item {outbox_id} failed for {filename}; status={status}: {exc}")
    if ready_for_index:
        filenames = list(dict.fromkeys(filename for _, filename in ready_for_index))
        try:
            indexer.update_index_items(filenames)
        except Exception as exc:
            for outbox_id, filename in ready_for_index:
                status = db_store.fail_mutation_outbox(
                    outbox_id,
                    str(exc),
                    max_attempts=max_attempts,
                    backoff_base=backoff_base,
                )
                stats["failed" if status == "failed" else "retrying"] += 1
                log.error(f"Outbox index batch failed for {filename}; status={status}: {exc}")
        else:
            for outbox_id, _ in ready_for_index:
                db_store.complete_mutation_outbox(outbox_id)
                stats["completed"] += 1
    return stats

def index_worker_loop():
    log.info("Outbox Consumer Thread started.")
    consecutive_failures = 0
    max_failures = 5
    backoff_base = 2

    while True:
        try:
            if consecutive_failures >= max_failures:
                write_status("halted", 0, index_queue.qsize(), "Outbox Consumer Halted", "Max consecutive failures reached", component="outbox")
                log.error("Outbox Consumer Halted. Entering 60s cooldown before retry.")
                time.sleep(60)
                consecutive_failures = 0
                continue
                
            from vector_lake import get_extension_root
            import os
            flag_path = get_extension_root() / "tmp" / "outbox_signal.lock"
            
            # The signal is only a latency hint. Durable rows are always polled.
            if os.path.exists(flag_path):
                try: os.remove(flag_path)
                except OSError: pass

            with global_task_lock:
                stats = process_mutation_outbox_batch(limit=50)

            if stats["claimed"]:
                write_status(
                    "processing",
                    stats["completed"],
                    index_queue.qsize(),
                    f"Outbox batch: {stats}",
                    "",
                    component="outbox",
                )
                log.info(f"Outbox batch completed: {stats}")

            # Manual filesystem edits are bounded so they cannot starve durable outbox work.
            pending_legacy = set()
            while len(pending_legacy) < 25:
                try:
                    pending_legacy.add(index_queue.get_nowait())
                    index_queue.task_done()
                except queue.Empty:
                    break

            if pending_legacy:
                write_status(
                    "processing",
                    0,
                    index_queue.qsize(),
                    f"Legacy projection batch: {len(pending_legacy)}",
                    "",
                    component="outbox",
                )
                from vector_lake.mutation_coordinator import execute_mutation_plan
                from vector_lake.wiki_utils import get_wiki_dir
                wiki_dir = get_wiki_dir()
                for fname in pending_legacy:
                    fpath = os.path.join(wiki_dir, fname)
                    try:
                        if os.path.exists(fpath):
                            with open(fpath, "r", encoding="utf-8") as f:
                                execute_mutation_plan(fname, content=f.read(), is_delete=False)
                        else:
                            execute_mutation_plan(fname, is_delete=True)
                    except Exception as e:
                        log.error(f"Failed to process manual edit for {fname}: {e}")
                write_status(
                    "idle",
                    len(pending_legacy),
                    index_queue.qsize(),
                    "Legacy projection batch completed",
                    "",
                    component="outbox",
                )
            elif not stats["claimed"]:
                write_status(
                    "idle",
                    0,
                    index_queue.qsize(),
                    "Outbox idle",
                    "",
                    component="outbox",
                )
                time.sleep(1)

            if consecutive_failures:
                write_status("idle", 0, index_queue.qsize(), "Outbox consumer recovered", "", component="outbox")
            consecutive_failures = 0

        except Exception as exc:
            consecutive_failures += 1
            log.error(f"Outbox worker error: {exc}")
            write_status("error", 0, index_queue.qsize(), "Outbox consumer exception", str(exc), component="outbox")
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
                    write_status("processing", 0, index_queue.qsize(), "Running Scheduled Auto-Lint", "", component="scheduler")
                    log.info("Triggering Scheduled Autonomous Auto-Lint...")
                    
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
                    write_status("idle", 0, index_queue.qsize(), "Scheduled Lint finished", "", component="scheduler")
            
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
            write_status("error", 0, index_queue.qsize(), "Scheduled lint exception", str(exc), component="scheduler")
            time.sleep(60)


def _start_watchdog_locked():
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
    write_status("idle", 0, index_queue.qsize(), "Watchdog started", "", component="watchdog")
    last_heartbeat = 0.0
    try:
        while True:
            now = time.time()
            if now - last_heartbeat >= 30:
                write_status("idle", 0, index_queue.qsize(), "Watchdog heartbeat", "", component="watchdog")
                last_heartbeat = now
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Termination signal received. Shutting down Watchdog...")
        observer.stop()
    observer.join()


def start_watchdog():
    """Run exactly one watchdog instance for the active MEMORY root."""
    from filelock import FileLock, Timeout
    from vector_lake.wiki_utils import get_meta_dir

    instance_lock = FileLock(str(get_meta_dir() / ".watchdog.instance.lock"))
    try:
        instance_lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError("A Vector Lake watchdog instance is already running for this MEMORY root.") from exc
    try:
        return _start_watchdog_locked()
    finally:
        instance_lock.release()

if __name__ == "__main__":
    start_watchdog()
