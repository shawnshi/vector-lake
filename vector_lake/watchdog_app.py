import logging
import os
import queue
import threading
import time
from pathlib import Path
import json
from vector_lake import get_extension_root
from vector_lake.watchdog_status import write_status

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


def _watch_directories() -> dict[str, Path]:
    from vector_lake.wiki_utils import get_raw_dir, get_wiki_dir

    raw_dir = get_raw_dir()
    return {
        "wiki": get_wiki_dir(),
        "raw": raw_dir,
        "diary": raw_dir / "privacy" / "Diary",
    }
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
        self.lock = threading.RLock()
        self.executor = None
        self.sync_future = None
        self.pending_paths = set()
        self.pending_overflow = False
        self.shutting_down = False

    def _run_ingest(self, paths, overflow):
        from vector_lake.tool_ingest import prepare_ingest_batch

        return prepare_ingest_batch(
            batch_size=50 if overflow else max(1, len(paths)),
            candidate_paths=None if overflow else paths,
        )

    def _submit_pending_locked(self):
        if self.shutting_down:
            return
        if self.sync_future is not None and not self.sync_future.done():
            return
        if not self.pending_paths and not self.pending_overflow:
            return
        import concurrent.futures

        if self.executor is None:
            self.executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="vector-lake-raw-ingest",
            )
        paths = sorted(self.pending_paths)
        overflow = self.pending_overflow
        self.pending_paths.clear()
        self.pending_overflow = False
        future = self.executor.submit(self._run_ingest, paths, overflow)
        self.sync_future = future
        future.add_done_callback(self._ingest_done)

    def _ingest_done(self, future):
        try:
            error = future.exception()
        except Exception as exc:
            error = exc
        if error:
            log.error(f"Raw ingest preparation failed: {error}")
        with self.lock:
            if self.sync_future is future:
                self.sync_future = None
            if not self.shutting_down:
                self._submit_pending_locked()

    def shutdown(self):
        with self.lock:
            self.shutting_down = True
            executor = self.executor
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self.lock:
            paths = sorted(self.pending_paths)
            overflow = self.pending_overflow
            self.pending_paths.clear()
            self.pending_overflow = False
            self.executor = None
            self.sync_future = None
        if paths or overflow:
            try:
                self._run_ingest(paths, overflow)
            except Exception as exc:
                log.error(f"Raw ingest shutdown drain failed: {exc}")

    def handle_event(self, event):
        if event.is_directory:
            return
        filepath = getattr(event, "dest_path", None) or event.src_path
        
        # Prevent Double-Trigger: Exclude privacy/Diary (handled by DiaryWatchdogHandler)
        if "privacy" in filepath and "Diary" in filepath:
            return
            
        filename = os.path.basename(filepath)
        # only md, pdf, txt, etc? Let's just say any file that doesn't start with . and doesn't end with .tmp
        if filename.startswith('.') or filename.endswith('.tmp'):
            return

        now = time.time()
        with self.lock:
            if self.shutting_down:
                return
            if len(self.last_triggered) > 2000:
                self.last_triggered.clear()
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now
            max_pending = max(1, int(os.environ.get("VECTOR_LAKE_RAW_EVENT_BUFFER", "500")))
            if len(self.pending_paths) < max_pending:
                self.pending_paths.add(str(Path(filepath).resolve()))
            else:
                self.pending_overflow = True
            self._submit_pending_locked()

        log.info(f"Raw source modified: {filename}. Scheduled path-scoped ingest preparation.")

    def on_created(self, event):
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)

    def on_moved(self, event):
        self.handle_event(event)


def process_mutation_outbox_batch(
    limit: int = 50,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    outbox_ids: list[int] | None = None,
) -> dict:
    """Process one durable outbox batch; per-row failures never abort peers."""
    from vector_lake import db_store, indexer
    from vector_lake.mutation_coordinator import materialize_markdown_projection
    from vector_lake.wiki_utils import get_wiki_dir, normalize_semantic_text

    lease_seconds = max(120, min(3600, max(1, int(limit))))
    claim_kwargs = {"limit": limit, "lease_seconds": lease_seconds}
    if outbox_ids is not None:
        claim_kwargs["outbox_ids"] = outbox_ids
    rows = db_store.claim_mutation_outbox(**claim_kwargs)
    stats = {"claimed": len(rows), "completed": 0, "retrying": 0, "failed": 0}
    ready_for_index = []
    for row in rows:
        outbox_id = int(row["id"])
        filename = row["filename"]
        lease_args = (
            row["lease_owner"],
            row["lease_token"],
            int(row["lease_generation"]),
        )
        try:
            if not db_store.mutation_outbox_lease_is_current(outbox_id, *lease_args):
                continue
            target = get_wiki_dir() / filename
            already_materialized = (
                not target.exists()
                if row["mutation_type"] == "delete"
                else (
                    row.get("payload_text") is not None
                    and target.exists()
                    and normalize_semantic_text(target.read_text(encoding="utf-8"))
                    == normalize_semantic_text(row.get("payload_text"))
                )
            )
            if not already_materialized:
                with db_store.transaction():
                    if not db_store.mutation_outbox_lease_is_current(outbox_id, *lease_args):
                        continue
                    materialize_markdown_projection(
                        filename,
                        row["mutation_type"],
                        row.get("payload_text"),
                        validation_mode=row.get("validation_mode") or "full",
                        projection_base_hash=row.get("projection_base_hash"),
                    )
            if db_store.mutation_outbox_lease_is_current(outbox_id, *lease_args):
                ready_for_index.append((row, filename))
        except Exception as exc:
            status = db_store.fail_mutation_outbox(
                outbox_id,
                str(exc),
                *lease_args,
                max_attempts=max_attempts,
                backoff_base=backoff_base,
            )
            if status != "stale":
                stats["failed" if status == "failed" else "retrying"] += 1
            log.error(f"Outbox item {outbox_id} failed for {filename}; status={status}: {exc}")
    if ready_for_index:
        current_rows = [
            (row, filename)
            for row, filename in ready_for_index
            if db_store.mutation_outbox_lease_is_current(
                int(row["id"]),
                row["lease_owner"],
                row["lease_token"],
                int(row["lease_generation"]),
            )
        ]
        filenames = list(dict.fromkeys(filename for _, filename in current_rows))
        try:
            if filenames:
                if indexer.index_projection_matches_canonical(filenames):
                    indexer.refresh_claim_graph_projection()
                else:
                    indexer.update_index_items(filenames)
        except Exception as exc:
            for row, filename in current_rows:
                outbox_id = int(row["id"])
                status = db_store.fail_mutation_outbox(
                    outbox_id,
                    str(exc),
                    row["lease_owner"],
                    row["lease_token"],
                    int(row["lease_generation"]),
                    max_attempts=max_attempts,
                    backoff_base=backoff_base,
                )
                if status != "stale":
                    stats["failed" if status == "failed" else "retrying"] += 1
                log.error(f"Outbox index batch failed for {filename}; status={status}: {exc}")
        else:
            for row, _ in current_rows:
                if db_store.complete_mutation_outbox(
                    int(row["id"]),
                    row["lease_owner"],
                    row["lease_token"],
                    int(row["lease_generation"]),
                ):
                    stats["completed"] += 1
    return stats


def process_legacy_projection_batch(filenames) -> dict:
    """Promote bounded manual Markdown edits into canonical state and durable outbox rows."""
    from vector_lake.mutation_coordinator import execute_mutation_batch
    from vector_lake.wiki_utils import get_wiki_dir

    stats = {"completed": 0, "failed": 0}
    wiki_dir = get_wiki_dir()
    for filename in dict.fromkeys(str(item) for item in filenames):
        target = wiki_dir / filename
        try:
            mutation = {"filename": filename, "is_delete": not target.exists()}
            if target.exists():
                mutation["content"] = target.read_text(encoding="utf-8")
            execute_mutation_batch([mutation], validation_mode="schema", origin="watchdog")
            stats["completed"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log.error("Failed to process manual edit for %s: %s", filename, exc)
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
                
            from vector_lake.wiki_utils import get_outbox_signal_path
            flag_path = get_outbox_signal_path()
            
            # The signal is only a latency hint. Durable rows are always polled.
            if flag_path.exists():
                try:
                    flag_path.unlink()
                except OSError:
                    pass

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
                legacy_stats = process_legacy_projection_batch(pending_legacy)
                write_status(
                    "error" if legacy_stats["failed"] else "idle",
                    legacy_stats["completed"],
                    index_queue.qsize(),
                    f"Legacy projection batch completed: {legacy_stats}",
                    f"{legacy_stats['failed']} manual edit(s) failed" if legacy_stats["failed"] else "",
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


def expire_stale_ingest_jobs_for_watchdog() -> int:
    """Run the bounded ingest expiry used by the hourly scheduler."""
    from vector_lake.db_store import expire_stale_subagent_jobs

    max_age_seconds = max(
        60,
        int(os.environ.get("VECTOR_LAKE_INGEST_TASK_MAX_AGE_SECONDS", "86400")),
    )
    return expire_stale_subagent_jobs(max_age_seconds=max_age_seconds)


def scheduled_lint_loop():
    log.info("Scheduled Lint Worker Thread started.")
    last_run_date_hour = ""
    last_expiry_date_hour = ""

    while True:
        try:
            # DB connection opens only inside actual work blocks now
            now = time.localtime()

            # Expire abandoned subagent work once per hour instead of waiting
            # for a manual CLI invocation or one of the twice-daily lint runs.
            current_date_hour = f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-{now.tm_hour}"
            if now.tm_min == 0 and current_date_hour != last_expiry_date_hour:
                from vector_lake.db_store import close_connection

                try:
                    expired = expire_stale_ingest_jobs_for_watchdog()
                    log.info("Hourly ingest expiry completed: %s stale job(s).", expired)
                    last_expiry_date_hour = current_date_hour
                finally:
                    close_connection()
            
            # Run at 10:00 and 23:00
            if now.tm_hour in (10, 23) and now.tm_min == 0:
                if current_date_hour != last_run_date_hour:
                    write_status("processing", 0, index_queue.qsize(), "Running Scheduled Auto-Lint", "", component="scheduler")
                    log.info("Triggering Scheduled Autonomous Auto-Lint...")

                    from vector_lake.tool_lint import lint_vector_lake
                    from vector_lake import indexer
                    from vector_lake.db_store import close_connection, get_connection
                    try:
                        with global_task_lock:
                            if indexer.refresh_graph_topology_if_dirty():
                                log.info("Graph topology refreshed during scheduled lint.")
                            lint_vector_lake(auto_fix=False)

                            # Truncate WAL to prevent unbounded growth
                            conn = get_connection()
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            log.info("SQLite WAL checkpoint (TRUNCATE) completed successfully.")
                    finally:
                        close_connection()
                    
                    log.info("Scheduled Autonomous Auto-Lint completed.")
                    last_run_date_hour = current_date_hour
                    write_status("idle", 0, index_queue.qsize(), "Scheduled Lint finished", "", component="scheduler")
            
            # Calculate wait time till next minute to avoid tight spinning, or just sleep for 30 seconds
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
    raw_handler = None
    watch_dirs = _watch_directories()

    wiki_dir = str(watch_dirs["wiki"])
    if os.path.exists(wiki_dir):
        wiki_handler = WikiIndexHandler()
        observer.schedule(wiki_handler, wiki_dir, recursive=False)
        log.info(f"Wiki AST monitor active on directory: {wiki_dir}")

    diary_dir = str(watch_dirs["diary"])
    if os.path.exists(diary_dir):
        diary_handler = DiaryWatchdogHandler()
        observer.schedule(diary_handler, diary_dir, recursive=False)
        log.info(f"Diary monitor active on directory: {diary_dir}")

    raw_dir = str(watch_dirs["raw"])
    if os.path.exists(raw_dir):
        raw_handler = RawWatchdogHandler()
        observer.schedule(raw_handler, raw_dir, recursive=True)
        log.info(f"Raw source monitor active on directory: {raw_dir}")

    observer.start()
    log.info("Vector Lake Watchdog Agent is now running in Background Index/Lint mode.")
    write_status("idle", 0, index_queue.qsize(), "Watchdog started", "", component="watchdog")
    last_heartbeat = 0.0
    try:
        last_heartbeat = 0
        while True:
            now = time.time()
            if now - last_heartbeat >= 30:
                write_status("idle", 0, index_queue.qsize(), "Watchdog heartbeat", "", component="watchdog")
                last_heartbeat = now
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Termination signal received. Shutting down Watchdog...")
    finally:
        observer.stop()
        if raw_handler is not None:
            raw_handler.shutdown()
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
