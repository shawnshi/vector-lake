import asyncio
from concurrent.futures import Future
import functools
import hashlib
import json
import inspect
import logging
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP


_DEFAULT_BLOCKING_WORKERS = 1

_MCP_HEAVY_TASKS = {
    "backup_retention": ("maintenance", 900.0),
    "batch_replace_links": ("maintenance", 900.0),
    "bulk_reconciliation": ("maintenance", 1800.0),
    "canonical_backfill": ("maintenance", 900.0),
    "canonical_reconcile_content": ("maintenance", 1800.0),
    "compact_change_set_history": ("maintenance", 1800.0),
    "delete_source": ("maintenance", 900.0),
    "doctor_vector_lake": ("scan", 900.0),
    "embedding_backfill": ("embedding", 3600.0),
    "evidence_foundation_backfill": ("maintenance", 1800.0),
    "finalize_ingest": ("projection", 900.0),
    "gc_vector_lake": ("maintenance", 1800.0),
    "get_governance_debt": ("scan", 900.0),
    "history_retention": ("maintenance", 1800.0),
    "lint_vector_lake": ("scan", 1800.0),
    "merge_suggestions_vector_lake": ("scan", 1800.0),
    "operational_memory_cleanup": ("maintenance", 900.0),
    "operational_memory_search_index": ("maintenance", 1800.0),
    "orphan_source_classify": ("scan", 900.0),
    "prepare_ingest_batch": ("ingest_scan", 1800.0),
    "projection_rebuild_index": ("projection", 1800.0),
    "projection_report": ("scan", 900.0),
    "reconcile_ingest_tasks": ("maintenance", 1800.0),
    "reconcile_orphan_ingest_packets": ("maintenance", 900.0),
    "rebuild_timeline_events": ("projection", 900.0),
    "rename_entity": ("maintenance", 900.0),
    "sync_vector_lake": ("ingest_scan", 1800.0),
    "topology_queue_cleanup": ("maintenance", 900.0),
    "trigger_audit_graph": ("scan", 1800.0),
    "trigger_autonomous_research": ("ingest_scan", 1800.0),
    "visualize_vector_lake": ("scan", 900.0),
    "wiki_restore": ("maintenance", 900.0),
}



class _DaemonThreadPoolExecutor:
    """Small public-API executor whose running calls cannot hold process exit."""

    _STOP = object()

    def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers < 1
        ):
            raise ValueError("max_workers must be a positive integer")
        self._max_workers = max_workers
        self._thread_name_prefix = str(thread_name_prefix or "daemon-executor")
        self._work_queue: queue.Queue = queue.Queue()
        self._threads = weakref.WeakSet()
        self._state_lock = threading.Lock()
        self._shutdown = False
        self._queued_items = 0
        self._running_workers = 0
        self._shutdown_complete = threading.Event()

    def _start_workers_locked(self) -> None:
        if self._threads:
            return
        for index in range(self._max_workers):
            worker = threading.Thread(
                name=f"{self._thread_name_prefix}_{index}",
                target=self._worker,
                daemon=True,
            )
            self._threads.add(worker)
            self._running_workers += 1
            worker.start()

    @staticmethod
    def _notify_terminal(terminal_callback) -> None:
        if terminal_callback is None:
            return
        try:
            terminal_callback()
        except Exception:
            logging.getLogger(__name__).exception(
                "Blocking executor terminal callback failed"
            )

    @staticmethod
    def _run_item(future: Future, call) -> None:
        """Run one item in a short-lived frame so results cannot linger idle."""
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = call()
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def _worker(self) -> None:
        try:
            while True:
                item = self._work_queue.get()
                if item is self._STOP:
                    return
                with self._state_lock:
                    self._queued_items -= 1
                future, call, terminal_callback = item
                try:
                    self._run_item(future, call)
                finally:
                    self._notify_terminal(terminal_callback)
                    del item, future, call, terminal_callback
        finally:
            with self._state_lock:
                self._running_workers -= 1
                if self._shutdown and self._running_workers == 0:
                    self._shutdown_complete.set()

    def _submit(self, fn, args, kwargs, terminal_callback) -> Future:
        if not callable(fn):
            raise TypeError("submitted work must be callable")
        future = Future()
        call = functools.partial(fn, *args, **kwargs)
        with self._state_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._start_workers_locked()
            self._queued_items += 1
            self._work_queue.put((future, call, terminal_callback))
        return future

    def submit_tracked(self, fn, terminal_callback) -> Future:
        """Submit a zero-argument call and notify when it leaves the physical queue."""
        if not callable(terminal_callback):
            raise TypeError("terminal_callback must be callable")
        return self._submit(fn, (), {}, terminal_callback)

    def queued_work_items(self) -> int:
        with self._state_lock:
            return self._queued_items

    def status_snapshot(self) -> dict:
        with self._state_lock:
            threads = list(self._threads)
            return {
                "queued_items": self._queued_items,
                "workers_daemon": all(worker.daemon for worker in threads),
                "running_workers": self._running_workers,
                "shutdown": self._shutdown,
                "shutdown_completed": self._shutdown_complete.is_set(),
            }

    def _take_queued_work_locked(self) -> list:
        cancelled = []
        stop_count = 0
        while True:
            try:
                item = self._work_queue.get_nowait()
            except queue.Empty:
                break
            if item is self._STOP:
                stop_count += 1
                continue
            self._queued_items -= 1
            cancelled.append(item)
        for _stop in range(stop_count):
            self._work_queue.put(self._STOP)
        return cancelled

    def _cancel_queued_work(self, queued_work: list) -> None:
        for future, _call, terminal_callback in queued_work:
            future.cancel()
            self._notify_terminal(terminal_callback)

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
        timeout: float | None = None,
    ) -> bool:
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("timeout must be a finite non-negative number") from exc
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be a finite non-negative number")
        with self._state_lock:
            first_shutdown = not self._shutdown
            self._shutdown = True
            threads = list(self._threads)
            queued_work = self._take_queued_work_locked() if cancel_futures else []
            if first_shutdown:
                for _worker in threads:
                    self._work_queue.put(self._STOP)
            if not threads:
                self._shutdown_complete.set()
        self._cancel_queued_work(queued_work)
        if wait:
            deadline = None if timeout is None else time.monotonic() + timeout
            for worker in threads:
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                worker.join(remaining)
        return self._shutdown_complete.is_set()


def _finalize_blocking_executor(executor: _DaemonThreadPoolExecutor) -> None:
    """Stop an abandoned server executor without blocking garbage collection."""
    executor.shutdown(wait=False, cancel_futures=True)


def _source_tree_revision(source_root: Path) -> str:
    """Hash loaded Python sources so long-running MCPs can detect code drift."""
    digest = hashlib.sha256()
    for source_path in sorted(source_root.rglob("*.py")):
        try:
            source_bytes = source_path.read_bytes()
        except FileNotFoundError:
            continue
        digest.update(source_path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(source_bytes)
        digest.update(b"\x00")
    return digest.hexdigest()


class MCPRuntimeGuard:
    """Fail closed when the running MCP no longer matches its source tree."""

    def __init__(
        self,
        source_root: Path,
        check_interval_seconds: float | None = None,
    ):
        self.source_root = Path(source_root).resolve()
        if check_interval_seconds is None:
            try:
                check_interval_seconds = float(
                    os.environ.get("VECTOR_LAKE_MCP_REVISION_CHECK_SECONDS", "5")
                )
            except ValueError:
                check_interval_seconds = 5.0
        self.check_interval_seconds = max(0.0, check_interval_seconds)
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        self.loaded_revision = _source_tree_revision(self.source_root)
        self._current_revision = self.loaded_revision
        self._last_checked = time.monotonic()
        self._lock = threading.Lock()

    def status(self, force: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            if force or now - self._last_checked >= self.check_interval_seconds:
                self._current_revision = _source_tree_revision(self.source_root)
                self._last_checked = now
            stale = self._current_revision != self.loaded_revision
            return {
                "pid": os.getpid(),
                "loaded_at": self.loaded_at,
                "source_root": str(self.source_root),
                "loaded_revision": self.loaded_revision,
                "current_revision": self._current_revision,
                "stale": stale,
                "restart_required": stale,
                "check_interval_seconds": self.check_interval_seconds,
            }

    def assert_current(self) -> None:
        status = self.status(force=True)
        if status["stale"]:
            raise RuntimeError(
                "Vector Lake MCP source changed after process startup; "
                "restart the MCP connector before invoking tools."
            )


class ReloadAwareFastMCP(FastMCP):
    """FastMCP dispatcher that refuses stale-code tool execution."""

    def __init__(
        self,
        *args,
        runtime_guard: MCPRuntimeGuard | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.runtime_guard = runtime_guard or MCPRuntimeGuard(
            Path(__file__).resolve().parent
        )
        try:
            worker_count = int(
                os.environ.get(
                    "VECTOR_LAKE_MCP_BLOCKING_WORKERS",
                    str(_DEFAULT_BLOCKING_WORKERS),
                )
            )
        except (TypeError, ValueError):
            worker_count = _DEFAULT_BLOCKING_WORKERS
        worker_count = max(1, min(8, worker_count))
        try:
            queue_capacity = int(
                os.environ.get(
                    "VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", str(worker_count)
                )
            )
        except (TypeError, ValueError):
            queue_capacity = worker_count
        queue_capacity = max(0, min(64, queue_capacity))
        try:
            admission_timeout = float(
                os.environ.get("VECTOR_LAKE_MCP_ADMISSION_TIMEOUT_SECONDS", "0.05")
            )
        except (TypeError, ValueError):
            admission_timeout = 0.05
        if not math.isfinite(admission_timeout):
            admission_timeout = 0.05
        try:
            shutdown_timeout = float(
                os.environ.get("VECTOR_LAKE_MCP_SHUTDOWN_TIMEOUT_SECONDS", "5")
            )
        except (TypeError, ValueError):
            shutdown_timeout = 5.0
        if not math.isfinite(shutdown_timeout):
            shutdown_timeout = 5.0
        try:
            heavy_task_wait = float(
                os.environ.get("VECTOR_LAKE_MCP_HEAVY_TASK_WAIT_SECONDS", "0.5")
            )
        except (TypeError, ValueError):
            heavy_task_wait = 0.5
        if not math.isfinite(heavy_task_wait):
            heavy_task_wait = 0.5
        self._blocking_worker_count = worker_count
        self._blocking_queue_capacity = queue_capacity
        self._blocking_admission_timeout = max(0.0, min(5.0, admission_timeout))
        self._blocking_shutdown_timeout = max(0.1, min(30.0, shutdown_timeout))
        self._heavy_task_wait = max(0.0, min(5.0, heavy_task_wait))
        self._blocking_slots = threading.BoundedSemaphore(worker_count + queue_capacity)
        self._blocking_inflight = 0
        self._blocking_executor = _DaemonThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="vector-lake-mcp",
        )
        self._executor_shutdown_lock = threading.Lock()
        self._executor_shutdown_started = False
        self._executor_shutdown_timed_out = False
        self._executor_finalizer = weakref.finalize(
            self, _finalize_blocking_executor, self._blocking_executor
        )

    @staticmethod
    def _cleanup_tool_connection(*, failed: bool) -> None:
        try:
            from vector_lake.db_store import cleanup_connection_after_tool_call

            cleanup_connection_after_tool_call(failed=failed)
        except Exception:
            logging.getLogger(__name__).debug(
                "Failed to clean up a tool-local database connection",
                exc_info=True,
            )

    @classmethod
    def _invoke_blocking_tool(cls, fn, *fn_args, **fn_kwargs):
        failed = True
        try:
            result = fn(*fn_args, **fn_kwargs)
            failed = False
            return result
        finally:
            cls._cleanup_tool_connection(failed=failed)

    def _assert_accepting_calls(self) -> None:
        with self._executor_shutdown_lock:
            if self._executor_shutdown_started:
                raise RuntimeError("Vector Lake MCP server is shutting down")

    def _submit_admitted_blocking_call(self, call):
        released = False

        def release_slot():
            nonlocal released
            with self._executor_shutdown_lock:
                if released:
                    return
                released = True
                self._blocking_inflight -= 1
            self._blocking_slots.release()

        with self._executor_shutdown_lock:
            if self._executor_shutdown_started:
                self._blocking_slots.release()
                raise RuntimeError("Vector Lake MCP server is shutting down")
            self._blocking_inflight += 1
            try:
                return self._blocking_executor.submit_tracked(call, release_slot)
            except BaseException:
                self._blocking_inflight -= 1
                self._blocking_slots.release()
                raise

    def _submit_blocking_call(self, call):
        """Synchronously admit a bounded blocking call for non-event-loop callers."""
        admitted = self._blocking_slots.acquire(
            timeout=self._blocking_admission_timeout
        )
        if not admitted:
            raise RuntimeError("Vector Lake MCP blocking executor is saturated; retry later")
        return self._submit_admitted_blocking_call(call)

    async def _acquire_blocking_slot(self) -> None:
        deadline = time.monotonic() + self._blocking_admission_timeout
        while True:
            self._assert_accepting_calls()
            if self._blocking_slots.acquire(blocking=False):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Vector Lake MCP blocking executor is saturated; retry later"
                )
            await asyncio.sleep(min(0.01, max(0.001, deadline - time.monotonic())))

    async def _run_blocking_call(self, call):
        await self._acquire_blocking_slot()
        future = self._submit_admitted_blocking_call(call)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    def blocking_executor_status(self) -> dict:
        executor_status = self._blocking_executor.status_snapshot()
        with self._executor_shutdown_lock:
            shutdown_started = self._executor_shutdown_started
            inflight = self._blocking_inflight
            timed_out = self._executor_shutdown_timed_out
        return {
            "workers": self._blocking_worker_count,
            "queue_capacity": self._blocking_queue_capacity,
            "inflight": inflight,
            "queued_items": executor_status["queued_items"],
            "admission_timeout_seconds": self._blocking_admission_timeout,
            "shutdown_timeout_seconds": self._blocking_shutdown_timeout,
            "workers_daemon": executor_status["workers_daemon"],
            "running_workers": executor_status["running_workers"],
            "shutdown_started": shutdown_started,
            "shutdown_completed": shutdown_started
            and executor_status["shutdown_completed"],
            "shutdown_timed_out": timed_out,
        }

    def shutdown_blocking_executor(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Idempotently stop this server's bounded blocking-tool executor."""
        if timeout is None:
            timeout = self._blocking_shutdown_timeout
        else:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("timeout must be a finite non-negative number") from exc
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be a finite non-negative number")
        with self._executor_shutdown_lock:
            first_shutdown = not self._executor_shutdown_started
            self._executor_shutdown_started = True
            if self._executor_finalizer.alive:
                self._executor_finalizer.detach()
        if first_shutdown or wait:
            completed = self._blocking_executor.shutdown(
                wait=wait,
                cancel_futures=True,
                timeout=timeout if wait else None,
            )
            if wait and not completed:
                with self._executor_shutdown_lock:
                    self._executor_shutdown_timed_out = True
                logging.getLogger(__name__).warning(
                    "Vector Lake MCP blocking executor did not drain within %.3f seconds",
                    timeout,
                )

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def managed_async_tool(*fn_args, **fn_kwargs):
                    self._assert_accepting_calls()
                    self.runtime_guard.assert_current()
                    failed = True
                    try:
                        result = await fn(*fn_args, **fn_kwargs)
                        failed = False
                        return result
                    finally:
                        self._cleanup_tool_connection(failed=failed)

                register(managed_async_tool)
                return fn

            @functools.wraps(fn)
            async def threaded_tool(*fn_args, **fn_kwargs):
                def invoke_current_tool():
                    if fn.__name__ != "mcp_runtime_status":
                        self.runtime_guard.assert_current()
                    policy = _MCP_HEAVY_TASKS.get(fn.__name__)
                    if policy is None:
                        return fn(*fn_args, **fn_kwargs)
                    from vector_lake.heavy_task_gate import heavy_task

                    task_class, warn_after_seconds = policy
                    with heavy_task(
                        task_class,
                        fn.__name__,
                        origin="mcp",
                        wait_timeout_seconds=self._heavy_task_wait,
                        warn_after_seconds=warn_after_seconds,
                    ):
                        return fn(*fn_args, **fn_kwargs)

                call = functools.partial(
                    self._invoke_blocking_tool,
                    invoke_current_tool,
                )
                return await self._run_blocking_call(call)

            register(threaded_tool)
            return fn

        return decorator

    def run(self, transport="stdio", mount_path=None):
        try:
            return super().run(transport=transport, mount_path=mount_path)
        finally:
            self.shutdown_blocking_executor(wait=True)

    async def call_tool(self, name: str, arguments: dict):
        self._assert_accepting_calls()
        if name != "mcp_runtime_status":
            await self._run_blocking_call(self.runtime_guard.assert_current)
        return await super().call_tool(name, arguments)

# Global lock against stdout pollution
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', force=True)
from vector_lake import tool_memory  # noqa: E402
from vector_lake import tools  # noqa: E402
from vector_lake.governance_store import insert_governance_item_if_absent, _utc_now  # noqa: E402
from vector_lake.tool_timeline import search_timeline_events  # noqa: E402

mcp = ReloadAwareFastMCP("vector-lake")


@mcp.tool()
def mcp_runtime_status() -> str:
    """Report whether this MCP process still matches the on-disk source tree."""
    status = mcp.runtime_guard.status(force=True)
    status["blocking_executor"] = mcp.blocking_executor_status()
    from vector_lake.heavy_task_gate import heavy_task_gate_status

    status["heavy_task_gate"] = heavy_task_gate_status()
    return json.dumps(
        status,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

@mcp.tool()
def search_timeline(entity_name: str = "", sentiment: str = "", action: str = "", limit: int = 10) -> str:
    """Search the strategic timeline events database.
    
    Args:
        entity_name: Filter by entity title (e.g., '卫宁健康'). Leave empty to search all.
        sentiment: Filter by sentiment ('positive', 'neutral', 'negative'). Leave empty for all.
        action: Filter by action type (e.g., 'Release', 'Earnings'). Leave empty for all.
        limit: Number of events to return (default 10).
    """
    return search_timeline_events(
        entity_name=entity_name if entity_name else None,
        sentiment=sentiment if sentiment else None,
        action=action if action else None,
        limit=limit
    )

@mcp.tool()
def rebuild_timeline_events(dry_run: bool = True, limit: int = 0) -> str:
    """Rebuild the timeline_events projection from timeline-event claims."""
    return tools.rebuild_timeline_events_from_claims(
        dry_run=dry_run,
        limit=limit if limit and limit > 0 else None,
    )

@mcp.tool()
def projection_report(limit: int = 20) -> str:
    """Report drift between Wiki pages, SQLite canonical entities, and index.json."""
    return tools.projection_diff_report(limit=limit)

@mcp.tool()
def canonical_backfill(dry_run: bool = True, limit: int = 50) -> str:
    """Backfill missing SQLite canonical rows from existing Wiki pages."""
    return tools.canonical_backfill_missing_wiki(dry_run=dry_run, limit=limit)

@mcp.tool()
def canonical_reconcile_content(dry_run: bool = True, limit: int = 0, batch_size: int = 100) -> str:
    """Reconcile schema-valid Wiki content into canonical state after an explicit maintenance review."""
    return tools.reconcile_canonical_content_from_wiki(
        dry_run=dry_run,
        limit=limit,
        batch_size=batch_size,
    )


@mcp.tool()
def evidence_foundation_backfill(dry_run: bool = True, limit: int = 500, batch_size: int = 100) -> str:
    """Backfill missing auditable evidence metadata without replacing canonical content."""
    return tools.evidence_foundation_backfill(
        dry_run=dry_run,
        limit=limit,
        batch_size=batch_size,
    )

@mcp.tool()
def backup_retention(
    dry_run: bool = True,
    keep_latest: int = 5,
    min_age_days: int = 30,
    stage_ttl_hours: int = 24,
    confirmation: str = "",
) -> str:
    """Preview or explicitly apply guarded backup retention."""
    return tools.backup_retention_maintenance(
        dry_run=dry_run,
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        stage_ttl_hours=stage_ttl_hours,
        confirmation=confirmation,
    )

@mcp.tool()
def projection_rebuild_index(dry_run: bool = True) -> str:
    """Rebuild index.json, FTS, embeddings, and claim_graph from SQLite canonical state."""
    return tools.rebuild_index_projection(dry_run=dry_run)

@mcp.tool()
def embedding_backfill(dry_run: bool = True, limit: int = 0, include_existing: bool = False) -> str:
    """Backfill missing vector embeddings under RPM/TPM rate limits."""
    return tools.embedding_backfill_projection(
        dry_run=dry_run,
        limit=limit if limit and limit > 0 else None,
        include_existing=include_existing,
    )

@mcp.tool()
def wiki_restore(dry_run: bool = True, limit: int = 10) -> str:
    """Restore missing Wiki Markdown pages from canonical metadata."""
    return tools.restore_missing_wiki_from_canonical(dry_run=dry_run, limit=limit)


@mcp.tool()
def operational_memory_search_index(
    dry_run: bool = True,
    batch_size: int = 256,
) -> str:
    """Report status or explicitly advance one bounded operational-memory index batch."""
    return tools.operational_memory_search_index_maintenance(
        dry_run=dry_run,
        batch_size=batch_size,
    )


@mcp.tool()
def operational_memory_cleanup(dry_run: bool = True, limit: int = 0) -> str:
    """Preview or archive known generated/template artifacts in operational memory."""
    return tools.cleanup_operational_memory(dry_run=dry_run, limit=limit)


@mcp.tool()
def history_retention(
    dry_run: bool = True,
    ttl_days: int = 30,
    batch_size: int = 500,
    max_delete_bytes: int = 128 * 1024 * 1024,
    keep_change_sets: int = 1000,
    keep_terminal_jobs: int = 1000,
    keep_terminal_outbox: int = 1000,
    keep_versions_per_family: int = 2,
    claim_version_cursor: str = "",
    evidence_version_cursor: str = "",
    version_cursor_receipt: str = "",
    plan_as_of: str = "",
    confirmation: str = "",
) -> str:
    """Preview or explicitly apply one bounded, reference-safe history batch."""
    return tools.history_retention_maintenance(
        dry_run=dry_run,
        ttl_days=ttl_days,
        batch_size=batch_size,
        max_delete_bytes=max_delete_bytes,
        keep_change_sets=keep_change_sets,
        keep_terminal_jobs=keep_terminal_jobs,
        keep_terminal_outbox=keep_terminal_outbox,
        keep_versions_per_family=keep_versions_per_family,
        claim_version_cursor=claim_version_cursor,
        evidence_version_cursor=evidence_version_cursor,
        version_cursor_receipt=version_cursor_receipt,
        plan_as_of=plan_as_of,
        confirmation=confirmation,
    )


@mcp.tool()
def compact_change_set_history(
    dry_run: bool = True,
    max_rows: int = 100,
    max_input_bytes: int = 64 * 1024 * 1024,
    confirmation: str = "",
    cursor: str = "",
) -> str:
    """Preview/apply one batch; resume with a successful result.safe_next_cursor."""
    return tools.compact_change_set_history(
        dry_run=dry_run,
        max_rows=max_rows,
        max_input_bytes=max_input_bytes,
        confirmation=confirmation,
        cursor=cursor,
    )

@mcp.tool()
def topology_queue_cleanup(dry_run: bool = True) -> str:
    """Preview or retire old indexer-generated community naming work."""
    import json

    return json.dumps(
        tools.retire_legacy_topology_queue(dry_run=dry_run),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def orphan_source_classify(dry_run: bool = True) -> str:
    """Classify unreferenced sources and optionally register explicit remediation debt."""
    import json

    return json.dumps(
        tools.classify_orphan_source_debt(dry_run=dry_run),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

@mcp.tool()
def search_vector_lake(query: str, top_k: int = 5, mode: str = "page") -> str:
    """Search the Vector Lake index.
    
    Args:
        query: The semantic query string.
        top_k: Number of results to return.
        mode: Search mode, can be 'page', 'memory', or 'claim'.
    """
    return tools.search_vector_lake(query, top_k, mode=mode)


@mcp.tool()
def export_evidence_packet(
    claim_id: str,
    include_evidence_text: bool = False,
    max_evidence_text_chars: int = 2000,
    actor_id: str = "",
    purpose: str = "",
) -> str:
    """Export a read-only CBSS EvidencePacket for one canonical claim.

    The packet remains a claim candidate. It never promotes a Vector Lake claim
    to an AcceptedFact and defaults to hashes and locators instead of raw text.
    """
    return tools.export_evidence_packet(
        claim_id,
        include_evidence_text=include_evidence_text,
        max_evidence_text_chars=max_evidence_text_chars,
        actor_id=actor_id,
        purpose=purpose,
    )


@mcp.tool()
def semantic_readiness(decision_id: str = "") -> str:
    """Report semantic readiness separately from infrastructure health."""
    return tools.semantic_readiness_vector_lake(decision_id or None)


@mcp.tool()
def sync_critical_decision_registry(
    payload_file: str,
    expected_sha256: str,
    actor_id: str,
) -> str:
    """Import an operator-pinned CriticalDecisionRegistry snapshot from the approved sandbox."""
    import json
    from vector_lake.decision_registry import sync_verified_registry_document

    payload_text = _read_payload(payload_file)
    return json.dumps(
        sync_verified_registry_document(
            payload_text,
            expected_sha256=expected_sha256,
            actor_id=actor_id,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

def _read_payload(payload_file: str) -> str:
    if not payload_file:
        return ""
    import os
    from pathlib import Path
    from vector_lake.native_llm import peek_subagent_brain_root

    abs_path = Path(payload_file).resolve()
    configured_root = os.environ.get("VECTOR_LAKE_PAYLOAD_ROOT")
    if configured_root:
        allowed = abs_path.is_relative_to(Path(configured_root).expanduser().resolve())
    else:
        allowed = False
        brain_roots = [
            peek_subagent_brain_root().resolve(),
            Path(os.path.expanduser("~/.codex/brain")).resolve(),
        ]
        for root in brain_roots:
            if not abs_path.is_relative_to(root):
                continue
            relative_parts = abs_path.relative_to(root).parts
            if len(relative_parts) >= 3 and relative_parts[1].lower() == "scratch":
                allowed = True
                break
    if not allowed:
        raise ValueError(f"[Security Error] Payload file must be within an approved agent sandbox: {payload_file}")
    if not abs_path.exists() or not abs_path.is_file():
        raise ValueError(f"[Sandbox Error] Payload file not found: {payload_file}. Please use write_to_file to create it first.")
    max_bytes = max(1, int(os.environ.get("VECTOR_LAKE_PAYLOAD_MAX_BYTES", str(5 * 1024 * 1024))))
    if abs_path.stat().st_size > max_bytes:
        raise ValueError(f"[Sandbox Error] Payload file exceeds {max_bytes} bytes: {payload_file}")
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

@mcp.tool()
def update_operational_memory(memory_type: str, payload_file: str) -> str:
    """Safely persist an operational memory (preference, decision, fact, task_state) without corrupting the graph.
    
    Args:
        memory_type: Type of memory ('preference', 'decision', 'fact', 'task_state').
        payload_file: Absolute path to a temporary file containing the text content of the memory.
    """
    try:
        content = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    return tool_memory.update_operational_memory(memory_type, content)

@mcp.tool()
def sync_vector_lake() -> str:
    """(Legacy Alias) Trigger an ingestion batch scan. Replaced by the asynchronous Subagent pipeline, now wraps prepare_ingest_batch."""
    try:
        return tools.sync_vector_lake()
    except Exception as e:
        import traceback
        logging.error(f"MCP Tool Exception (sync_vector_lake): {e}\n{traceback.format_exc()}")
        return f"MCP Exception: {str(e)}\n{traceback.format_exc()}"

@mcp.tool()
def lint_vector_lake(auto_fix: bool = False) -> str:
    """Run self-healing audit on the Wiki nodes.
    
    Args:
        auto_fix: Automatically fix issues such as decaying notes.
    """
    try:
        return tools.lint_vector_lake(auto_fix=auto_fix)
    except Exception as e:
        import traceback
        logging.error(f"MCP Tool Exception (lint_vector_lake): {e}\n{traceback.format_exc()}")
        return f"MCP Exception: {str(e)}\n{traceback.format_exc()}"

@mcp.tool()
def query_logic_lake(query_str: str) -> str:
    """Deep reasoning with budget-controlled context.
    
    Args:
        query_str: The topic or command for reasoning.
    """
    return tools.prepare_query_context(query_str)

@mcp.tool()
def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    """Finalize the logic lake query by indexing the new pages and syncing to the governance store.
    
    Args:
        files_written_str: Comma-separated list of filenames (e.g. 'Synthesis_Topic.md') that were written by the subagent.
        query_str: The original query string for the trace.
    """
    return tools.finalize_query_synthesis(files_written_str, query_str)

@mcp.tool()
def review_governance_list() -> str:
    """List pending items in the governance review queue (contradictions, gaps, merges)."""
    return tools.review_vector_lake(action="list")

@mcp.tool()
def resolve_governance_item(item_id: str, resolution: str, payload_file: str = None) -> str:
    """Resolve a governance item.

    Args:
        item_id: The ID or index of the item.
        resolution: Resolution action: 'skip', 'create', 'merge', 'acknowledge'.
        payload_file: Optional absolute path to a temporary JSON file containing the expected outcome manifest (e.g. {"allow_cycles": false}).
    """
    import json
    manifest = None
    if payload_file:
        try:
            manifest_str = _read_payload(payload_file)
            if manifest_str.strip():
                manifest = json.loads(manifest_str)
        except json.JSONDecodeError as e:
            return f"[Sandbox JSON Error] Failed to parse payload file {payload_file}: {e}. Please fix the JSON and retry."
        except Exception as e:
            return str(e)
    return tools.review_vector_lake(action="resolve", index=item_id, resolution=resolution, change_manifest=manifest)
@mcp.tool()
def trigger_autonomous_research(dry_run: bool = False) -> str:
    """Autonomously scan graph gaps and governance queue to formulate web research directives.
    
    Args:
        dry_run: If true, just lists the topics without emitting a SYSTEM DIRECTIVE.
    """
    return tools.research_vector_lake(dry_run=dry_run)

@mcp.tool()
def review_strategic_purpose(as_of: str = "") -> str:
    """Review due Standing Intelligence Requirements without changing the Wiki.

    Args:
        as_of: Optional YYYY-MM-DD date. Defaults to the current day.
    """
    return tools.review_strategic_purpose(as_of=as_of)

@mcp.tool()
def get_governance_debt(top: int = 20) -> str:
    """Show governance debt metrics.
    
    Args:
        top: Number of top items to show.
    """
    return tools.debt_vector_lake(top=top)

@mcp.tool()
def trigger_audit_graph() -> str:
    """Synthesize graph topology insights into the unified review surface."""
    return tools.audit_graph()

@mcp.tool()
def delete_source(raw_path: str, dry_run: bool = True) -> str:
    """Cascade-delete a raw source and all related wiki pages.
    
    Args:
        raw_path: Path to the raw source file to remove.
        dry_run: Preview what would be deleted without making changes.
    """
    return tools.delete_source(raw_path, dry_run=dry_run)

@mcp.tool()
def doctor_vector_lake() -> str:
    """Validate runtime dependencies and filesystem layout health."""
    return tools.doctor_vector_lake()

@mcp.tool()
def rename_entity(old_name: str, new_name: str, dry_run: bool = True) -> str:
    """Rename a Wiki entity (filename/frontmatter) and automatically update all referring markdown links.
    
    Args:
        old_name: Current name of the entity (e.g. 'Concept_Old-Name.md').
        new_name: New name for the entity (e.g. 'Concept_New-Name.md').
        dry_run: Preview changes without writing to disk.
    """
    from vector_lake.tool_rename import rename_vector_lake_entity
    return rename_vector_lake_entity(old_name, new_name, dry_run=dry_run)

@mcp.tool()
def trace_vector_lake(query_or_id: str) -> str:
    """Show provenance trace for a query or identifier.
    
    Args:
        query_or_id: Query text or object identifier.
    """
    return tools.trace_vector_lake(query_or_id)

@mcp.tool()
def merge_suggestions_vector_lake(limit: int = 20, enqueue: bool = False) -> str:
    """Detect and surface candidate entity merges.
    
    Args:
        limit: Maximum number of merge candidates to surface.
        enqueue: If True, enqueue the candidates into the governance review queue.
    """
    return tools.merge_suggestions_vector_lake(limit=limit, enqueue=enqueue)

@mcp.tool()
def gc_vector_lake(
    days: int = 30,
    dry_run: bool = True,
    orphan_confirmation: str | None = None,
) -> str:
    """Automatically prune isolated or orphaned entities.
    
    Args:
        days: Prune entities older than this many days (default: 30).
        dry_run: Preview what would be deleted without making changes.
        orphan_confirmation: Exact fingerprint returned by a current dry-run;
            orphan pages remain untouched when omitted.
    """
    from vector_lake.tool_gc import validate_gc_days

    days = validate_gc_days(days)
    return tools.gc_vector_lake(
        days=days,
        dry_run=dry_run,
        orphan_confirmation=orphan_confirmation,
    )

@mcp.tool()
def prepare_ingest_batch(batch_size: int = 5) -> str:
    """Scan for unprocessed raw sources and prepare subagent ingestion instructions.
    
    Args:
        batch_size: Number of files to process in this batch (default: 5).
    """
    return tools.prepare_ingest_batch(batch_size=batch_size)

@mcp.tool()
def list_ingest_tasks(limit: int = 20, include_queued: bool = True) -> str:
    """List queued or awaiting-subagent ingest jobs."""
    return tools.list_ingest_tasks(limit=limit, include_queued=include_queued)

@mcp.tool()
def claim_ingest_tasks(limit: int = 5, lease_seconds: int = 3600) -> str:
    """Lease awaiting ingest task packets to the current-environment subagent host."""
    return tools.claim_ingest_tasks(limit=limit, lease_seconds=lease_seconds)

@mcp.tool()
def expire_ingest_tasks(max_age_seconds: int = 86400) -> str:
    """Expire stale awaiting-subagent ingest jobs so they can be retried deliberately."""
    return tools.expire_ingest_tasks(max_age_seconds=max_age_seconds)


@mcp.tool()
def reconcile_ingest_tasks(dry_run: bool = True, limit: int = 0) -> str:
    """Classify and safely recover abandoned or terminal ingest tasks."""
    return tools.reconcile_ingest_job_debt(dry_run=dry_run, limit=limit)


@mcp.tool()
def reconcile_orphan_ingest_packets(
    dry_run: bool = True,
    min_age_seconds: int = 86400,
    limit: int = 0,
) -> str:
    """Preview or remove old ingest task packets with no durable reference."""
    return tools.reconcile_orphan_ingest_task_packets(
        dry_run=dry_run, min_age_seconds=min_age_seconds, limit=limit
    )


@mcp.tool()
def finalize_ingest(
    files_written: list = None,
    processed_data: dict = None,
    files_written_payload_file: str = "",
    raw_files_payload_file: str = "",
) -> str:
    """Finalize ingestion after a subagent has produced validated wiki pages.
    
    Args:
        files_written: Direct list of dicts with 'filename' and 'content'.
        processed_data: Claimed job dict with filepath/hash/source_hash/source_projection_hash/integration_candidates/ingest_contract_version/lease fields plus an integration disposition manifest.
        files_written_payload_file: Sandbox JSON file containing files_written.
        raw_files_payload_file: Sandbox JSON file containing processed_data.
    """
    import json
    try:
        import json
        if files_written_payload_file or raw_files_payload_file:
            if not files_written_payload_file or not raw_files_payload_file:
                return "Error: Both payload files are required when using the file-based ingest contract."
            files_written = json.loads(_read_payload(files_written_payload_file))
            processed_data = json.loads(_read_payload(raw_files_payload_file))
        if not isinstance(files_written, list) or not isinstance(processed_data, dict):
            return "Error: finalize_ingest requires a files list and processed-data object."
        return tools.finalize_ingest(files_written, processed_data)
    except Exception as e:
        return str(e)

@mcp.tool()
def check_duplicate_entity(candidate_title: str, candidate_type: str, candidate_summary: str = "") -> str:
    """Check if an entity or concept already exists in the graph to prevent duplicates.
    
    Args:
        candidate_title: The title of the entity to create.
        candidate_type: The specific type of the entity (e.g. 'vendor', 'product', 'person', 'event', 'concept').
        candidate_summary: A brief summary of the entity to use for similarity matching.
    """
    return tools.check_duplicate_entity(candidate_title, candidate_type, candidate_summary)

@mcp.tool()
def visualize_vector_lake(output_dir: str = None) -> str:
    """Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard."""
    if output_dir:
        from pathlib import Path
        import os
        abs_dir = Path(output_dir).resolve()
        allowed_roots = [Path(os.path.expanduser("~/.gemini")).resolve(), Path(os.path.expanduser("~/.codex")).resolve()]
        if not any(abs_dir.is_relative_to(root) for root in allowed_roots):
            return "Error: Write operations must be contained within an approved agent sandbox."
    return tools.visualize_vector_lake(output_dir)

@mcp.tool()
def write_wiki_page(filename: str, payload_file: str) -> str:
    """Write or update a Vector Lake wiki page safely.
    
    Args:
        filename: The filename (e.g. 'Concept_Example.md').
        payload_file: Absolute path to a temporary file containing the full markdown content including YAML frontmatter.
    """
    try:
        content = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    from vector_lake.wiki_utils import SafeWriteError
    try:
        from vector_lake.mutation_coordinator import execute_mutation_plan
        execute_mutation_plan(filename, content=content, is_delete=False)
        return f"Successfully wrote {filename} and queued index update."
    except SafeWriteError as e:
        return f"[Write Rejected] {str(e)}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

@mcp.tool()
def propose_schema_mutation(new_category: str, payload_file: str, parent_category: str = "Uncategorized") -> str:
    """Propose a new taxonomy category to the ontology team.
    
    Args:
        new_category: The name of the new category.
        payload_file: Absolute path to a temporary file containing a brief definition or justification for the category.
        parent_category: The parent category (default: 'Uncategorized').
    """
    try:
        description = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    item_id = f"gov_{uuid.uuid4().hex[:12]}"
    insert_governance_item_if_absent({
            "item_id": item_id,
            "type": "schema-mutation",
            "title": f"New Schema Category: {new_category}",
            "description": f"Definition: {description}\nParent: {parent_category}",
            "created_at": _utc_now(),
            "status": "pending",
            "source": "mcp-agent",
            "affected_ids": [],
            "search_queries": [],
            "affected_pages": ["SCHEMA_CATEGORIES.md"],
        })
    return f"Schema mutation proposed and logged as {item_id} for review."



@mcp.tool()
def batch_replace_links(old_text: str, new_text: str, dry_run: bool = True) -> str:
    """Batch replace occurrences of a string (usually a link) across all wiki pages.
    Use this when an entity's name changes but `rename_entity` failed to cover all cases.
    
    Args:
        old_text: The exact string to search for (e.g. '[[Old Name]]').
        new_text: The exact replacement string (e.g. '[[New Name]]').
        dry_run: If True, only count how many files would be modified without actually changing them.
    """
    
    if old_text.strip() in ["", "---", "[[", "]]", "```", "#"]:
        return f"Error: '{old_text}' is a structural syntax marker. Global replacement aborted to protect graph topology."
        
    from vector_lake.wiki_utils import (
        get_wiki_dir,
        iter_markdown_files,
        normalize_semantic_text,
    )
    from vector_lake.mutation_coordinator import execute_mutation_batch
    wiki_dir = get_wiki_dir()
    modified_count = 0
    matched_files = []
    mutations = []
    
    for filepath in iter_markdown_files(wiki_dir):
        filename = filepath.name
        try:
            content_bytes = filepath.read_bytes()
            content = normalize_semantic_text(content_bytes.decode("utf-8"))
            if old_text in content:
                mutations.append(
                    {
                        "filename": filename,
                        "content": content.replace(old_text, new_text),
                        "expected_projection_hash": hashlib.sha256(
                            content_bytes
                        ).hexdigest(),
                    }
                )
                modified_count += 1
                matched_files.append(filename)
        except Exception as e:
            logging.error(f"Error processing {filename} for link replacement: {e}")
            
    if dry_run:
        return f"[DRY RUN] Would replace '{old_text}' with '{new_text}' in {modified_count} files: {', '.join(matched_files[:10])}..."

    if mutations:
        execute_mutation_batch(mutations)
    return f"Successfully replaced '{old_text}' with '{new_text}' in {modified_count} files and queued projections."

@mcp.tool()
def bulk_reconciliation(payload_file: str, dry_run: bool = True) -> str:
    """Execute a batch of graph reconciliation operations (merge, replace_only, alias).
    
    Args:
        payload_file: Absolute path to a temporary JSON file containing the operations array.
        dry_run: Whether to perform a dry run (default: True).
    """
    import json
    try:
        content = _read_payload(payload_file)
        operations = json.loads(content)
    except Exception as e:
        return str(e)
    from vector_lake.tool_bulk_reconciliation import bulk_reconcile
    return bulk_reconcile(operations, dry_run)

if __name__ == "__main__":
    mcp.run()
