import logging
import os
import queue
import threading
import time
from pathlib import Path
from itertools import islice
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

_NON_CANONICAL_WIKI_FILENAMES = frozenset(
    {
        "index.md",
        "log.md",
        "overview.md",
        "orphan_pages.md",
        "wiki_link_stats.md",
        "synthesis_log.md",
    }
)


def _is_canonical_wiki_filename(filename: str) -> bool:
    normalized = str(filename).casefold()
    return (
        normalized.endswith(".md")
        and normalized not in _NON_CANONICAL_WIKI_FILENAMES
        and not normalized.startswith("system_")
    )


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default



_BACKGROUND_THREADS_LOCK = threading.Lock()
_BACKGROUND_THREADS: dict[str, threading.Thread] = {}


def _bounded_env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.1,
    maximum: float = 300.0,
) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _shutdown_timeout_seconds() -> float:
    return _bounded_env_float(
        "VECTOR_LAKE_WATCHDOG_SHUTDOWN_TIMEOUT_SECONDS",
        10.0,
    )


def background_thread_health() -> dict[str, bool]:
    """Return the liveness of the most recently registered watchdog workers."""
    with _BACKGROUND_THREADS_LOCK:
        return {
            name: thread.is_alive()
            for name, thread in _BACKGROUND_THREADS.items()
        }


def _register_background_threads(threads: dict[str, threading.Thread]) -> None:
    with _BACKGROUND_THREADS_LOCK:
        _BACKGROUND_THREADS.clear()
        _BACKGROUND_THREADS.update(threads)


def _join_threads_bounded(
    threads: dict[str, threading.Thread],
    timeout_seconds: float,
) -> list[str]:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    for thread in threads.values():
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
    return sorted(name for name, thread in threads.items() if thread.is_alive())
def _wiki_reconcile_marker_path() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "wiki_reconcile_required.json"


class WikiIndexEventBuffer:
    """Bounded, path-deduplicating Wiki event buffer.

    Overflow is sticky and generation-tracked. Events arriving during a full
    reconciliation advance the generation so a stale worker cannot clear the
    marker. Reconcile claims are rate-limited while successful partial batches
    may explicitly request an immediate next claim.
    """

    def __init__(
        self,
        max_pending: int | None = None,
        *,
        persist_reconcile_marker: bool = False,
    ):
        self.max_pending = max(
            1,
            int(
                max_pending
                if max_pending is not None
                else _positive_env_int("VECTOR_LAKE_WIKI_EVENT_BUFFER", 500)
            ),
        )
        self._queue = queue.Queue(maxsize=self.max_pending)
        self._pending_paths: set[str] = set()
        self._lock = threading.Lock()
        self._full_reconcile_required = False
        self._reconcile_generation = 0
        self._next_reconcile_attempt_at = 0.0
        self._persist_reconcile_marker = bool(persist_reconcile_marker)
        self._reconcile_plan_generation: int | None = None
        self._reconcile_plan_candidates: tuple[str, ...] = ()
        self._reconcile_plan_cursor = 0
        self._reconcile_plan_unplanned = 0

    def _invalidate_reconcile_plan_locked(self) -> None:
        self._reconcile_plan_generation = None
        self._reconcile_plan_candidates = ()
        self._reconcile_plan_cursor = 0
        self._reconcile_plan_unplanned = 0

    def _persist_full_reconcile_locked(self) -> bool:
        if not self._persist_reconcile_marker:
            return True
        temporary = None
        try:
            marker = _wiki_reconcile_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_name(
                f".{marker.name}.{os.getpid()}.{threading.get_ident()}."
                f"{time.time_ns()}.tmp"
            )
            payload = json.dumps(
                {
                    "required": True,
                    "generation": self._reconcile_generation,
                    "updated_at": time.time(),
                },
                sort_keys=True,
            )
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
            return True
        except Exception as exc:
            log.error("Could not persist Wiki reconciliation marker: %s", exc)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _remove_full_reconcile_marker_locked(self) -> bool:
        if not self._persist_reconcile_marker:
            return True
        try:
            _wiki_reconcile_marker_path().unlink(missing_ok=True)
            return True
        except Exception as exc:
            log.error("Could not remove Wiki reconciliation marker: %s", exc)
            return False

    def _full_reconcile_marker_exists_locked(self) -> bool:
        if not self._persist_reconcile_marker:
            return True
        try:
            return _wiki_reconcile_marker_path().is_file()
        except OSError as exc:
            log.error("Could not inspect Wiki reconciliation marker: %s", exc)
            return False

    def require_full_reconcile(self) -> int:
        """Set a sticky reconciliation marker without retaining another path."""
        with self._lock:
            self._full_reconcile_required = True
            self._reconcile_generation += 1
            self._next_reconcile_attempt_at = 0.0
            self._invalidate_reconcile_plan_locked()
            if not self._persist_full_reconcile_locked():
                raise RuntimeError(
                    "Wiki reconciliation is required but its durable marker "
                    "could not be persisted"
                )
            return self._reconcile_generation

    def restore_full_reconcile_marker(self) -> bool:
        """Restore a durable overflow marker after a watchdog restart."""
        if not self._persist_reconcile_marker:
            return False
        marker = _wiki_reconcile_marker_path()
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            generation = max(1, int(payload.get("generation", 1)))
        except FileNotFoundError:
            return False
        except Exception as exc:
            generation = 1
            log.warning(
                "Wiki reconciliation marker could not be read; forcing a safe scan: %s",
                exc,
            )
        with self._lock:
            self._full_reconcile_required = True
            self._reconcile_generation = max(
                self._reconcile_generation,
                generation,
            )
            self._next_reconcile_attempt_at = 0.0
            self._invalidate_reconcile_plan_locked()
        return True

    def install_reconcile_plan(
        self,
        expected_generation: int,
        candidates,
        total_drift: int,
    ) -> bool:
        """Install a bounded in-process plan only for the scanned generation."""
        bounded = tuple(
            str(item) for item in islice(candidates, 50_000)
        )
        with self._lock:
            if (
                not self._full_reconcile_required
                or self._reconcile_generation != int(expected_generation)
            ):
                return False
            self._reconcile_plan_generation = int(expected_generation)
            self._reconcile_plan_candidates = bounded
            self._reconcile_plan_cursor = 0
            self._reconcile_plan_unplanned = max(
                0,
                int(total_drift) - len(bounded),
            )
            return True

    def reconcile_plan_batch(
        self,
        expected_generation: int,
        batch_size: int,
    ) -> list[str] | None:
        """Peek the next plan batch; None means the generation needs a scan."""
        with self._lock:
            if (
                self._reconcile_plan_generation != int(expected_generation)
                or self._reconcile_generation != int(expected_generation)
            ):
                return None
            end = min(
                len(self._reconcile_plan_candidates),
                self._reconcile_plan_cursor + max(1, int(batch_size)),
            )
            return list(
                self._reconcile_plan_candidates[self._reconcile_plan_cursor:end]
            )

    def acknowledge_reconcile_plan_batch(
        self,
        expected_generation: int,
        filenames,
    ) -> bool:
        """Advance only when the completed batch is exactly the current head."""
        completed = tuple(str(item) for item in filenames)
        with self._lock:
            if (
                self._reconcile_plan_generation != int(expected_generation)
                or self._reconcile_generation != int(expected_generation)
            ):
                return False
            end = self._reconcile_plan_cursor + len(completed)
            if self._reconcile_plan_candidates[self._reconcile_plan_cursor:end] != completed:
                return False
            self._reconcile_plan_cursor = end
            return True

    def reconcile_plan_remaining(
        self,
        expected_generation: int,
    ) -> tuple[int, int] | None:
        """Return queued and unplanned drift counts for the current generation."""
        with self._lock:
            if (
                self._reconcile_plan_generation != int(expected_generation)
                or self._reconcile_generation != int(expected_generation)
            ):
                return None
            queued = len(self._reconcile_plan_candidates) - self._reconcile_plan_cursor
            return queued, self._reconcile_plan_unplanned

    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    def put(self, path: str) -> bool:
        """Queue one path without blocking; return whether it was newly queued."""
        value = str(path)
        key = self._path_key(value)
        with self._lock:
            if self._full_reconcile_required:
                self._reconcile_generation += 1
                self._invalidate_reconcile_plan_locked()
                if (
                    self._persist_reconcile_marker
                    and not self._full_reconcile_marker_exists_locked()
                    and not self._persist_full_reconcile_locked()
                ):
                    raise RuntimeError(
                        "Wiki reconciliation marker remains non-durable"
                    )
                return False
            if key in self._pending_paths:
                return False
            try:
                self._queue.put_nowait(value)
            except queue.Full:
                self._full_reconcile_required = True
                self._reconcile_generation += 1
                self._next_reconcile_attempt_at = 0.0
                self._invalidate_reconcile_plan_locked()
                if not self._persist_full_reconcile_locked():
                    raise RuntimeError(
                        "Wiki event buffer overflowed but its reconciliation "
                        "marker could not be persisted"
                    )
                return False
            self._pending_paths.add(key)
            return True

    def get_nowait(self) -> str:
        with self._lock:
            value = self._queue.get_nowait()
            self._pending_paths.discard(self._path_key(value))
            return value

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    @property
    def full_reconcile_required(self) -> bool:
        with self._lock:
            return self._full_reconcile_required

    @property
    def reconcile_generation(self) -> int:
        with self._lock:
            return self._reconcile_generation

    def claim_full_reconcile_marker(
        self,
        retry_interval_seconds: float = 30.0,
        *,
        now: float | None = None,
    ) -> int | None:
        """Claim the current generation no more often than the retry interval."""
        with self._lock:
            if not self._full_reconcile_required:
                return None
            current_time = time.monotonic() if now is None else float(now)
            if current_time < self._next_reconcile_attempt_at:
                return None
            if (
                self._persist_reconcile_marker
                and not self._full_reconcile_marker_exists_locked()
                and not self._persist_full_reconcile_locked()
            ):
                self._next_reconcile_attempt_at = current_time + max(
                    1.0,
                    float(retry_interval_seconds),
                )
                raise RuntimeError(
                    "Wiki reconciliation marker is not durable; refusing the claim"
                )
            self._next_reconcile_attempt_at = current_time + max(
                1.0,
                float(retry_interval_seconds),
            )
            return self._reconcile_generation

    def allow_immediate_full_reconcile_retry(self) -> bool:
        """Let a successful partial reconcile claim the next batch immediately."""
        with self._lock:
            if not self._full_reconcile_required:
                return False
            self._next_reconcile_attempt_at = 0.0
            return True

    def clear_full_reconcile(self, expected_generation: int) -> bool:
        """CAS-acknowledge reconciliation without losing concurrent events."""
        with self._lock:
            if (
                not self._full_reconcile_required
                or self._reconcile_generation != int(expected_generation)
            ):
                return False
            if not self._remove_full_reconcile_marker_locked():
                self._next_reconcile_attempt_at = 0.0
                return False
            self._full_reconcile_required = False
            self._next_reconcile_attempt_at = 0.0
            self._invalidate_reconcile_plan_locked()
            return True


index_queue = WikiIndexEventBuffer(persist_reconcile_marker=True)


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
        if not _is_canonical_wiki_filename(filename):
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
    def __init__(self, stop_event: threading.Event | None = None):
        self.stop_event = stop_event or threading.Event()
        self.sync_process = None
        self.sync_dirty = False
        self.last_triggered = {}
        self.lock = threading.Lock()
        self.monitor_thread: threading.Thread | None = None
        self.shutting_down = False

    def _process_is_running_locked(self) -> bool:
        if self.sync_process is None:
            return False
        try:
            return self.sync_process.poll() is None
        except Exception:
            return True

    def _monitor_sync_process(self, process) -> None:
        try:
            while process.poll() is None:
                if self.stop_event.wait(0.05):
                    return
        except Exception as exc:
            log.warning("Could not monitor diary sync process: %s", exc)
        with self.lock:
            if self.sync_process is not process:
                return
            self.sync_process = None
            if self.shutting_down or self.stop_event.is_set() or not self.sync_dirty:
                self.sync_dirty = False
                return
            self.sync_dirty = False
            self._launch_sync_locked("coalesced diary update")

    def _launch_sync_locked(self, filename: str) -> bool:
        import subprocess
        import sys

        if self.shutting_down or self.stop_event.is_set():
            return False
        sync_script = os.path.expanduser("~/.gemini/scripts/sync_focus.py")
        if not os.path.exists(sync_script):
            return False
        log.info("Diary modified: %s. Triggering sync_focus.py...", filename)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = subprocess.Popen(
                [sys.executable, sync_script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.error("Failed to trigger sync_focus.py: %s", exc)
            return False
        self.sync_process = process
        monitor = threading.Thread(
            target=self._monitor_sync_process,
            args=(process,),
            daemon=True,
            name="vector-lake-diary-sync-monitor",
        )
        self.monitor_thread = monitor
        monitor.start()
        return True

    def handle_event(self, event):
        if event.is_directory or self.stop_event.is_set():
            return
        filepath = event.src_path
        filename = os.path.basename(filepath)
        if not filename.casefold().endswith(".md"):
            return

        now = time.time()
        with self.lock:
            if self.shutting_down or self.stop_event.is_set():
                return
            if self._process_is_running_locked():
                self.sync_dirty = True
                self.last_triggered[filepath] = now
                log.info(
                    "Diary sync already running; queued a trailing sync for %s.",
                    filename,
                )
                return
            if self.sync_process is not None:
                self.sync_process = None
                self.sync_dirty = False

            if len(self.last_triggered) > 2000:
                self.last_triggered.clear()
            if (
                filepath in self.last_triggered
                and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS
            ):
                return
            self.last_triggered[filepath] = now
            self._launch_sync_locked(filename)

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        timeout = (
            _shutdown_timeout_seconds()
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        deadline = time.monotonic() + timeout
        with self.lock:
            self.shutting_down = True
            self.sync_dirty = False
            process = self.sync_process
            monitor = self.monitor_thread

        if process is not None and self._process_is_running_locked():
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except Exception as exc:
                    log.warning("Could not terminate diary sync process: %s", exc)
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if process.poll() is None:
                kill = getattr(process, "kill", None)
                if callable(kill):
                    try:
                        kill()
                    except Exception as exc:
                        log.warning("Could not kill diary sync process: %s", exc)

        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=max(0.0, deadline - time.monotonic()))
        monitor_alive = bool(monitor is not None and monitor.is_alive())
        process_alive = bool(
            process is not None
            and getattr(process, "poll", lambda: None)() is None
        )
        if monitor_alive or process_alive:
            log.warning(
                "Diary watchdog shutdown timed out: monitor_alive=%s process_alive=%s",
                monitor_alive,
                process_alive,
            )
        return not monitor_alive and not process_alive

    def on_created(self, event):
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)

class RawWatchdogHandler(FileSystemEventHandler):
    def __init__(
        self,
        retry_base_seconds: float = 1.0,
        stop_event: threading.Event | None = None,
    ):
        self.stop_event = stop_event or threading.Event()
        self.last_triggered = {}
        self.lock = threading.RLock()
        self.sync_thread: threading.Thread | None = None
        self.sync_future = None
        self.pending_paths = set()
        self.pending_overflow = False
        self.shutting_down = False
        self.retry_attempt = 0
        self.retry_timer = None
        self.retry_base_seconds = max(0.01, float(retry_base_seconds))

    @staticmethod
    def _full_scan_complete(result) -> bool:
        return "No new files to ingest." in str(result or "")

    def _run_ingest(self, paths, overflow):
        from vector_lake.tool_ingest import prepare_ingest_batch

        return prepare_ingest_batch(
            batch_size=50 if overflow else max(1, len(paths)),
            candidate_paths=None if overflow else paths,
        )

    def _queue_batch_locked(self, paths, overflow) -> None:
        max_pending = _positive_env_int("VECTOR_LAKE_RAW_EVENT_BUFFER", 500)
        for path in paths:
            if len(self.pending_paths) < max_pending:
                self.pending_paths.add(str(path))
            else:
                overflow = True
        self.pending_overflow = self.pending_overflow or bool(overflow)

    def _retry_timer_fired(self) -> None:
        with self.lock:
            self.retry_timer = None
            self._submit_pending_locked()

    def _schedule_retry_locked(self) -> None:
        if self.shutting_down or self.stop_event.is_set() or self.retry_timer is not None:
            return
        delay = min(
            60.0,
            self.retry_base_seconds * (2 ** min(max(0, self.retry_attempt - 1), 10)),
        )
        timer = threading.Timer(delay, self._retry_timer_fired)
        timer.daemon = True
        self.retry_timer = timer
        timer.start()

    def _run_ingest_future(self, future, paths, overflow) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = self._run_ingest(paths, overflow)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def _submit_pending_locked(self):
        if self.shutting_down or self.stop_event.is_set() or self.retry_timer is not None:
            return
        if self.sync_future is not None and not self.sync_future.done():
            return
        if not self.pending_paths and not self.pending_overflow:
            return
        import concurrent.futures

        paths = sorted(self.pending_paths)
        overflow = self.pending_overflow
        self.pending_paths.clear()
        self.pending_overflow = False
        future = concurrent.futures.Future()
        worker = threading.Thread(
            target=self._run_ingest_future,
            args=(future, paths, overflow),
            daemon=True,
            name="vector-lake-raw-ingest",
        )
        self.sync_future = future
        self.sync_thread = worker
        try:
            worker.start()
        except Exception:
            self.sync_future = None
            self.sync_thread = None
            self._queue_batch_locked(paths, overflow)
            raise
        future.add_done_callback(
            lambda completed, submitted_paths=paths, submitted_overflow=overflow: (
                self._ingest_done(
                    completed,
                    submitted_paths,
                    submitted_overflow,
                )
            )
        )
    def _ingest_done(self, future, paths, overflow):
        result = None
        try:
            result = future.result()
            error = None
        except BaseException as exc:
            error = exc
            log.error("Raw ingest preparation failed: %s", error)
        with self.lock:
            if self.sync_future is future:
                self.sync_future = None
                self.sync_thread = None
            if error is not None:
                self._queue_batch_locked(paths, overflow)
                self.retry_attempt += 1
            else:
                self.retry_attempt = 0
                if overflow and not self._full_scan_complete(result):
                    self.pending_overflow = True
            if self.shutting_down or self.stop_event.is_set():
                return
            if error is not None:
                self._schedule_retry_locked()
            else:
                self._submit_pending_locked()

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        """Stop accepting work and wait only within the configured deadline."""
        import concurrent.futures

        timeout = (
            _shutdown_timeout_seconds()
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        deadline = time.monotonic() + timeout
        with self.lock:
            self.shutting_down = True
            retry_timer = self.retry_timer
            self.retry_timer = None
            worker_thread = self.sync_thread
            future = self.sync_future

        if retry_timer is not None:
            retry_timer.cancel()
            retry_timer.join(timeout=max(0.0, deadline - time.monotonic()))

        future_done = future is None or future.done()
        if future is not None and not future_done:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
                future_done = True
            except concurrent.futures.TimeoutError:
                future_done = False
            except BaseException:
                future_done = True

        if worker_thread is not None and worker_thread is not threading.current_thread():
            worker_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        worker_done = worker_thread is None or not worker_thread.is_alive()
        if not future_done or not worker_done:
            log.warning("Raw ingest shutdown timed out with in-flight preparation.")
            return False

        with self.lock:
            paths = sorted(self.pending_paths)
            overflow = self.pending_overflow
            self.pending_paths.clear()
            self.pending_overflow = False
            self.sync_thread = None
            self.sync_future = None

        drain_error: list[BaseException] = []
        if paths or overflow:
            def drain_pending() -> None:
                try:
                    result = self._run_ingest(paths, overflow)
                    if overflow and not self._full_scan_complete(result):
                        log.warning(
                            "Raw ingest shutdown left additional full-scan work pending."
                        )
                except BaseException as exc:
                    drain_error.append(exc)

            drain_thread = threading.Thread(
                target=drain_pending,
                daemon=True,
                name="vector-lake-raw-shutdown-drain",
            )
            drain_thread.start()
            drain_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if drain_thread.is_alive():
                log.warning("Raw ingest shutdown drain exceeded its deadline.")
                return False
        if drain_error:
            log.error("Raw ingest shutdown drain failed: %s", drain_error[0])
            return False
        return True
    def handle_event(self, event):
        if event.is_directory:
            return
        filepath = getattr(event, "dest_path", None) or event.src_path

        # Prevent Double-Trigger: Exclude privacy/Diary (handled by DiaryWatchdogHandler)
        if "privacy" in filepath and "Diary" in filepath:
            return

        filename = os.path.basename(filepath)
        if filename.startswith(".") or filename.endswith(".tmp"):
            return

        now = time.time()
        with self.lock:
            if self.shutting_down or self.stop_event.is_set():
                return
            if len(self.last_triggered) > 2000:
                self.last_triggered.clear()
            if (
                filepath in self.last_triggered
                and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS
            ):
                return
            self.last_triggered[filepath] = now
            self._queue_batch_locked([str(Path(filepath).resolve())], False)
            self._submit_pending_locked()

        log.info(
            "Raw source modified: %s. Scheduled path-scoped ingest preparation.",
            filename,
        )

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


def process_legacy_projection_queue_batch(
    event_buffer: WikiIndexEventBuffer,
    filenames,
) -> dict:
    """Process ephemeral Wiki events and retain a durable retry on failure."""
    stats = process_legacy_projection_batch(filenames)
    if stats["failed"]:
        event_buffer.require_full_reconcile()
    return stats


def _collect_wiki_projection_drift(selected_limit: int) -> dict:
    """Collect a bounded candidate plan while counting all current drift."""
    from vector_lake import governance_store
    from vector_lake.wiki_utils import get_wiki_dir, iter_markdown_files

    selected_limit = max(1, int(selected_limit))
    wiki_dir = get_wiki_dir()
    wiki_paths = {
        path.stem: path
        for path in sorted(iter_markdown_files(wiki_dir), key=lambda item: item.name)
    }
    canonical_versions = governance_store.canonical_page_versions()
    candidates = []
    errors = []
    total_drift = 0

    for page_key in sorted(set(wiki_paths) | set(canonical_versions)):
        path = wiki_paths.get(page_key)
        canonical_version = canonical_versions.get(page_key)
        filename = path.name if path is not None else f"{page_key}.md"
        if not _is_canonical_wiki_filename(filename):
            continue

        if path is None or canonical_version is None:
            drifted = True
        else:
            try:
                content = path.read_text(encoding="utf-8")
                observed_version = governance_store.canonical_page_version_from_content(
                    filename,
                    content,
                )
                drifted = observed_version != canonical_version
            except Exception as exc:
                if len(errors) < 100:
                    errors.append(f"{filename}: {exc}")
                continue

        if not drifted:
            continue
        total_drift += 1
        if len(candidates) < selected_limit:
            candidates.append(filename)

    return {
        "candidates": candidates,
        "errors": errors,
        "total_drift": total_drift,
    }


def _scan_wiki_projection_drift(limit: int = 25) -> dict:
    """Compatibility scan that exposes at most one projection batch."""
    return _collect_wiki_projection_drift(max(1, min(25, int(limit))))


def _scan_wiki_reconcile_plan(limit: int = 10_000) -> dict:
    """Build one bounded generation-fenced in-process reconciliation plan."""
    return _collect_wiki_projection_drift(max(1, min(50_000, int(limit))))


def reconcile_wiki_overflow_once(
    event_buffer: WikiIndexEventBuffer,
    expected_generation: int,
    batch_size: int = 25,
) -> dict:
    """Process one plan batch and scan only at plan creation/final verification."""
    selected_limit = max(1, min(25, int(batch_size)))
    plan_limit = max(
        selected_limit,
        min(
            50_000,
            _positive_env_int("VECTOR_LAKE_WIKI_RECONCILE_PLAN_LIMIT", 10_000),
        ),
    )
    scan = None
    selected = event_buffer.reconcile_plan_batch(
        expected_generation,
        selected_limit,
    )

    if selected is None:
        scan = _scan_wiki_reconcile_plan(limit=plan_limit)
        if scan["errors"]:
            return {
                "generation": expected_generation,
                "selected": 0,
                "completed": 0,
                "failed": 0,
                "remaining": scan["total_drift"],
                "scan_errors": scan["errors"],
                "generation_changed": (
                    event_buffer.reconcile_generation != expected_generation
                ),
                "cleared": False,
            }
        if not event_buffer.install_reconcile_plan(
            expected_generation,
            scan["candidates"],
            scan["total_drift"],
        ):
            event_buffer.allow_immediate_full_reconcile_retry()
            return {
                "generation": expected_generation,
                "selected": 0,
                "completed": 0,
                "failed": 0,
                "remaining": scan["total_drift"],
                "scan_errors": [],
                "generation_changed": True,
                "cleared": False,
            }
        selected = event_buffer.reconcile_plan_batch(
            expected_generation,
            selected_limit,
        )
        if selected is None:
            event_buffer.allow_immediate_full_reconcile_retry()
            return {
                "generation": expected_generation,
                "selected": 0,
                "completed": 0,
                "failed": 0,
                "remaining": scan["total_drift"],
                "scan_errors": [],
                "generation_changed": True,
                "cleared": False,
            }

    legacy_stats = {"completed": 0, "failed": 0}
    if selected:
        legacy_stats = process_legacy_projection_batch(selected)
        if legacy_stats["failed"]:
            remaining_state = event_buffer.reconcile_plan_remaining(
                expected_generation
            )
            remaining = sum(remaining_state) if remaining_state is not None else 0
            return {
                "generation": expected_generation,
                "selected": len(selected),
                "completed": legacy_stats["completed"],
                "failed": legacy_stats["failed"],
                "remaining": remaining,
                "scan_errors": [],
                "generation_changed": (
                    event_buffer.reconcile_generation != expected_generation
                ),
                "cleared": False,
            }
        if not event_buffer.acknowledge_reconcile_plan_batch(
            expected_generation,
            selected,
        ):
            event_buffer.allow_immediate_full_reconcile_retry()
            return {
                "generation": expected_generation,
                "selected": len(selected),
                "completed": legacy_stats["completed"],
                "failed": 0,
                "remaining": 0,
                "scan_errors": [],
                "generation_changed": True,
                "cleared": False,
            }

    remaining_state = event_buffer.reconcile_plan_remaining(expected_generation)
    if remaining_state is None:
        event_buffer.allow_immediate_full_reconcile_retry()
        return {
            "generation": expected_generation,
            "selected": len(selected),
            "completed": legacy_stats["completed"],
            "failed": legacy_stats["failed"],
            "remaining": 0,
            "scan_errors": [],
            "generation_changed": True,
            "cleared": False,
        }
    queued_remaining, unplanned = remaining_state
    if queued_remaining:
        event_buffer.allow_immediate_full_reconcile_retry()
        return {
            "generation": expected_generation,
            "selected": len(selected),
            "completed": legacy_stats["completed"],
            "failed": 0,
            "remaining": queued_remaining + unplanned,
            "scan_errors": [],
            "generation_changed": False,
            "cleared": False,
        }

    # A newly installed empty plan already is a valid final scan. Any consumed
    # plan needs one final scan, which can also seed the next capped plan.
    verification = (
        scan
        if not selected and scan is not None
        else _scan_wiki_reconcile_plan(limit=plan_limit)
    )
    if verification["errors"]:
        return {
            "generation": expected_generation,
            "selected": len(selected),
            "completed": legacy_stats["completed"],
            "failed": 0,
            "remaining": verification["total_drift"],
            "scan_errors": verification["errors"],
            "generation_changed": (
                event_buffer.reconcile_generation != expected_generation
            ),
            "cleared": False,
        }

    if verification["total_drift"] == 0:
        cleared = event_buffer.clear_full_reconcile(expected_generation)
        return {
            "generation": expected_generation,
            "selected": len(selected),
            "completed": legacy_stats["completed"],
            "failed": 0,
            "remaining": 0,
            "scan_errors": [],
            "generation_changed": not cleared,
            "cleared": cleared,
        }

    installed = event_buffer.install_reconcile_plan(
        expected_generation,
        verification["candidates"],
        verification["total_drift"],
    )
    event_buffer.allow_immediate_full_reconcile_retry()
    return {
        "generation": expected_generation,
        "selected": len(selected),
        "completed": legacy_stats["completed"],
        "failed": 0,
        "remaining": verification["total_drift"],
        "scan_errors": [],
        "generation_changed": not installed,
        "cleared": False,
    }

def index_worker_loop(stop_event: threading.Event | None = None):
    """Drain projection work until the watchdog requests a coordinated stop."""
    stop_event = stop_event or threading.Event()
    log.info("Outbox Consumer Thread started.")
    consecutive_failures = 0
    max_failures = 5
    backoff_base = 2
    reconcile_retry_seconds = max(
        5,
        min(
            300,
            _positive_env_int("VECTOR_LAKE_WIKI_RECONCILE_RETRY_SECONDS", 30),
        ),
    )

    write_status(
        "idle",
        0,
        index_queue.qsize(),
        "Outbox consumer started",
        "",
        component="outbox",
    )

    while not stop_event.is_set():
        try:
            if consecutive_failures >= max_failures:
                write_status(
                    "halted",
                    0,
                    index_queue.qsize(),
                    "Outbox Consumer Halted",
                    "Max consecutive failures reached",
                    component="outbox",
                )
                log.error("Outbox Consumer Halted. Entering 60s cooldown before retry.")
                if stop_event.wait(60):
                    break
                consecutive_failures = 0
                continue

            from vector_lake.wiki_utils import get_outbox_signal_path

            flag_path = get_outbox_signal_path()
            if flag_path.exists():
                try:
                    flag_path.unlink()
                except OSError:
                    pass

            with global_task_lock:
                stats = process_mutation_outbox_batch(limit=50)

            if consecutive_failures:
                write_status(
                    "idle",
                    0,
                    index_queue.qsize(),
                    "Outbox consumer recovered",
                    "",
                    component="outbox",
                )

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

            # Manual filesystem edits remain bounded and drain before overflow scans.
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
                legacy_stats = process_legacy_projection_queue_batch(
                    index_queue,
                    pending_legacy,
                )
                write_status(
                    "error" if legacy_stats["failed"] else "idle",
                    legacy_stats["completed"],
                    index_queue.qsize(),
                    f"Legacy projection batch completed: {legacy_stats}",
                    (
                        f"{legacy_stats['failed']} manual edit(s) failed"
                        if legacy_stats["failed"]
                        else ""
                    ),
                    component="outbox",
                )

            reconcile_stats = None
            if not pending_legacy and index_queue.empty():
                generation = index_queue.claim_full_reconcile_marker(
                    retry_interval_seconds=reconcile_retry_seconds,
                )
                if generation is not None:
                    log.warning(
                        "Reconciling Wiki overflow generation %s at capacity %s.",
                        generation,
                        index_queue.max_pending,
                    )
                    with global_task_lock:
                        reconcile_stats = reconcile_wiki_overflow_once(
                            index_queue,
                            generation,
                            batch_size=25,
                        )
                    cleared = reconcile_stats["cleared"]
                    write_status(
                        "idle" if cleared else "error",
                        reconcile_stats["completed"],
                        index_queue.qsize(),
                        (
                            "Full Wiki reconciliation completed"
                            if cleared
                            else "Full Wiki reconciliation pending"
                        ),
                        (
                            ""
                            if cleared
                            else (
                                f"remaining={reconcile_stats['remaining']}; "
                                f"failed={reconcile_stats['failed']}; "
                                f"scan_errors={len(reconcile_stats['scan_errors'])}; "
                                f"generation_changed={reconcile_stats['generation_changed']}"
                            )
                        ),
                        component="outbox",
                    )
                    log.info("Wiki overflow reconciliation: %s", reconcile_stats)

            if not pending_legacy and reconcile_stats is None and not stats["claimed"]:
                if index_queue.full_reconcile_required:
                    write_status(
                        "error",
                        0,
                        index_queue.qsize(),
                        "Full Wiki reconciliation pending",
                        f"retry interval={reconcile_retry_seconds}s",
                        component="outbox",
                    )
                else:
                    write_status(
                        "idle",
                        0,
                        index_queue.qsize(),
                        "Outbox idle",
                        "",
                        component="outbox",
                    )
                if stop_event.wait(1):
                    break

            consecutive_failures = 0

        except Exception as exc:
            consecutive_failures += 1
            log.error(f"Outbox worker error: {exc}")
            write_status(
                "error",
                0,
                index_queue.qsize(),
                "Outbox consumer exception",
                str(exc),
                component="outbox",
            )
            if stop_event.wait(min(backoff_base ** consecutive_failures, 60)):
                break
        finally:
            from vector_lake.db_store import close_connection

            close_connection()

    write_status(
        "stopped",
        0,
        index_queue.qsize(),
        "Outbox consumer stopped",
        "",
        component="outbox",
    )


def expire_stale_ingest_jobs_for_watchdog() -> int:
    """Run the bounded ingest expiry used by the hourly scheduler."""
    from vector_lake.db_store import expire_stale_subagent_jobs

    max_age_seconds = max(
        60,
        int(os.environ.get("VECTOR_LAKE_INGEST_TASK_MAX_AGE_SECONDS", "86400")),
    )
    return expire_stale_subagent_jobs(max_age_seconds=max_age_seconds)


def scheduled_lint_loop(stop_event: threading.Event | None = None):
    """Run scheduled maintenance until the shared stop event is set."""
    stop_event = stop_event or threading.Event()
    log.info("Scheduled Lint Worker Thread started.")
    last_run_date_hour = ""
    last_expiry_date_hour = ""

    write_status(
        "idle",
        0,
        index_queue.qsize(),
        "Scheduled lint worker started",
        "",
        component="scheduler",
    )

    while not stop_event.is_set():
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
                wait_seconds = 60
            else:
                wait_seconds = 30

            write_status(
                "idle",
                0,
                index_queue.qsize(),
                "Scheduled lint heartbeat",
                "",
                component="scheduler",
            )
            if stop_event.wait(wait_seconds):
                break
                
        except Exception as exc:
            log.error(f"Scheduled lint worker error: {exc}")
            write_status("error", 0, index_queue.qsize(), "Scheduled lint exception", str(exc), component="scheduler")
            if stop_event.wait(60):
                break

    write_status(
        "stopped",
        0,
        index_queue.qsize(),
        "Scheduled lint worker stopped",
        "",
        component="scheduler",
    )


def _start_watchdog_locked(stop_event: threading.Event | None = None):
    stop_event = stop_event or threading.Event()
    if index_queue.restore_full_reconcile_marker():
        log.warning("Restored pending Wiki reconciliation marker after restart.")

    from vector_lake.ingest_worker import start_worker

    worker_threads = {
        "outbox": threading.Thread(
            target=index_worker_loop,
            args=(stop_event,),
            daemon=True,
            name="vector-lake-outbox-worker",
        ),
        "scheduler": threading.Thread(
            target=scheduled_lint_loop,
            args=(stop_event,),
            daemon=True,
            name="vector-lake-scheduled-lint-worker",
        ),
        "ingest": threading.Thread(
            target=start_worker,
            args=(stop_event,),
            daemon=True,
            name="vector-lake-ingest-worker",
        ),
    }
    _register_background_threads(worker_threads)
    started_workers: dict[str, threading.Thread] = {}

    observer = Observer()
    observer_started = False
    raw_handler = None
    diary_handler = None
    failure_reason = ""
    watch_dirs = _watch_directories()

    try:
        for name, thread in worker_threads.items():
            try:
                thread.start()
            except Exception:
                failure_reason = f"background worker failed to start: {name}"
                raise
            started_workers[name] = thread

        wiki_dir = str(watch_dirs["wiki"])
        if os.path.exists(wiki_dir):
            wiki_handler = WikiIndexHandler()
            observer.schedule(wiki_handler, wiki_dir, recursive=False)
            log.info("Wiki AST monitor active on directory: %s", wiki_dir)

        diary_dir = str(watch_dirs["diary"])
        if os.path.exists(diary_dir):
            diary_handler = DiaryWatchdogHandler(stop_event=stop_event)
            observer.schedule(diary_handler, diary_dir, recursive=False)
            log.info("Diary monitor active on directory: %s", diary_dir)

        raw_dir = str(watch_dirs["raw"])
        if os.path.exists(raw_dir):
            raw_handler = RawWatchdogHandler(stop_event=stop_event)
            observer.schedule(raw_handler, raw_dir, recursive=True)
            log.info("Raw source monitor active on directory: %s", raw_dir)

        observer.start()
        observer_started = True
        log.info(
            "Vector Lake Watchdog Agent is running in Background Index/Lint mode."
        )
        write_status(
            "idle",
            0,
            index_queue.qsize(),
            "Watchdog started",
            "",
            component="watchdog",
        )
        heartbeat_seconds = _bounded_env_float(
            "VECTOR_LAKE_WATCHDOG_HEARTBEAT_SECONDS",
            30.0,
            minimum=1.0,
            maximum=120.0,
        )
        monitor_seconds = _bounded_env_float(
            "VECTOR_LAKE_WATCHDOG_MONITOR_SECONDS",
            1.0,
            minimum=0.05,
            maximum=10.0,
        )
        last_heartbeat = 0.0
        while not stop_event.wait(monitor_seconds):
            dead_workers = sorted(
                name for name, thread in started_workers.items() if not thread.is_alive()
            )
            if dead_workers:
                failure_reason = "background worker stopped unexpectedly: " + ",".join(
                    dead_workers
                )
                for component in dead_workers:
                    write_status(
                        "halted",
                        0,
                        index_queue.qsize(),
                        "Background worker died",
                        failure_reason,
                        component=component,
                    )
                log.error(failure_reason)
                stop_event.set()
                break

            observer_is_alive = getattr(observer, "is_alive", None)
            if callable(observer_is_alive) and not observer_is_alive():
                failure_reason = "filesystem observer stopped unexpectedly"
                write_status(
                    "halted",
                    0,
                    index_queue.qsize(),
                    "Filesystem observer died",
                    failure_reason,
                    component="watchdog",
                )
                log.error(failure_reason)
                stop_event.set()
                break

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds:
                write_status(
                    "idle",
                    0,
                    index_queue.qsize(),
                    "Watchdog heartbeat",
                    "",
                    component="watchdog",
                )
                last_heartbeat = now
    except KeyboardInterrupt:
        log.info("Termination signal received. Shutting down Watchdog...")
    finally:
        stop_event.set()
        deadline = time.monotonic() + _shutdown_timeout_seconds()
        shutdown_failures: list[str] = []

        if observer_started:
            try:
                observer.stop()
            except Exception as exc:
                shutdown_failures.append(f"observer_stop:{exc}")

        if raw_handler is not None:
            remaining = max(0.0, deadline - time.monotonic())
            if not raw_handler.shutdown(timeout_seconds=remaining):
                shutdown_failures.append("raw_handler_timeout")

        if diary_handler is not None:
            remaining = max(0.0, deadline - time.monotonic())
            if not diary_handler.shutdown(timeout_seconds=remaining):
                shutdown_failures.append("diary_handler_timeout")

        if observer_started:
            remaining = max(0.0, deadline - time.monotonic())
            observer.join(timeout=remaining)
            observer_is_alive = getattr(observer, "is_alive", None)
            if callable(observer_is_alive) and observer_is_alive():
                shutdown_failures.append("observer_join_timeout")

        remaining = max(0.0, deadline - time.monotonic())
        alive_workers = _join_threads_bounded(started_workers, remaining)
        if alive_workers:
            shutdown_failures.append("worker_join_timeout:" + ",".join(alive_workers))

        shutdown_detail = "; ".join(
            item for item in (failure_reason, *shutdown_failures) if item
        )
        write_status(
            "halted" if shutdown_detail else "stopped",
            0,
            index_queue.qsize(),
            "Watchdog shutdown incomplete" if shutdown_detail else "Watchdog stopped",
            shutdown_detail,
            component="watchdog",
        )


def start_watchdog(stop_event: threading.Event | None = None):
    """Run exactly one watchdog instance for the active MEMORY root."""
    from filelock import FileLock, Timeout
    from vector_lake.wiki_utils import get_meta_dir

    instance_lock = FileLock(str(get_meta_dir() / ".watchdog.instance.lock"))
    try:
        instance_lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError(
            "A Vector Lake watchdog instance is already running for this MEMORY root."
        ) from exc
    try:
        return _start_watchdog_locked(stop_event=stop_event)
    finally:
        instance_lock.release()

if __name__ == "__main__":
    start_watchdog()
