import hashlib
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from itertools import islice
import json
from vector_lake import get_extension_root
from vector_lake.watchdog_status import (
    begin_watchdog_run,
    current_watchdog_run_id,
    write_status,
)

# Load config
CONFIG_PATH = get_extension_root() / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

EXCLUDE_PATHS = config.get("exclude_paths", [])

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print(
        "Error: `watchdog` library is not installed. Please run `pip install watchdog`.",
        flush=True,
    )
    import sys

    sys.exit(1)


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
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
_RAW_EVENT_LOG_LOCK = threading.Lock()
_RAW_EVENT_LOG_WINDOW_STARTED = 0.0
_RAW_EVENT_LOG_SUPPRESSED = 0
_RAW_EVENT_LOG_FILENAMES: set[str] = set()


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


def _auto_ingest_drain_timeout_seconds() -> float:
    return _bounded_env_float(
        "VECTOR_LAKE_AUTO_INGEST_DRAIN_TIMEOUT_SECONDS",
        300.0,
        minimum=1.0,
        maximum=3600.0,
    )


def _outbox_batch_yield_seconds() -> float:
    """Return the interruptible pause after a successful durable outbox batch."""
    return _bounded_env_float(
        "VECTOR_LAKE_OUTBOX_BATCH_YIELD_SECONDS",
        0.05,
        minimum=0.001,
        maximum=1.0,
    )


def _topology_refresh_deadline(
    *,
    now: float,
    dirty_since: float | None,
) -> tuple[float, float]:
    """Coalesce projection batches while enforcing a maximum dirty age."""
    first_dirty = float(now if dirty_since is None else dirty_since)
    debounce = _bounded_env_float(
        "VECTOR_LAKE_TOPOLOGY_REFRESH_DEBOUNCE_SECONDS",
        5.0,
        minimum=0.1,
        maximum=60.0,
    )
    max_staleness = _bounded_env_float(
        "VECTOR_LAKE_TOPOLOGY_MAX_STALENESS_SECONDS",
        300.0,
        minimum=1.0,
        maximum=3600.0,
    )
    return first_dirty, min(float(now) + debounce, first_dirty + max_staleness)


def _raw_event_log_window_seconds() -> float:
    return _bounded_env_float(
        "VECTOR_LAKE_RAW_EVENT_LOG_WINDOW_SECONDS",
        5.0,
        minimum=0.1,
        maximum=60.0,
    )


def _log_raw_event(filename: str) -> None:
    """Emit one detail line per small window and aggregate repeated events."""
    global _RAW_EVENT_LOG_FILENAMES
    global _RAW_EVENT_LOG_SUPPRESSED
    global _RAW_EVENT_LOG_WINDOW_STARTED

    now = time.monotonic()
    with _RAW_EVENT_LOG_LOCK:
        if not _RAW_EVENT_LOG_WINDOW_STARTED:
            _RAW_EVENT_LOG_WINDOW_STARTED = now
            log.info(
                "Raw source modified: %s. Scheduled path-scoped ingest preparation.",
                filename,
            )
            return
        elapsed = now - _RAW_EVENT_LOG_WINDOW_STARTED
        if elapsed < _raw_event_log_window_seconds():
            _RAW_EVENT_LOG_SUPPRESSED += 1
            if len(_RAW_EVENT_LOG_FILENAMES) < 50:
                _RAW_EVENT_LOG_FILENAMES.add(filename)
            return
        if _RAW_EVENT_LOG_SUPPRESSED:
            log.info(
                "Raw source events aggregated: suppressed=%s unique_files=%s "
                "window_seconds=%.2f",
                _RAW_EVENT_LOG_SUPPRESSED,
                len(_RAW_EVENT_LOG_FILENAMES),
                elapsed,
            )
        _RAW_EVENT_LOG_WINDOW_STARTED = now
        _RAW_EVENT_LOG_SUPPRESSED = 0
        _RAW_EVENT_LOG_FILENAMES = set()
        log.info(
            "Raw source modified: %s. Scheduled path-scoped ingest preparation.",
            filename,
        )


def background_thread_health() -> dict[str, bool]:
    """Return the liveness of the most recently registered watchdog workers."""
    with _BACKGROUND_THREADS_LOCK:
        return {name: thread.is_alive() for name, thread in _BACKGROUND_THREADS.items()}


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


def _drain_auto_ingest_worker(
    threads: dict[str, threading.Thread],
    alive_workers: list[str],
    heartbeat: Callable[[], None] | None = None,
    timeout_seconds: float | None = None,
) -> list[str]:
    """Wait for an in-flight finalizer only within a durable shutdown budget."""
    if "auto_ingest" not in alive_workers:
        return alive_workers
    auto_thread = threads.get("auto_ingest")
    timeout = (
        _auto_ingest_drain_timeout_seconds()
        if timeout_seconds is None
        else max(0.0, float(timeout_seconds))
    )
    deadline = time.monotonic() + timeout
    while (
        auto_thread is not None
        and auto_thread.is_alive()
        and time.monotonic() < deadline
    ):
        if heartbeat is not None:
            heartbeat()
        auto_thread.join(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
    if auto_thread is not None and auto_thread.is_alive():
        return alive_workers
    return [name for name in alive_workers if name != "auto_ingest"]


def _wiki_reconcile_marker_path() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "wiki_reconcile_required.json"


def watchdog_stop_request_path() -> Path:
    """Return the stable control marker used for bounded external shutdown."""
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / ".watchdog.stop"


def request_watchdog_stop() -> Path:
    """Atomically ask the active watchdog to run its normal shutdown sequence."""
    path = watchdog_stop_request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(
                {
                    "requested_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                    "requester_pid": os.getpid(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return path


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
        bounded = tuple(str(item) for item in islice(candidates, 50_000))
        with self._lock:
            if not self._full_reconcile_required or self._reconcile_generation != int(
                expected_generation
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
            if self._reconcile_plan_generation != int(
                expected_generation
            ) or self._reconcile_generation != int(expected_generation):
                return None
            end = min(
                len(self._reconcile_plan_candidates),
                self._reconcile_plan_cursor + max(1, int(batch_size)),
            )
            return list(
                self._reconcile_plan_candidates[self._reconcile_plan_cursor : end]
            )

    def acknowledge_reconcile_plan_batch(
        self,
        expected_generation: int,
        filenames,
    ) -> bool:
        """Advance only when the completed batch is exactly the current head."""
        completed = tuple(str(item) for item in filenames)
        with self._lock:
            if self._reconcile_plan_generation != int(
                expected_generation
            ) or self._reconcile_generation != int(expected_generation):
                return False
            end = self._reconcile_plan_cursor + len(completed)
            if (
                self._reconcile_plan_candidates[self._reconcile_plan_cursor : end]
                != completed
            ):
                return False
            self._reconcile_plan_cursor = end
            return True

    def reconcile_plan_remaining(
        self,
        expected_generation: int,
    ) -> tuple[int, int] | None:
        """Return queued and unplanned drift counts for the current generation."""
        with self._lock:
            if self._reconcile_plan_generation != int(
                expected_generation
            ) or self._reconcile_generation != int(expected_generation):
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
                    raise RuntimeError("Wiki reconciliation marker remains non-durable")
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
            if not self._full_reconcile_required or self._reconcile_generation != int(
                expected_generation
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


def _raw_watch_configuration() -> tuple[list[Path], str]:
    """Return one validated ingest config snapshot for watch reconciliation."""
    from vector_lake.tool_ingest import (
        _load_ingest_config,
        get_ingest_target_directories,
    )

    config_snapshot = _load_ingest_config()
    targets = get_ingest_target_directories(
        config_snapshot,
        collapse_nested=True,
    )
    token = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True)
    return targets, token


def _watch_directories(
    raw_targets: list[Path] | None = None,
) -> dict[str, Path | list[Path]]:
    from vector_lake.tool_ingest import get_ingest_target_directories
    from vector_lake.wiki_utils import get_raw_dir, get_wiki_dir

    raw_dir = get_raw_dir()
    return {
        "wiki": get_wiki_dir(),
        "raw": raw_dir,
        "raw_targets": (
            get_ingest_target_directories(collapse_nested=True)
            if raw_targets is None
            else raw_targets
        ),
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
                if db_store.is_managed_projection_state(
                    filename, "update", payload_text
                ):
                    return
            elif db_store.is_managed_projection_state(filename, "delete"):
                return
        except Exception as exc:
            log.warning("Could not classify projection event for %s: %s", filename, exc)

        now = time.time()
        with self.lock:
            if len(self.last_triggered) > 1000:
                self.last_triggered = {
                    k: v
                    for k, v in self.last_triggered.items()
                    if (now - v) <= DEBOUNCE_SECONDS * 2
                }

            if (
                filepath in self.last_triggered
                and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS
            ):
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
            process is not None and getattr(process, "poll", lambda: None)() is None
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
        from vector_lake.tool_ingest import FULL_SCAN_COMPLETE_TOKEN

        lines = str(result or "").splitlines()
        return bool(lines) and lines[0].strip() == FULL_SCAN_COMPLETE_TOKEN

    def request_full_scan(self) -> None:
        """Schedule a bounded startup/recovery scan through the single-flight queue."""
        with self.lock:
            self._queue_batch_locked([], True)
            self._submit_pending_locked()

    def _run_ingest(self, paths, overflow):
        from vector_lake.tool_ingest import prepare_ingest_batch

        options = {
            "batch_size": 50 if overflow else max(1, len(paths)),
            "candidate_paths": None if overflow else paths,
        }
        if overflow:
            options["_enqueue_all"] = True
            from vector_lake.heavy_task_gate import heavy_task

            with heavy_task(
                "ingest_scan",
                "watchdog-raw-full-scan",
                origin="watchdog",
                wait_timeout_seconds=0,
                warn_after_seconds=1800,
            ):
                return prepare_ingest_batch(**options)
        return prepare_ingest_batch(**options)

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
        if (
            self.shutting_down
            or self.stop_event.is_set()
            or self.retry_timer is not None
        ):
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
        if (
            self.shutting_down
            or self.stop_event.is_set()
            or self.retry_timer is not None
        ):
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
            from vector_lake.heavy_task_gate import HeavyTaskBusy

            if isinstance(error, HeavyTaskBusy):
                log.info(
                    "Raw full scan deferred because the heavy-task gate is occupied."
                )
            else:
                log.error("Raw ingest preparation failed: %s", error)
        with self.lock:
            if self.sync_future is future:
                self.sync_future = None
                self.sync_thread = None
            if error is not None:
                self._queue_batch_locked(paths, overflow)
                from vector_lake.heavy_task_gate import HeavyTaskBusy

                if not isinstance(error, HeavyTaskBusy):
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

        if (
            worker_thread is not None
            and worker_thread is not threading.current_thread()
        ):
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
        from vector_lake.tool_ingest import is_private_diary_path

        filepath = getattr(event, "dest_path", None) or event.src_path
        if is_private_diary_path(filepath):
            return

        filename = os.path.basename(filepath)
        if filename.startswith(".") or filename.endswith(".tmp"):
            return

        with self.lock:
            if self.shutting_down or self.stop_event.is_set():
                return
            # pending_paths is the trailing-edge debounce. An event received
            # during an in-flight preparation is retained for the next pass.
            self._queue_batch_locked([str(Path(filepath).resolve())], False)
            self._submit_pending_locked()

        _log_raw_event(filename)

    def on_created(self, event):
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)

    def on_moved(self, event):
        self.handle_event(event)


def _outbox_batch_budget_seconds() -> float:
    return _bounded_env_float(
        "VECTOR_LAKE_OUTBOX_BATCH_BUDGET_SECONDS",
        60.0,
        minimum=1.0,
        maximum=3600.0,
    )


def _outbox_lease_seconds(limit: int, budget_seconds: float) -> int:
    return max(
        120,
        min(3600, max(max(1, int(limit)), int(budget_seconds) + 30)),
    )


def _outbox_lease_renew_interval_seconds(lease_seconds: int) -> float:
    return _bounded_env_float(
        "VECTOR_LAKE_OUTBOX_LEASE_RENEW_INTERVAL_SECONDS",
        max(1.0, min(30.0, float(lease_seconds) / 3.0)),
        minimum=0.05,
        maximum=30.0,
    )


def _renew_mutation_outbox_lease(db_store, row: dict, lease_seconds: int) -> bool:
    """Renew one outbox lease under its full owner/token/generation fence."""
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    connection = db_store.get_connection()
    with db_store.transaction():
        updated = connection.execute(
            "UPDATE mutation_outbox SET lease_until = ? "
            "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
            "AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (
                lease_until,
                int(row["id"]),
                row["lease_owner"],
                row["lease_token"],
                int(row["lease_generation"]),
                now,
            ),
        )
    return bool(updated.rowcount)


def _release_mutation_outbox_lease(db_store, row: dict, reason: str) -> bool:
    """CAS-release scheduler-deferred work without charging a content attempt."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    connection = db_store.get_connection()
    with db_store.transaction():
        updated = connection.execute(
            "UPDATE mutation_outbox SET status = 'pending', available_at = ?, "
            "attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
            "last_error = ? WHERE id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (
                now,
                str(reason)[:4000],
                int(row["id"]),
                row["lease_owner"],
                row["lease_token"],
                int(row["lease_generation"]),
                now,
            ),
        )
    return bool(updated.rowcount)


def _run_with_outbox_lease_renewal(
    db_store,
    rows: list[dict],
    lease_seconds: int,
    operation: Callable[[list[dict]], None],
) -> list[dict]:
    """Keep current leases alive while one slow projection operation runs."""
    active = [
        row
        for row in rows
        if _renew_mutation_outbox_lease(db_store, row, lease_seconds)
    ]
    if not active:
        return []
    stop_renewal = threading.Event()
    interval = _outbox_lease_renew_interval_seconds(lease_seconds)

    def renew_loop() -> None:
        try:
            while not stop_renewal.wait(interval):
                for row in active:
                    try:
                        if not _renew_mutation_outbox_lease(
                            db_store,
                            row,
                            lease_seconds,
                        ):
                            log.warning(
                                "Outbox lease lost during projection work: id=%s",
                                row["id"],
                            )
                    except Exception as exc:
                        log.warning(
                            "Outbox lease renewal failed transiently for id=%s: %s",
                            row["id"],
                            exc,
                        )
        finally:
            db_store.close_connection()

    renewal_thread = threading.Thread(
        target=renew_loop,
        daemon=True,
        name="vector-lake-outbox-lease-renewer",
    )
    renewal_thread.start()
    try:
        operation(active)
    finally:
        stop_renewal.set()
        renewal_thread.join(timeout=min(5.0, interval + 0.5))
        if renewal_thread.is_alive():
            log.error("Outbox lease-renewal thread did not stop within its bound.")
    return active


def _apply_outbox_index_projection(indexer, filenames: list[str]) -> None:
    system_filenames = [
        filename for filename in filenames if indexer.is_system_page_filename(filename)
    ]
    if indexer.index_projection_matches_canonical(filenames):
        if not indexer.projection_pair_matches_current_generation():
            indexer.refresh_claim_graph_projection()
    else:
        indexer.update_index_items(filenames)
        if system_filenames and not indexer.index_projection_matches_canonical(
            system_filenames
        ):
            raise RuntimeError(
                "Selected index projection does not match canonical state after "
                "outbox indexing."
            )
    if not indexer.projection_pair_matches_current_generation():
        raise RuntimeError(
            "Projection pair is not committed against the current canonical "
            "generation after outbox indexing."
        )


def _settle_outbox_index_partition(
    rows: list[dict],
    *,
    db_store,
    indexer,
    lease_seconds: int,
    deadline: float,
    stats: dict,
    max_attempts: int,
    backoff_base: float,
) -> None:
    current_rows = [
        row
        for row in rows
        if db_store.mutation_outbox_lease_is_current(
            int(row["id"]),
            row["lease_owner"],
            row["lease_token"],
            int(row["lease_generation"]),
        )
    ]
    if not current_rows:
        return
    if time.monotonic() >= deadline:
        for row in current_rows:
            _release_mutation_outbox_lease(
                db_store,
                row,
                "Outbox batch time budget exhausted before projection indexing",
            )
        return

    try:
        active_rows = _run_with_outbox_lease_renewal(
            db_store,
            current_rows,
            lease_seconds,
            lambda held: _apply_outbox_index_projection(
                indexer,
                list(dict.fromkeys(str(row["filename"]) for row in held)),
            ),
        )
    except Exception as exc:
        if len(current_rows) > 1 and time.monotonic() < deadline:
            midpoint = len(current_rows) // 2
            log.warning(
                "Outbox index batch failed; isolating poison rows across %s/%s "
                "partitions: %s",
                midpoint,
                len(current_rows) - midpoint,
                exc,
            )
            _settle_outbox_index_partition(
                current_rows[:midpoint],
                db_store=db_store,
                indexer=indexer,
                lease_seconds=lease_seconds,
                deadline=deadline,
                stats=stats,
                max_attempts=max_attempts,
                backoff_base=backoff_base,
            )
            _settle_outbox_index_partition(
                current_rows[midpoint:],
                db_store=db_store,
                indexer=indexer,
                lease_seconds=lease_seconds,
                deadline=deadline,
                stats=stats,
                max_attempts=max_attempts,
                backoff_base=backoff_base,
            )
            return
        for row in current_rows:
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
            log.error(
                "Outbox index item %s failed for %s; status=%s: %s",
                outbox_id,
                row["filename"],
                status,
                exc,
            )
        return

    for row in active_rows:
        if db_store.complete_mutation_outbox(
            int(row["id"]),
            row["lease_owner"],
            row["lease_token"],
            int(row["lease_generation"]),
        ):
            stats["completed"] += 1


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

    budget_seconds = _outbox_batch_budget_seconds()
    deadline = time.monotonic() + budget_seconds
    lease_seconds = _outbox_lease_seconds(limit, budget_seconds)
    claim_kwargs = {
        "limit": limit,
        "lease_seconds": lease_seconds,
        "lease_owner": (
            f"watchdog:{current_watchdog_run_id()}:{os.getpid()}"
        ),
    }
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
            if time.monotonic() >= deadline:
                _release_mutation_outbox_lease(
                    db_store,
                    row,
                    "Outbox batch time budget exhausted before materialization",
                )
                continue
            if not _renew_mutation_outbox_lease(db_store, row, lease_seconds):
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
                if not db_store.mutation_outbox_lease_is_current(
                    outbox_id, *lease_args
                ):
                    continue
                materialize_markdown_projection(
                    filename,
                    row["mutation_type"],
                    row.get("payload_text"),
                    validation_mode=row.get("validation_mode") or "full",
                    projection_base_hash=row.get("projection_base_hash"),
                )
            if _renew_mutation_outbox_lease(db_store, row, lease_seconds):
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
            log.error(
                f"Outbox item {outbox_id} failed for {filename}; status={status}: {exc}"
            )
    if ready_for_index:
        _settle_outbox_index_partition(
            [row for row, _filename in ready_for_index],
            db_store=db_store,
            indexer=indexer,
            lease_seconds=lease_seconds,
            deadline=deadline,
            stats=stats,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
        )
    return stats


def process_legacy_projection_batch(filenames) -> dict:
    """Promote bounded manual Markdown edits into canonical state and durable outbox rows."""
    from vector_lake.mutation_coordinator import execute_mutation_batch
    from vector_lake.wiki_utils import get_wiki_dir, normalize_semantic_text

    stats = {"completed": 0, "failed": 0}
    wiki_dir = get_wiki_dir()
    for filename in dict.fromkeys(str(item) for item in filenames):
        target = wiki_dir / filename
        try:
            target_exists = target.exists()
            mutation = {
                "filename": filename,
                "is_delete": not target_exists,
                "expected_projection_hash": "",
            }
            if target_exists:
                content_bytes = target.read_bytes()
                mutation["content"] = normalize_semantic_text(
                    content_bytes.decode("utf-8")
                )
                mutation["expected_projection_hash"] = hashlib.sha256(
                    content_bytes
                ).hexdigest()
            execute_mutation_batch(
                [mutation], validation_mode="schema", origin="watchdog"
            )
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
            remaining_state = event_buffer.reconcile_plan_remaining(expected_generation)
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
    topology_dirty_since: float | None = None
    topology_refresh_due: float | None = None

    write_status(
        "idle",
        0,
        index_queue.qsize(),
        "Outbox consumer started",
        "",
        component="outbox",
    )

    while not stop_event.is_set():
        gate_lease = None
        gate_entered = False
        gate_failure = None
        no_completed_work = False
        successful_outbox_batch = False
        retry_wait_seconds = 0.0
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
                log.error(
                    "Outbox Consumer Halted. Entering 60s cooldown before retry."
                )
                if stop_event.wait(60):
                    break
                consecutive_failures = 0
                continue

            from vector_lake import db_store
            from vector_lake.wiki_utils import get_outbox_signal_path

            flag_path = get_outbox_signal_path()
            monotonic_now = time.monotonic()
            topology_due = bool(
                topology_refresh_due is not None
                and monotonic_now >= topology_refresh_due
            )
            try:
                claimable_outbox = db_store.mutation_outbox_has_claimable()
            except Exception:
                claimable_outbox = True
            work_pending = bool(
                flag_path.exists()
                or claimable_outbox
                or not index_queue.empty()
                or index_queue.full_reconcile_required
                or topology_due
            )
            if not work_pending:
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
                continue

            from vector_lake.heavy_task_gate import HeavyTaskBusy, heavy_task

            gate_lease = heavy_task(
                "projection",
                "watchdog-outbox-cycle",
                origin="watchdog",
                wait_timeout_seconds=0,
                warn_after_seconds=900,
            )
            try:
                gate_lease.__enter__()
                gate_entered = True
            except HeavyTaskBusy:
                write_status(
                    "idle",
                    0,
                    index_queue.qsize(),
                    "Outbox deferred by heavy-task gate",
                    "Another memory-intensive operation is active",
                    component="outbox",
                )
                if stop_event.wait(1):
                    break
                continue

            if flag_path.exists():
                try:
                    flag_path.unlink()
                except OSError:
                    pass

            with global_task_lock:
                stats = process_mutation_outbox_batch(limit=50)
            successful_outbox_batch = bool(stats["completed"])

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
                log.info("Outbox batch completed: %s", stats)

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

            projection_completed = bool(stats["completed"])
            if pending_legacy:
                legacy_stats = process_legacy_projection_queue_batch(
                    index_queue,
                    pending_legacy,
                )
                projection_completed = projection_completed or bool(
                    legacy_stats["completed"]
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
                                "scan_errors="
                                f"{len(reconcile_stats['scan_errors'])}; "
                                "generation_changed="
                                f"{reconcile_stats['generation_changed']}"
                            )
                        ),
                        component="outbox",
                    )
                    log.info("Wiki overflow reconciliation: %s", reconcile_stats)

            if reconcile_stats is not None:
                projection_completed = projection_completed or bool(
                    reconcile_stats.get("completed")
                )
            if projection_completed:
                topology_dirty_since, topology_refresh_due = (
                    _topology_refresh_deadline(
                        now=time.monotonic(),
                        dirty_since=topology_dirty_since,
                    )
                )

            if topology_due:
                from vector_lake import indexer

                with global_task_lock:
                    refreshed = indexer.refresh_graph_topology_if_dirty()
                topology_dirty_since = None
                topology_refresh_due = None
                write_status(
                    "idle",
                    0,
                    index_queue.qsize(),
                    (
                        "Graph topology refreshed after projection batches"
                        if refreshed
                        else "Graph topology already current"
                    ),
                    "",
                    component="outbox",
                )

            no_completed_work = (
                not pending_legacy
                and reconcile_stats is None
                and not stats["claimed"]
            )
            if no_completed_work:
                if index_queue.full_reconcile_required:
                    write_status(
                        "idle",
                        0,
                        index_queue.qsize(),
                        "Full Wiki reconciliation deferred",
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

            consecutive_failures = 0

        except (KeyboardInterrupt, SystemExit) as exc:
            gate_failure = exc
            raise
        except Exception as exc:
            gate_failure = exc
            consecutive_failures += 1
            log.error("Outbox worker error: %s", exc)
            write_status(
                "error",
                0,
                index_queue.qsize(),
                "Outbox consumer exception",
                str(exc),
                component="outbox",
            )
            retry_wait_seconds = min(
                backoff_base**consecutive_failures,
                60,
            )
        finally:
            from vector_lake.db_store import close_connection

            try:
                close_connection()
            finally:
                if gate_entered and gate_lease is not None:
                    gate_lease.__exit__(
                        type(gate_failure) if gate_failure is not None else None,
                        gate_failure,
                        (
                            gate_failure.__traceback__
                            if gate_failure is not None
                            else None
                        ),
                    )

        wait_seconds = (
            retry_wait_seconds
            or (_outbox_batch_yield_seconds() if successful_outbox_batch else 0.0)
            or (1.0 if no_completed_work else 0.0)
        )
        if wait_seconds and stop_event.wait(wait_seconds):
            break

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
    last_storage_sample_date = ""
    last_storage_attempt_date_hour = ""
    pending_due = ""
    next_due = ""

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
            utc_now = time.gmtime()

            # Expire abandoned subagent work once per hour instead of waiting
            # for a manual CLI invocation or one of the twice-daily lint runs.
            current_date_hour = (
                f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-{now.tm_hour}"
            )
            storage_date = (
                f"{utc_now.tm_year:04d}-{utc_now.tm_mon:02d}-{utc_now.tm_mday:02d}"
            )
            storage_date_hour = f"{storage_date}-{utc_now.tm_hour}"
            if (
                storage_date != last_storage_sample_date
                and storage_date_hour != last_storage_attempt_date_hour
            ):
                last_storage_attempt_date_hour = storage_date_hour
                try:
                    from vector_lake.storage_growth import (
                        record_storage_growth_sample,
                    )

                    record_storage_growth_sample()
                    last_storage_sample_date = storage_date
                    log.info("Daily storage growth baseline recorded for %s.", storage_date)
                except Exception as exc:
                    log.warning("Daily storage growth baseline failed: %s", exc)
            observed_due = (
                current_date_hour
                if now.tm_hour in (10, 23) and now.tm_min == 0
                else ""
            )
            if (
                observed_due
                and observed_due != last_run_date_hour
                and observed_due != pending_due
                and observed_due != next_due
            ):
                if pending_due:
                    # Coalesce multiple missed windows to the newest follow-up.
                    # The oldest pending window is always attempted first.
                    next_due = observed_due
                else:
                    pending_due = observed_due

            if now.tm_min == 0 and current_date_hour != last_expiry_date_hour:
                from vector_lake.db_store import close_connection

                try:
                    expired = expire_stale_ingest_jobs_for_watchdog()
                    log.info(
                        "Hourly ingest expiry completed: %s stale job(s).", expired
                    )
                    last_expiry_date_hour = current_date_hour
                finally:
                    close_connection()

            # Run at 10:00 and 23:00. Once observed, a due slot remains pending
            # across minute boundaries until the heavy-task gate admits it.
            if pending_due and pending_due != last_run_date_hour:
                due_to_run = pending_due
                if due_to_run:
                    write_status(
                        "processing",
                        0,
                        index_queue.qsize(),
                        "Running Scheduled Auto-Lint",
                        "",
                        component="scheduler",
                    )
                    log.info("Triggering Scheduled Autonomous Auto-Lint...")

                    from vector_lake.tool_lint import lint_vector_lake
                    from vector_lake import indexer
                    from vector_lake.db_store import close_connection
                    from vector_lake.heavy_task_gate import (
                        HeavyTaskBusy,
                        heavy_task,
                    )

                    scheduled_completed = False
                    try:
                        with heavy_task(
                            "scan",
                            "watchdog-scheduled-lint",
                            origin="watchdog",
                            wait_timeout_seconds=0,
                            warn_after_seconds=1800,
                        ):
                            with global_task_lock:
                                if indexer.refresh_graph_topology_if_dirty():
                                    log.info(
                                        "Graph topology refreshed during scheduled lint."
                                    )
                                lint_vector_lake(auto_fix=False)
                        scheduled_completed = True
                    except HeavyTaskBusy:
                        log.info(
                            "Scheduled lint deferred because the heavy-task gate "
                            "is occupied."
                        )
                        write_status(
                            "idle",
                            0,
                            index_queue.qsize(),
                            "Scheduled Lint deferred",
                            "Another memory-intensive operation is active",
                            component="scheduler",
                        )
                    finally:
                        close_connection()

                    if scheduled_completed:
                        log.info("Scheduled Autonomous Auto-Lint completed.")
                        last_run_date_hour = due_to_run
                        pending_due = next_due
                        next_due = ""
                        write_status(
                            "idle",
                            0,
                            index_queue.qsize(),
                            "Scheduled Lint finished",
                            "",
                            component="scheduler",
                        )

            # Pending maintenance retries every 30 seconds even after the
            # original minute-zero window has passed.
            if pending_due:
                wait_seconds = 30
            elif observed_due:
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
            write_status(
                "error",
                0,
                index_queue.qsize(),
                "Scheduled lint exception",
                str(exc),
                component="scheduler",
            )
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
    worker_stop_event = threading.Event()
    auto_stop_event = threading.Event()
    stop_request_path = watchdog_stop_request_path()
    stop_request_path.unlink(missing_ok=True)
    begin_watchdog_run(
        ("watchdog", "outbox", "scheduler", "ingest", "auto_ingest")
    )
    if index_queue.restore_full_reconcile_marker():
        log.warning("Restored pending Wiki reconciliation marker after restart.")

    from vector_lake.auto_ingest_worker import start_auto_ingest_worker
    from vector_lake.ingest_worker import start_worker

    worker_threads = {
        "outbox": threading.Thread(
            target=index_worker_loop,
            args=(worker_stop_event,),
            daemon=True,
            name="vector-lake-outbox-worker",
        ),
        "scheduler": threading.Thread(
            target=scheduled_lint_loop,
            args=(worker_stop_event,),
            daemon=True,
            name="vector-lake-scheduled-lint-worker",
        ),
        "ingest": threading.Thread(
            target=start_worker,
            args=(worker_stop_event,),
            daemon=True,
            name="vector-lake-ingest-worker",
        ),
        "auto_ingest": threading.Thread(
            target=start_auto_ingest_worker,
            args=(auto_stop_event,),
            daemon=False,
            name="vector-lake-auto-ingest-worker",
        ),
    }
    _register_background_threads(worker_threads)
    started_workers: dict[str, threading.Thread] = {}

    observer = Observer()
    observer_started = False
    raw_handler = None
    diary_handler = None
    failure_reason = ""
    initial_raw_targets, raw_config_token = _raw_watch_configuration()
    watch_dirs = _watch_directories(initial_raw_targets)
    raw_watch_handles: dict[str, object | None] = {}
    raw_watch_refresh_seconds = _bounded_env_float(
        "VECTOR_LAKE_RAW_WATCH_REFRESH_SECONDS",
        5.0,
        minimum=0.05,
        maximum=300.0,
    )
    raw_watch_retry_max_seconds = _bounded_env_float(
        "VECTOR_LAKE_RAW_WATCH_RETRY_MAX_SECONDS",
        300.0,
        minimum=raw_watch_refresh_seconds,
        maximum=3600.0,
    )
    raw_watch_retry_state: dict[str, tuple[int, float]] = {}
    raw_watch_cleanup_retry_state: dict[str, tuple[int, float]] = {}

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

        raw_dirs = [
            str(path)
            for path in watch_dirs.get("raw_targets", [watch_dirs["raw"]])
            if os.path.exists(path)
        ]
        if raw_dirs:
            raw_handler = RawWatchdogHandler(stop_event=stop_event)
            for raw_dir in raw_dirs:
                target_key = os.path.normcase(str(Path(raw_dir).resolve()))
                try:
                    watch_handle = observer.schedule(
                        raw_handler,
                        raw_dir,
                        recursive=True,
                    )
                except Exception as exc:
                    raw_watch_retry_state[target_key] = (
                        1,
                        time.monotonic() + raw_watch_refresh_seconds,
                    )
                    log.warning(
                        "Raw source monitor startup deferred for %s; retry in %.2fs: %s",
                        raw_dir,
                        raw_watch_refresh_seconds,
                        exc,
                    )
                    continue
                raw_watch_retry_state.pop(target_key, None)
                raw_watch_handles[target_key] = watch_handle
                log.info(
                    "Raw source monitor active on directory: %s",
                    raw_dir,
                )

        observer.start()
        observer_started = True
        if raw_handler is not None:
            raw_handler.request_full_scan()
        log.info("Vector Lake Watchdog Agent is running in Background Index/Lint mode.")
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
        last_raw_watch_refresh = time.monotonic()
        while not stop_event.wait(monitor_seconds):
            if stop_request_path.exists():
                log.info("External watchdog stop request received.")
                stop_event.set()
                break
            dead_workers = sorted(
                name
                for name, thread in started_workers.items()
                if not thread.is_alive()
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

            observed_emitters = getattr(observer, "emitters", None)
            if observed_emitters is not None:
                raw_handles = [
                    handle
                    for handle in raw_watch_handles.values()
                    if handle is not None
                ]
                dead_non_raw_emitters = []
                for emitter in list(observed_emitters):
                    emitter_is_alive = getattr(emitter, "is_alive", None)
                    if not callable(emitter_is_alive):
                        continue
                    try:
                        is_alive = emitter_is_alive()
                    except Exception:
                        is_alive = False
                    if is_alive:
                        continue
                    emitter_watch = getattr(emitter, "watch", None)
                    if any(emitter_watch == raw_handle for raw_handle in raw_handles):
                        continue
                    dead_non_raw_emitters.append(emitter)
                if dead_non_raw_emitters:
                    failure_reason = "filesystem observer emitter stopped unexpectedly"
                    write_status(
                        "halted",
                        0,
                        index_queue.qsize(),
                        "Filesystem observer emitter died",
                        failure_reason,
                        component="watchdog",
                    )
                    log.error(failure_reason)
                    stop_event.set()
                    break
            now = time.monotonic()
            if now - last_raw_watch_refresh >= raw_watch_refresh_seconds:
                last_raw_watch_refresh = now
                try:
                    current_targets, current_config_token = _raw_watch_configuration()
                    desired_targets = {}
                    for current_target in current_targets:
                        if current_target.is_dir():
                            resolved_target = current_target.resolve()
                            target_key = os.path.normcase(str(resolved_target))
                            desired_targets[target_key] = resolved_target
                    for target_key in tuple(raw_watch_retry_state):
                        if target_key not in desired_targets:
                            raw_watch_retry_state.pop(target_key, None)

                    dead_target_keys = set()
                    observed_emitters = getattr(observer, "emitters", None)
                    if observed_emitters is not None:
                        emitters_by_watch = {
                            emitter.watch: emitter
                            for emitter in list(observed_emitters)
                        }
                        for target_key, watch_handle in list(raw_watch_handles.items()):
                            if watch_handle is None:
                                continue
                            emitter = emitters_by_watch.get(watch_handle)
                            emitter_alive = (
                                emitter is not None
                                and callable(getattr(emitter, "is_alive", None))
                                and emitter.is_alive()
                            )
                            if emitter_alive:
                                continue
                            dead_target_keys.add(target_key)

                    stale_target_keys = {
                        target_key
                        for target_key in raw_watch_handles
                        if target_key not in desired_targets
                    }
                    cleanup_target_keys = dead_target_keys | stale_target_keys
                    for target_key in tuple(raw_watch_cleanup_retry_state):
                        if target_key not in cleanup_target_keys:
                            raw_watch_cleanup_retry_state.pop(target_key, None)

                    removed_targets = []
                    cleaned_dead_targets = []
                    failed_cleanup_targets = []
                    for target_key in sorted(cleanup_target_keys):
                        cleanup_attempt, cleanup_at = raw_watch_cleanup_retry_state.get(
                            target_key, (0, 0.0)
                        )
                        if now < cleanup_at:
                            continue
                        watch_handle = raw_watch_handles.get(target_key)
                        unschedule = getattr(observer, "unschedule", None)
                        try:
                            if watch_handle is None or not callable(unschedule):
                                raise RuntimeError(
                                    "filesystem observer did not return a "
                                    "removable raw watch handle"
                                )
                            unschedule(watch_handle)
                        except Exception as exc:
                            cleanup_attempt += 1
                            cleanup_delay = min(
                                raw_watch_retry_max_seconds,
                                raw_watch_refresh_seconds
                                * (2 ** min(max(0, cleanup_attempt - 1), 10)),
                            )
                            raw_watch_cleanup_retry_state[target_key] = (
                                cleanup_attempt,
                                now + cleanup_delay,
                            )
                            failed_cleanup_targets.append(target_key)
                            log.warning(
                                "Raw monitor cleanup deferred for %s; retry in %.2fs: %s",
                                target_key,
                                cleanup_delay,
                                exc,
                            )
                            continue
                        raw_watch_cleanup_retry_state.pop(target_key, None)
                        raw_watch_handles.pop(target_key, None)
                        if target_key in stale_target_keys:
                            removed_targets.append(target_key)
                            log.info(
                                "Raw source monitor removed for directory: %s",
                                target_key,
                            )
                        else:
                            cleaned_dead_targets.append(target_key)
                            log.error(
                                "Raw source monitor died; resubscribing: %s",
                                target_key,
                            )

                    added_targets = []
                    failed_add_targets = []
                    for target_key, resolved_target in desired_targets.items():
                        if target_key in raw_watch_handles:
                            raw_watch_retry_state.pop(target_key, None)
                            continue
                        retry_attempt, retry_at = raw_watch_retry_state.get(
                            target_key, (0, 0.0)
                        )
                        if now < retry_at:
                            continue
                        if raw_handler is None:
                            raw_handler = RawWatchdogHandler(stop_event=stop_event)
                        try:
                            watch_handle = observer.schedule(
                                raw_handler,
                                str(resolved_target),
                                recursive=True,
                            )
                        except Exception as exc:
                            retry_attempt += 1
                            retry_delay = min(
                                raw_watch_retry_max_seconds,
                                raw_watch_refresh_seconds
                                * (2 ** min(max(0, retry_attempt - 1), 10)),
                            )
                            raw_watch_retry_state[target_key] = (
                                retry_attempt,
                                now + retry_delay,
                            )
                            failed_add_targets.append(target_key)
                            log.warning(
                                "Raw source monitor addition deferred for %s; retry in %.2fs: %s",
                                target_key,
                                retry_delay,
                                exc,
                            )
                            continue
                        raw_watch_retry_state.pop(target_key, None)
                        raw_watch_handles[target_key] = watch_handle
                        added_targets.append(str(resolved_target))
                        log.info(
                            "Raw source monitor added for directory: %s",
                            resolved_target,
                        )

                    config_changed = current_config_token != raw_config_token
                    if raw_handler is not None and (
                        config_changed
                        or added_targets
                        or cleaned_dead_targets
                        or failed_cleanup_targets
                        or failed_add_targets
                        or removed_targets
                    ):
                        raw_handler.request_full_scan()
                    raw_config_token = current_config_token
                except Exception as exc:
                    log.warning(
                        "Raw watch configuration refresh failed; keeping "
                        "prior subscriptions: %s",
                        exc,
                    )
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
        auto_stop_event.set()
        stop_request_path.unlink(missing_ok=True)
        shutdown_failures: list[str] = []

        auto_thread = started_workers.get("auto_ingest")
        if auto_thread is not None and auto_thread.is_alive():
            drain_action = "Watchdog waiting for automatic ingest finalizer"
            drain_error = (
                "singleton and execution locks remain held until finalization returns"
            )

            def publish_drain_heartbeat() -> None:
                try:
                    published = write_status(
                        "draining",
                        0,
                        index_queue.qsize(),
                        drain_action,
                        drain_error,
                        component="watchdog",
                    )
                except Exception as exc:
                    failure = (
                        "watchdog_drain_heartbeat_exception:"
                        f"{type(exc).__name__}:{exc}"
                    )[:1000]
                    log.error("Could not publish watchdog drain heartbeat: %s", exc)
                else:
                    failure = (
                        "" if published else "watchdog_drain_heartbeat_publish_failed"
                    )
                if failure and not any(
                    item.startswith("watchdog_drain_heartbeat_")
                    for item in shutdown_failures
                ):
                    shutdown_failures.append(failure)

            alive_workers = _drain_auto_ingest_worker(
                started_workers,
                ["auto_ingest"],
                heartbeat=publish_drain_heartbeat,
            )
            if alive_workers:
                shutdown_failures.append(
                    "auto_ingest_drain_incomplete:" + ",".join(alive_workers)
                )

        worker_stop_event.set()
        deadline = time.monotonic() + _shutdown_timeout_seconds()

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
        peer_workers = {
            name: thread
            for name, thread in started_workers.items()
            if name != "auto_ingest"
        }
        alive_workers = _join_threads_bounded(peer_workers, remaining)
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
