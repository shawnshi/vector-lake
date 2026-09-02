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
import stat
import sys
import threading
import time
import unicodedata
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from vector_lake.cancellation import (
    CANCELLATION_CONTRACT_VERSION,
    CancellationOperation,
    CancellationRegistry,
    CooperativeCancellation,
    ToolDeadlineExceeded,
    bind_cancellation_operation,
    cancellation_checkpoint,
    current_request_deadline,
    make_request_deadline,
    request_deadline_scope,
)


_DEFAULT_BLOCKING_WORKERS = 2
_DEFAULT_BLOCKING_QUEUE_CAPACITY = 4
_DEFAULT_HEAVY_WORKERS = 1
_DEFAULT_RUNTIME_REVISION_FULL_HASH_SECONDS = 60.0
_RUNTIME_REVISION_STRICT_ENV = "VECTOR_LAKE_MCP_REVISION_STRICT"
_RUNTIME_REVISION_MAX_FILES = 8192
_RUNTIME_REVISION_MAX_DIRECTORIES = 2048
_RUNTIME_REVISION_MAX_SCANNED_ENTRIES = 32768
_MCP_CALL_DEADLINE_ARGUMENT = "_vector_lake_deadline_seconds"
_MCP_CALL_DEADLINE_MAX_SECONDS = 3600.0

_MCP_HEAVY_TASKS = {
    "auto_ingest_budget_status": ("scan", 900.0),
    "auto_ingest_receipt_retention": ("maintenance", 900.0),
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
    "finalize_query_synthesis": ("projection", 900.0),
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
    "propose_schema_mutation": ("maintenance", 900.0),
    "reconcile_ingest_tasks": ("maintenance", 1800.0),
    "recover_terminal_ingest_outputs": ("projection", 1800.0),
    "reconcile_orphan_ingest_packets": ("maintenance", 900.0),
    "rebuild_timeline_events": ("projection", 900.0),
    "rename_entity": ("maintenance", 900.0),
    "semantic_readiness_campaign": ("scan", 900.0),
    "sync_vector_lake": ("ingest_scan", 1800.0),
    "sync_critical_decision_registry": ("maintenance", 900.0),
    "topology_queue_cleanup": ("maintenance", 900.0),
    "unsupported_claim_debt": ("maintenance", 900.0),
    "trigger_audit_graph": ("scan", 1800.0),
    "trigger_autonomous_research": ("ingest_scan", 1800.0),
    "visualize_vector_lake": ("scan", 900.0),
    "wiki_restore": ("maintenance", 900.0),
    "write_wiki_batch": ("maintenance", 900.0),
}

# Trusted-host-only gate for explicitly authorized recovery of ingest leases.
_MANUAL_INGEST_ADMIN_ENV = "VECTOR_LAKE_ALLOW_MANUAL_INGEST_ADMIN"
_MANUAL_QUERY_SYNTHESIS_ENV = "VECTOR_LAKE_ALLOW_MANUAL_QUERY_SYNTHESIS"
_SYSTEM_PAGE_WRITE_ENV = "VECTOR_LAKE_ALLOW_SYSTEM_PAGE_WRITE"
_WIKI_BATCH_SCHEMA_MAINTENANCE_ENV = (
    "VECTOR_LAKE_WIKI_BATCH_SCHEMA_MAINTENANCE_ALLOWLIST"
)
_EVIDENCE_TEXT_EXPORT_ENV = "VECTOR_LAKE_ALLOW_EVIDENCE_TEXT_EXPORT"
_MCP_SURFACE_ENV = "VECTOR_LAKE_MCP_SURFACE"
_MEMORY_MCP_SURFACE_TOOLS = frozenset(
    {
        "auto_ingest_budget_status",
        "mcp_runtime_status",
        "memory_capabilities",
        "recall",
        "remember",
        "entity",
        "synthesize",
        "context_pack",
        "delta",
    }
)
_READONLY_MCP_SURFACE_TOOLS = frozenset(
    {
        "auto_ingest_budget_status",
        "check_duplicate_entity",
        "context_pack",
        "delta",
        "doctor_vector_lake",
        "entity",
        "export_evidence_packet",
        "get_governance_debt",
        "list_ingest_tasks",
        "mcp_runtime_status",
        "memory_capabilities",
        "projection_report",
        "recall",
        "review_governance_list",
        "review_strategic_purpose",
        "search_timeline",
        "search_vector_lake",
        "semantic_readiness",
        "semantic_readiness_campaign",
        "synthesize",
        "trace_vector_lake",
    }
)
_MCP_SURFACE_ALLOWLISTS = {
    "memory": _MEMORY_MCP_SURFACE_TOOLS,
    "readonly": _READONLY_MCP_SURFACE_TOOLS,
}

_RUNTIME_REVISION_ROOT_FILES = (
    "config.json",
    "runtime_profiles.json",
    "requirements-ci-bootstrap.lock.txt",
    "requirements-ci.lock.txt",
    "requirements.lock.txt",
    "requirements.txt",
    "SCHEMA_CATEGORIES.md",
    "schema.md",
    "watchdog_sync.py",
)
_RUNTIME_REVISION_ASSET_DIRS = (
    "contracts",
    "templates",
)
_RUNTIME_REVISION_PLUGIN_ROOT_ENTRIES = frozenset(
    (*_RUNTIME_REVISION_ROOT_FILES, *_RUNTIME_REVISION_ASSET_DIRS)
)
_HOST_ADAPTER_REVISION_ROOT_FILES = (
    ".mcp.json",
    "CONTEXT.md",
    "gemini-extension.json",
    "mcp.json",
    "mcp_config.json",
    "plugin.json",
)
_HOST_ADAPTER_REVISION_ASSET_DIRS = (
    ".codex-plugin",
    "skills",
)
_HOST_ADAPTER_REVISION_FILES = (
    "scripts/vector_lake_mcp.py",
)


def _require_explicit_capability(env_name: str, action: str) -> None:
    if os.environ.get(env_name) != "1":
        raise PermissionError(
            f"{action} is disabled by default; set {env_name}=1 in the trusted host"
        )



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


def _runtime_revision_paths(source_root: Path) -> list[tuple[str, Path]]:
    """Return the bounded set of code and restart-sensitive plugin assets."""
    source_root = Path(source_root).resolve()
    plugin_root = source_root.parent if source_root.name == "vector_lake" else source_root
    paths: dict[str, Path] = {}

    for source_path in source_root.rglob("*.py"):
        if source_path.is_file():
            relative = source_path.relative_to(plugin_root).as_posix()
            paths[relative] = source_path

    if plugin_root != source_root:
        for filename in _RUNTIME_REVISION_ROOT_FILES:
            candidate = plugin_root / filename
            if candidate.is_file():
                paths[candidate.relative_to(plugin_root).as_posix()] = candidate
        for dirname in _RUNTIME_REVISION_ASSET_DIRS:
            asset_root = plugin_root / dirname
            if not asset_root.is_dir():
                continue
            for candidate in asset_root.rglob("*"):
                if candidate.is_file():
                    paths[candidate.relative_to(plugin_root).as_posix()] = candidate

    return sorted(paths.items())


def _revision_digest(paths: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative_path, source_path in paths:
        try:
            source_bytes = source_path.read_bytes()
        except FileNotFoundError:
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(source_bytes)
        digest.update(b"\x00")
    return digest.hexdigest()


def _source_tree_revision(source_root: Path) -> str:
    """Hash loaded code and restart-sensitive assets for drift detection."""
    return _revision_digest(_runtime_revision_paths(source_root))


def _host_adapter_revision_paths(source_root: Path) -> list[tuple[str, Path]]:
    """Return host manifests, launcher, context, and skill assets."""
    source_root = Path(source_root).resolve()
    plugin_root = source_root.parent if source_root.name == "vector_lake" else source_root
    if plugin_root == source_root:
        return []
    paths: dict[str, Path] = {}
    for filename in (*_HOST_ADAPTER_REVISION_ROOT_FILES, *_HOST_ADAPTER_REVISION_FILES):
        candidate = plugin_root / filename
        if candidate.is_file():
            paths[candidate.relative_to(plugin_root).as_posix()] = candidate
    for dirname in _HOST_ADAPTER_REVISION_ASSET_DIRS:
        asset_root = plugin_root / dirname
        if not asset_root.is_dir():
            continue
        for candidate in asset_root.rglob("*"):
            if candidate.is_file():
                paths[candidate.relative_to(plugin_root).as_posix()] = candidate
    return sorted(paths.items())


def _host_adapter_revision(source_root: Path) -> str:
    return _revision_digest(_host_adapter_revision_paths(source_root))


@dataclass(frozen=True)
class _RuntimeRevisionInventory:
    source_root: Path
    paths: tuple[tuple[str, Path], ...]
    metadata_identity: tuple[tuple, ...]
    directory_tokens: tuple[tuple[Path, str, tuple[tuple, ...]], ...]
    scanned_entry_count: int


def _runtime_revision_inventory_limit(kind: str, observed: int, limit: int) -> None:
    if observed > limit:
        raise RuntimeError(
            "runtime revision inventory "
            f"{kind} limit exceeded: observed={observed}, limit={limit}"
        )


def _runtime_revision_entry_token(entry) -> tuple[tuple, str] | None:
    """Return a stable DirEntry metadata token and its traversal kind."""
    try:
        is_directory = entry.is_dir(follow_symlinks=False)
        is_file = entry.is_file(follow_symlinks=True)
    except FileNotFoundError:
        return ((entry.name, "missing"), "missing")
    if is_directory:
        # Child names detect directory add/delete; each known child directory is
        # scanned independently. Omitting directory timestamps also avoids
        # delayed directory-mtime updates causing a spurious inventory rebuild.
        return ((entry.name, "directory"), "directory")
    kind = "file" if is_file else "other"
    try:
        try:
            observed = entry.stat(follow_symlinks=True)
        except FileNotFoundError:
            observed = entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        return ((entry.name, "missing"), "missing")
    return (
        (
            entry.name,
            kind,
            int(observed.st_dev),
            int(observed.st_ino),
            int(observed.st_mode),
            int(observed.st_size),
            int(observed.st_mtime_ns),
            int(observed.st_ctime_ns),
        ),
        kind,
    )


def _runtime_revision_directory_snapshot(
    directory: Path,
    mode: str,
    *,
    collect_children: bool = True,
) -> tuple[tuple[tuple, ...], tuple[tuple[Path, str], ...], int]:
    """Scan one known directory without recursion and return a bounded token."""
    tokens = []
    children = []
    raw_entry_count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                raw_entry_count += 1
                if (
                    mode == "plugin_root"
                    and entry.name not in _RUNTIME_REVISION_PLUGIN_ROOT_ENTRIES
                ):
                    continue
                if mode == "python" and not entry.name.casefold().endswith(".py"):
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except FileNotFoundError:
                        continue
                token_and_kind = _runtime_revision_entry_token(entry)
                if token_and_kind is None:
                    continue
                token, kind = token_and_kind
                include = False
                if mode == "all":
                    include = kind in {"directory", "file"}
                elif mode == "python":
                    include = kind == "directory" or (
                        kind == "file" and entry.name.casefold().endswith(".py")
                    )
                elif mode == "plugin_root":
                    include = bool(
                        (kind == "directory" and entry.name in _RUNTIME_REVISION_ASSET_DIRS)
                        or (kind == "file" and entry.name in _RUNTIME_REVISION_ROOT_FILES)
                    )
                else:
                    raise ValueError(f"unsupported runtime inventory mode: {mode}")
                if include:
                    tokens.append(token)
                    if collect_children:
                        children.append((Path(entry.path), kind))
    except (FileNotFoundError, NotADirectoryError):
        return (("<missing-directory>",),), (), 0
    tokens.sort(key=lambda item: (str(item[0]).casefold(), str(item[0])))
    if collect_children:
        children.sort(
            key=lambda item: (
                os.path.normcase(str(item[0])),
                str(item[0]),
            )
        )
    return tuple(tokens), tuple(children), raw_entry_count


def _runtime_revision_metadata_from_paths(
    paths: tuple[tuple[str, Path], ...],
) -> tuple[tuple, ...]:
    identity = []
    for relative_path, source_path in paths:
        try:
            observed = source_path.stat()
        except FileNotFoundError:
            continue
        identity.append(
            (
                relative_path,
                int(observed.st_dev),
                int(observed.st_ino),
                int(observed.st_mode),
                int(observed.st_size),
                int(observed.st_mtime_ns),
                int(observed.st_ctime_ns),
            )
        )
    return tuple(identity)


def _build_runtime_revision_inventory(source_root: Path) -> _RuntimeRevisionInventory:
    """Build the bounded startup inventory using iterative ``os.scandir``."""
    source_root = Path(source_root).resolve()
    if not source_root.is_dir():
        raise RuntimeError(f"runtime revision source root is unavailable: {source_root}")
    plugin_root = source_root.parent if source_root.name == "vector_lake" else source_root
    paths: dict[str, Path] = {}
    directory_tokens: dict[Path, tuple[str, tuple[tuple, ...]]] = {}
    scanned_entry_count = 0

    def add_file(candidate: Path) -> None:
        relative = candidate.relative_to(plugin_root).as_posix()
        if relative in paths:
            return
        paths[relative] = candidate
        _runtime_revision_inventory_limit(
            "file",
            len(paths),
            _RUNTIME_REVISION_MAX_FILES,
        )

    def scan_tree(root: Path, mode: str) -> None:
        nonlocal scanned_entry_count
        stack = [root]
        while stack:
            directory = stack.pop()
            if directory in directory_tokens:
                continue
            _runtime_revision_inventory_limit(
                "directory",
                len(directory_tokens) + 1,
                _RUNTIME_REVISION_MAX_DIRECTORIES,
            )
            token, children, entry_count = _runtime_revision_directory_snapshot(
                directory,
                mode,
            )
            if token == (("<missing-directory>",),):
                raise RuntimeError(
                    f"runtime revision directory disappeared during inventory: {directory}"
                )
            scanned_entry_count += entry_count
            _runtime_revision_inventory_limit(
                "scanned entry",
                scanned_entry_count,
                _RUNTIME_REVISION_MAX_SCANNED_ENTRIES,
            )
            directory_tokens[directory] = (mode, token)
            for child, kind in children:
                if kind == "directory":
                    stack.append(child)
                elif kind == "file":
                    add_file(child)

    scan_tree(source_root, "python")
    if plugin_root != source_root:
        _runtime_revision_inventory_limit(
            "directory",
            len(directory_tokens) + 1,
            _RUNTIME_REVISION_MAX_DIRECTORIES,
        )
        root_token, root_children, entry_count = (
            _runtime_revision_directory_snapshot(plugin_root, "plugin_root")
        )
        scanned_entry_count += entry_count
        _runtime_revision_inventory_limit(
            "scanned entry",
            scanned_entry_count,
            _RUNTIME_REVISION_MAX_SCANNED_ENTRIES,
        )
        directory_tokens[plugin_root] = ("plugin_root", root_token)
        for child, kind in root_children:
            if kind == "file":
                add_file(child)
        for dirname in _RUNTIME_REVISION_ASSET_DIRS:
            asset_root = plugin_root / dirname
            if asset_root.is_dir():
                scan_tree(asset_root, "all")

    ordered_paths = tuple(sorted(paths.items()))
    ordered_directories = tuple(
        (directory, mode, token)
        for directory, (mode, token) in sorted(
            directory_tokens.items(),
            key=lambda item: (os.path.normcase(str(item[0])), str(item[0])),
        )
    )
    return _RuntimeRevisionInventory(
        source_root=source_root,
        paths=ordered_paths,
        metadata_identity=_runtime_revision_metadata_from_paths(ordered_paths),
        directory_tokens=ordered_directories,
        scanned_entry_count=scanned_entry_count,
    )


def _refresh_runtime_revision_inventory(
    inventory: _RuntimeRevisionInventory,
) -> tuple[_RuntimeRevisionInventory, bool]:
    """Probe cached directories and rebuild only after a token changes."""
    scanned_entry_count = 0
    for directory, mode, expected_token in inventory.directory_tokens:
        observed_token, _children, entry_count = (
            _runtime_revision_directory_snapshot(
                directory,
                mode,
                collect_children=False,
            )
        )
        scanned_entry_count += entry_count
        _runtime_revision_inventory_limit(
            "scanned entry",
            scanned_entry_count,
            _RUNTIME_REVISION_MAX_SCANNED_ENTRIES,
        )
        if observed_token != expected_token:
            return _build_runtime_revision_inventory(inventory.source_root), True
    return inventory, False


def _runtime_revision_metadata_identity(source_root: Path) -> tuple[tuple, ...]:
    """Describe restart-sensitive files without reading their contents."""
    return _build_runtime_revision_inventory(source_root).metadata_identity


def _runtime_revision_seconds(env_name: str, default: float) -> float:
    try:
        value = float(os.environ.get(env_name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(0.0, value)


def _validated_mcp_call_deadline(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("MCP tool deadline must be a finite number of seconds")
    try:
        deadline_seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "MCP tool deadline must be a finite number of seconds"
        ) from exc
    if not math.isfinite(deadline_seconds) or deadline_seconds < 0:
        raise ValueError("MCP tool deadline must be a finite non-negative number")
    if deadline_seconds > _MCP_CALL_DEADLINE_MAX_SECONDS:
        raise ValueError(
            "MCP tool deadline exceeds hard limit: "
            f"{_MCP_CALL_DEADLINE_MAX_SECONDS:g} seconds"
        )
    return None if deadline_seconds == 0 else deadline_seconds


class MCPRuntimeGuard:
    """Fail closed when the running MCP no longer matches its source tree."""

    def __init__(
        self,
        source_root: Path,
        check_interval_seconds: float | None = None,
        full_hash_interval_seconds: float | None = None,
        strict: bool | None = None,
    ):
        self.source_root = Path(source_root).resolve()
        if check_interval_seconds is None:
            check_interval_seconds = _runtime_revision_seconds(
                "VECTOR_LAKE_MCP_REVISION_CHECK_SECONDS",
                5.0,
            )
        elif not math.isfinite(check_interval_seconds):
            check_interval_seconds = 5.0
        if full_hash_interval_seconds is None:
            full_hash_interval_seconds = _runtime_revision_seconds(
                "VECTOR_LAKE_MCP_REVISION_FULL_HASH_SECONDS",
                _DEFAULT_RUNTIME_REVISION_FULL_HASH_SECONDS,
            )
        elif not math.isfinite(full_hash_interval_seconds):
            full_hash_interval_seconds = _DEFAULT_RUNTIME_REVISION_FULL_HASH_SECONDS
        if strict is None:
            strict = os.environ.get(_RUNTIME_REVISION_STRICT_ENV) == "1"
        self.check_interval_seconds = max(0.0, check_interval_seconds)
        self.full_hash_interval_seconds = max(0.0, full_hash_interval_seconds)
        self.strict = bool(strict)
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        self._revision_inventory = _build_runtime_revision_inventory(
            self.source_root
        )
        self.loaded_revision = _source_tree_revision(self.source_root)
        self.loaded_host_adapter_revision = _host_adapter_revision(self.source_root)
        self._metadata_identity = self._revision_inventory.metadata_identity
        self._current_revision = self.loaded_revision
        now = time.monotonic()
        checked_at = datetime.now(timezone.utc).isoformat()
        self._last_checked = now
        self._last_full_hash_monotonic = now
        self._last_metadata_check_at = checked_at
        self._last_full_hash_at = checked_at
        self._last_check_kind = "startup_full"
        self._last_check_duration_ms = (time.perf_counter() - started) * 1000.0
        self._metadata_checks = 1
        self._full_hashes = 1
        self._cached_checks = 0
        self._singleflight_waits = 0
        self._inventory_rebuilds = 1
        self._lock = threading.Lock()

    def status(self, force: bool = False) -> dict:
        waited_for_refresh = not self._lock.acquire(blocking=False)
        if waited_for_refresh:
            self._lock.acquire()
        try:
            if waited_for_refresh:
                self._singleflight_waits += 1
            now = time.monotonic()
            exact_check = bool(force or self.strict)
            served_from_cache = bool(
                not exact_check
                and (
                    waited_for_refresh
                    or now - self._last_checked < self.check_interval_seconds
                )
            )
            if served_from_cache:
                self._cached_checks += 1
            else:
                started = time.perf_counter()
                revision_inventory, inventory_rebuilt = (
                    _refresh_runtime_revision_inventory(self._revision_inventory)
                )
                if inventory_rebuilt:
                    self._inventory_rebuilds += 1
                metadata_identity = revision_inventory.metadata_identity
                self._metadata_checks += 1
                metadata_changed = metadata_identity != self._metadata_identity
                periodic_full_hash = bool(
                    now - self._last_full_hash_monotonic
                    >= self.full_hash_interval_seconds
                )
                if exact_check or metadata_changed or periodic_full_hash:
                    self._current_revision = _source_tree_revision(self.source_root)
                    self._full_hashes += 1
                    self._last_full_hash_monotonic = time.monotonic()
                    self._last_full_hash_at = datetime.now(timezone.utc).isoformat()
                    if force:
                        check_kind = "forced_full"
                    elif self.strict:
                        check_kind = "strict_full"
                    elif metadata_changed:
                        check_kind = "metadata_changed_full"
                    else:
                        check_kind = "periodic_full"
                else:
                    check_kind = "metadata"
                self._revision_inventory = revision_inventory
                self._metadata_identity = metadata_identity
                self._last_checked = time.monotonic()
                self._last_metadata_check_at = datetime.now(timezone.utc).isoformat()
                self._last_check_kind = check_kind
                self._last_check_duration_ms = (
                    time.perf_counter() - started
                ) * 1000.0
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
                "full_hash_interval_seconds": self.full_hash_interval_seconds,
                "strict": self.strict,
                "revision_path_count": len(self._metadata_identity),
                "inventory_file_count": len(self._revision_inventory.paths),
                "inventory_directory_count": len(
                    self._revision_inventory.directory_tokens
                ),
                "inventory_scanned_entry_count": (
                    self._revision_inventory.scanned_entry_count
                ),
                "inventory_file_limit": _RUNTIME_REVISION_MAX_FILES,
                "inventory_directory_limit": _RUNTIME_REVISION_MAX_DIRECTORIES,
                "inventory_scanned_entry_limit": (
                    _RUNTIME_REVISION_MAX_SCANNED_ENTRIES
                ),
                "inventory_rebuilds": self._inventory_rebuilds,
                "metadata_checks": self._metadata_checks,
                "full_hashes": self._full_hashes,
                "cached_checks": self._cached_checks,
                "singleflight_waits": self._singleflight_waits,
                "last_check_kind": self._last_check_kind,
                "last_check_duration_ms": round(self._last_check_duration_ms, 3),
                "last_metadata_check_at": self._last_metadata_check_at,
                "last_full_hash_at": self._last_full_hash_at,
                "served_from_cache": served_from_cache,
            }
        finally:
            self._lock.release()

    def assert_current(self, *, force: bool = False) -> None:
        status = self.status(force=force)
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
        self._default_call_deadline_seconds = _validated_mcp_call_deadline(
            os.environ.get("VECTOR_LAKE_MCP_TOOL_DEADLINE_SECONDS", "0")
        )
        self._cancellation_registry = CancellationRegistry()
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
                    "VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY",
                    str(_DEFAULT_BLOCKING_QUEUE_CAPACITY),
                )
            )
        except (TypeError, ValueError):
            queue_capacity = _DEFAULT_BLOCKING_QUEUE_CAPACITY
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
            thread_name_prefix="vector-lake-mcp-fast",
        )
        try:
            heavy_worker_count = int(
                os.environ.get(
                    "VECTOR_LAKE_MCP_HEAVY_WORKERS",
                    str(_DEFAULT_HEAVY_WORKERS),
                )
            )
        except (TypeError, ValueError):
            heavy_worker_count = _DEFAULT_HEAVY_WORKERS
        heavy_worker_count = max(1, min(2, heavy_worker_count))
        try:
            heavy_queue_capacity = int(
                os.environ.get(
                    "VECTOR_LAKE_MCP_HEAVY_QUEUE_CAPACITY",
                    str(heavy_worker_count),
                )
            )
        except (TypeError, ValueError):
            heavy_queue_capacity = heavy_worker_count
        heavy_queue_capacity = max(0, min(8, heavy_queue_capacity))
        self._heavy_worker_count = heavy_worker_count
        self._heavy_queue_capacity = heavy_queue_capacity
        self._heavy_slots = threading.BoundedSemaphore(
            heavy_worker_count + heavy_queue_capacity
        )
        self._heavy_inflight = 0
        self._heavy_executor = _DaemonThreadPoolExecutor(
            max_workers=heavy_worker_count,
            thread_name_prefix="vector-lake-mcp-heavy",
        )
        self._control_worker_count = 1
        self._control_queue_capacity = 1
        self._control_slots = threading.BoundedSemaphore(2)
        self._control_inflight = 0
        self._control_executor = _DaemonThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="vector-lake-mcp-control",
        )
        self._lane_metrics_lock = threading.Lock()
        self._lane_metrics = {
            lane: {
                "submitted": 0,
                "started": 0,
                "completed": 0,
                "failed": 0,
                "admission_rejections": 0,
                "queue_wait_seconds_total": 0.0,
                "queue_wait_seconds_max": 0.0,
                "execution_seconds_total": 0.0,
                "execution_seconds_max": 0.0,
            }
            for lane in ("fast", "heavy", "control")
        }
        self._executor_shutdown_lock = threading.Lock()
        self._executor_shutdown_started = False
        self._executor_shutdown_timed_out = False
        self._executor_finalizer = weakref.finalize(
            self, _finalize_blocking_executor, self._blocking_executor
        )
        self._heavy_executor_finalizer = weakref.finalize(
            self, _finalize_blocking_executor, self._heavy_executor
        )
        self._control_executor_finalizer = weakref.finalize(
            self, _finalize_blocking_executor, self._control_executor
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

    def _observed_lane_call(self, lane: str, call):
        submitted_at = time.monotonic()
        with self._lane_metrics_lock:
            self._lane_metrics[lane]["submitted"] += 1

        def observed():
            started_at = time.monotonic()
            queue_wait = max(0.0, started_at - submitted_at)
            with self._lane_metrics_lock:
                metrics = self._lane_metrics[lane]
                metrics["started"] += 1
                metrics["queue_wait_seconds_total"] += queue_wait
                metrics["queue_wait_seconds_max"] = max(
                    metrics["queue_wait_seconds_max"], queue_wait
                )
            failed = True
            try:
                result = call()
                failed = False
                return result
            finally:
                elapsed = max(0.0, time.monotonic() - started_at)
                with self._lane_metrics_lock:
                    metrics = self._lane_metrics[lane]
                    metrics["completed"] += 1
                    metrics["failed"] += int(failed)
                    metrics["execution_seconds_total"] += elapsed
                    metrics["execution_seconds_max"] = max(
                        metrics["execution_seconds_max"], elapsed
                    )

        return observed

    def _lane_metrics_snapshot(self, lane: str) -> dict:
        with self._lane_metrics_lock:
            return {
                key: round(value, 6) if isinstance(value, float) else int(value)
                for key, value in self._lane_metrics[lane].items()
            }

    def _record_admission_rejection(self, lane: str) -> None:
        with self._lane_metrics_lock:
            self._lane_metrics[lane]["admission_rejections"] += 1

    def _new_cancellation_operation(
        self,
        *,
        tool_name: str,
        lane: str,
    ) -> CancellationOperation:
        deadline = current_request_deadline()
        if deadline is None:
            deadline = make_request_deadline(self._default_call_deadline_seconds)
        return self._cancellation_registry.create(
            tool_name=tool_name,
            lane=lane,
            deadline=deadline,
        )

    def _readonly_scan_without_shared_gate(self, policy) -> bool:
        """Keep the readonly MCP surface physically read-only in canonical meta.

        The dedicated heavy executor already supplies bounded in-process
        admission.  Readonly exposes only scan-class heavy tools, so acquiring
        the cross-process file gate would add control-plane writes without
        protecting any mutation performed by this surface.
        """
        if policy is None or policy[0] != "scan":
            return False
        effective_surface = str(
            getattr(
                self,
                "_vector_lake_effective_surface",
                os.environ.get(_MCP_SURFACE_ENV, "full"),
            )
        ).strip().lower()
        return effective_surface == "readonly"

    def _effective_call_deadline(self, requested_value) -> float | None:
        requested = _validated_mcp_call_deadline(requested_value)
        configured = self._default_call_deadline_seconds
        if configured is None:
            return requested
        if requested is None:
            return configured
        return min(configured, requested)

    @staticmethod
    def _raise_if_operation_deadline_expired(
        operation: CancellationOperation | None,
    ) -> None:
        if operation is None or not operation.deadline_expired():
            return
        operation.cancel_before_start("deadline_exceeded")
        raise ToolDeadlineExceeded(operation)

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
                return self._blocking_executor.submit_tracked(
                    self._observed_lane_call("fast", call),
                    release_slot,
                )
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
            self._record_admission_rejection("fast")
            raise RuntimeError(
                "Vector Lake MCP blocking executor is saturated; retry later; "
                f"retry_after_seconds={self._blocking_admission_timeout:.3f}; "
                "lane=fast"
            )
        return self._submit_admitted_blocking_call(call)

    def _submit_admitted_heavy_call(self, call):
        released = False

        def release_slot():
            nonlocal released
            with self._executor_shutdown_lock:
                if released:
                    return
                released = True
                self._heavy_inflight -= 1
            self._heavy_slots.release()

        with self._executor_shutdown_lock:
            if self._executor_shutdown_started:
                self._heavy_slots.release()
                raise RuntimeError("Vector Lake MCP server is shutting down")
            self._heavy_inflight += 1
            try:
                return self._heavy_executor.submit_tracked(
                    self._observed_lane_call("heavy", call),
                    release_slot,
                )
            except BaseException:
                self._heavy_inflight -= 1
                self._heavy_slots.release()
                raise

    def _submit_heavy_call(self, call):
        admitted = self._heavy_slots.acquire(
            timeout=self._blocking_admission_timeout
        )
        if not admitted:
            self._record_admission_rejection("heavy")
            raise RuntimeError(
                "Vector Lake MCP heavy executor is saturated; "
                f"retry_after_seconds={self._blocking_admission_timeout:.3f}"
            )
        return self._submit_admitted_heavy_call(call)

    def _submit_admitted_control_call(self, call):
        released = False

        def release_slot():
            nonlocal released
            with self._executor_shutdown_lock:
                if released:
                    return
                released = True
                self._control_inflight -= 1
            self._control_slots.release()

        with self._executor_shutdown_lock:
            if self._executor_shutdown_started:
                self._control_slots.release()
                raise RuntimeError("Vector Lake MCP server is shutting down")
            self._control_inflight += 1
            try:
                return self._control_executor.submit_tracked(
                    self._observed_lane_call("control", call),
                    release_slot,
                )
            except BaseException:
                self._control_inflight -= 1
                self._control_slots.release()
                raise

    async def _acquire_control_slot(
        self,
        operation: CancellationOperation | None = None,
    ) -> None:
        deadline = time.monotonic() + self._blocking_admission_timeout
        while True:
            self._raise_if_operation_deadline_expired(operation)
            self._assert_accepting_calls()
            if self._control_slots.acquire(blocking=False):
                return
            if time.monotonic() >= deadline:
                self._record_admission_rejection("control")
                raise RuntimeError(
                    "Vector Lake MCP control executor is saturated; retry later; "
                    f"retry_after_seconds={self._blocking_admission_timeout:.3f}; "
                    "lane=control"
                )
            await asyncio.sleep(
                min(0.01, max(0.001, deadline - time.monotonic()))
            )

    async def _acquire_blocking_slot(
        self,
        operation: CancellationOperation | None = None,
    ) -> None:
        deadline = time.monotonic() + self._blocking_admission_timeout
        while True:
            self._raise_if_operation_deadline_expired(operation)
            self._assert_accepting_calls()
            if self._blocking_slots.acquire(blocking=False):
                return
            if time.monotonic() >= deadline:
                self._record_admission_rejection("fast")
                raise RuntimeError(
                    "Vector Lake MCP blocking executor is saturated; retry later; "
                    f"retry_after_seconds={self._blocking_admission_timeout:.3f}; "
                    "lane=fast"
                )
            sleep_seconds = min(0.01, max(0.001, deadline - time.monotonic()))
            operation_remaining = (
                None if operation is None else operation.remaining_seconds()
            )
            if operation_remaining is not None:
                sleep_seconds = min(
                    sleep_seconds,
                    max(0.001, operation_remaining),
                )
            await asyncio.sleep(sleep_seconds)

    async def _acquire_heavy_slot(
        self,
        operation: CancellationOperation | None = None,
    ) -> None:
        deadline = time.monotonic() + self._blocking_admission_timeout
        while True:
            self._raise_if_operation_deadline_expired(operation)
            self._assert_accepting_calls()
            if self._heavy_slots.acquire(blocking=False):
                return
            if time.monotonic() >= deadline:
                self._record_admission_rejection("heavy")
                raise RuntimeError(
                    "Vector Lake MCP heavy executor is saturated; "
                    f"retry_after_seconds={self._blocking_admission_timeout:.3f}"
                )
            sleep_seconds = min(0.01, max(0.001, deadline - time.monotonic()))
            operation_remaining = (
                None if operation is None else operation.remaining_seconds()
            )
            if operation_remaining is not None:
                sleep_seconds = min(
                    sleep_seconds,
                    max(0.001, operation_remaining),
                )
            await asyncio.sleep(sleep_seconds)

    async def _run_executor_call(
        self,
        call,
        *,
        operation: CancellationOperation | None,
        acquire_slot,
        submit_call,
    ):
        def consume_detached_result(completed_future) -> None:
            try:
                completed_future.exception()
            except BaseException:
                # The operation registry is the authoritative detached outcome.
                # Retrieving the exception prevents asyncio from emitting a
                # misleading "Future exception was never retrieved" diagnostic.
                pass

        try:
            await acquire_slot(operation)
        except asyncio.CancelledError:
            if operation is not None:
                operation.cancel_before_start("client_cancelled")
            raise
        except ToolDeadlineExceeded:
            raise
        except BaseException as exc:
            if operation is not None:
                operation.mark_failed(exc)
            raise
        try:
            future = submit_call(call)
        except BaseException as exc:
            if operation is not None:
                operation.mark_failed(exc)
            raise
        wrapped = asyncio.wrap_future(future)
        try:
            remaining = None if operation is None else operation.remaining_seconds()
            if remaining is None:
                return await asyncio.shield(wrapped)
            completed, _pending = await asyncio.wait(
                (wrapped,),
                timeout=max(0.0, remaining),
            )
            if completed:
                return await wrapped
            queued_cancelled = future.cancel()
            if operation is not None:
                if queued_cancelled:
                    operation.cancel_before_start("deadline_exceeded")
                else:
                    future.add_done_callback(consume_detached_result)
                    operation.request_cancellation(
                        "deadline_exceeded",
                        detached=True,
                    )
                raise ToolDeadlineExceeded(operation)
            raise TimeoutError("MCP blocking call deadline exceeded")
        except asyncio.CancelledError:
            queued_cancelled = future.cancel()
            if operation is not None:
                if queued_cancelled:
                    operation.cancel_before_start("client_cancelled")
                else:
                    future.add_done_callback(consume_detached_result)
                    operation.request_cancellation(
                        "client_cancelled",
                        detached=True,
                    )
                snapshot = operation.snapshot()
                logging.getLogger(__name__).info(
                    "MCP request cancelled; operation_id=%s status=%s",
                    snapshot["operation_id"],
                    snapshot["status"],
                )
            raise

    async def _run_control_call(
        self,
        call,
        operation: CancellationOperation | None = None,
    ):
        return await self._run_executor_call(
            call,
            operation=operation,
            acquire_slot=self._acquire_control_slot,
            submit_call=self._submit_admitted_control_call,
        )

    async def _run_blocking_call(
        self,
        call,
        operation: CancellationOperation | None = None,
    ):
        return await self._run_executor_call(
            call,
            operation=operation,
            acquire_slot=self._acquire_blocking_slot,
            submit_call=self._submit_admitted_blocking_call,
        )

    async def _run_heavy_call(
        self,
        call,
        operation: CancellationOperation | None = None,
    ):
        return await self._run_executor_call(
            call,
            operation=operation,
            acquire_slot=self._acquire_heavy_slot,
            submit_call=self._submit_admitted_heavy_call,
        )

    def blocking_executor_status(self) -> dict:
        executor_status = self._blocking_executor.status_snapshot()
        heavy_executor_status = self._heavy_executor.status_snapshot()
        control_executor_status = self._control_executor.status_snapshot()
        with self._executor_shutdown_lock:
            shutdown_started = self._executor_shutdown_started
            inflight = self._blocking_inflight
            timed_out = self._executor_shutdown_timed_out
            heavy_inflight = self._heavy_inflight
            control_inflight = self._control_inflight
        fast_lane = {
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
            "metrics": self._lane_metrics_snapshot("fast"),
        }
        heavy_lane = {
            "workers": self._heavy_worker_count,
            "queue_capacity": self._heavy_queue_capacity,
            "inflight": heavy_inflight,
            "queued_items": heavy_executor_status["queued_items"],
            "admission_timeout_seconds": self._blocking_admission_timeout,
            "shutdown_timeout_seconds": self._blocking_shutdown_timeout,
            "workers_daemon": heavy_executor_status["workers_daemon"],
            "running_workers": heavy_executor_status["running_workers"],
            "shutdown_started": shutdown_started,
            "shutdown_completed": shutdown_started
            and heavy_executor_status["shutdown_completed"],
            "shutdown_timed_out": timed_out,
            "metrics": self._lane_metrics_snapshot("heavy"),
        }
        control_lane = {
            "workers": self._control_worker_count,
            "queue_capacity": self._control_queue_capacity,
            "inflight": control_inflight,
            "queued_items": control_executor_status["queued_items"],
            "admission_timeout_seconds": self._blocking_admission_timeout,
            "shutdown_timeout_seconds": self._blocking_shutdown_timeout,
            "workers_daemon": control_executor_status["workers_daemon"],
            "running_workers": control_executor_status["running_workers"],
            "shutdown_started": shutdown_started,
            "shutdown_completed": shutdown_started
            and control_executor_status["shutdown_completed"],
            "shutdown_timed_out": timed_out,
            "metrics": self._lane_metrics_snapshot("control"),
        }
        combined = dict(fast_lane)
        combined["shutdown_completed"] = (
            fast_lane["shutdown_completed"]
            and heavy_lane["shutdown_completed"]
            and control_lane["shutdown_completed"]
        )
        combined["fast_lane"] = fast_lane
        combined["heavy_lane"] = heavy_lane
        combined["control_lane"] = control_lane
        return combined

    def cancellation_status(self, operation_id: str = "") -> dict:
        operation_id = str(operation_id or "").strip()
        if len(operation_id) > 128:
            raise ValueError("operation_id exceeds 128 characters")
        status = self._cancellation_registry.snapshot(operation_id)
        status.update(
            {
                "contract_version": CANCELLATION_CONTRACT_VERSION,
                "deadline_argument": _MCP_CALL_DEADLINE_ARGUMENT,
                "default_deadline_seconds": self._default_call_deadline_seconds,
                "deadline_max_seconds": _MCP_CALL_DEADLINE_MAX_SECONDS,
                "checkpoint_contract": (
                    "interruptible scans exit at the next cancellation checkpoint; "
                    "non-interruptible phases remain atomic and report "
                    "cancellation_pending until terminal"
                ),
            }
        )
        return status

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
            if self._heavy_executor_finalizer.alive:
                self._heavy_executor_finalizer.detach()
            if self._control_executor_finalizer.alive:
                self._control_executor_finalizer.detach()
        if first_shutdown or wait:
            deadline = time.monotonic() + timeout if wait else None
            fast_completed = self._blocking_executor.shutdown(
                wait=wait,
                cancel_futures=True,
                timeout=timeout if wait else None,
            )
            heavy_timeout = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                else None
            )
            heavy_completed = self._heavy_executor.shutdown(
                wait=wait,
                cancel_futures=True,
                timeout=heavy_timeout,
            )
            control_timeout = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                else None
            )
            control_completed = self._control_executor.shutdown(
                wait=wait,
                cancel_futures=True,
                timeout=control_timeout,
            )
            if wait and not (
                fast_completed and heavy_completed and control_completed
            ):
                with self._executor_shutdown_lock:
                    self._executor_shutdown_timed_out = True
                logging.getLogger(__name__).warning(
                    "Vector Lake MCP blocking executor did not drain within %.3f seconds",
                    timeout,
                )

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            if fn.__name__ == "mcp_runtime_status" and not inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def direct_runtime_status(*fn_args, **fn_kwargs):
                    # Keep status independent from the fast/heavy lanes without
                    # hashing source files on the event-loop thread.
                    self._assert_accepting_calls()
                    call = functools.partial(
                        self._invoke_blocking_tool,
                        functools.partial(fn, *fn_args, **fn_kwargs),
                    )
                    return await self._run_control_call(call)

                register(direct_runtime_status)
                return fn

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
                policy = _MCP_HEAVY_TASKS.get(fn.__name__)
                if (
                    fn.__name__ == "doctor_vector_lake"
                    and str(fn_kwargs.get("mode") or "quick").strip().casefold()
                    == "quick"
                ):
                    policy = None
                lane = "heavy" if policy is not None else "fast"
                operation = self._new_cancellation_operation(
                    tool_name=fn.__name__,
                    lane=lane,
                )

                def invoke_current_tool():
                    with bind_cancellation_operation(operation):
                        operation.mark_running()
                        result = None
                        try:
                            cancellation_checkpoint("worker_start")
                            if fn.__name__ != "mcp_runtime_status":
                                self.runtime_guard.assert_current()
                            cancellation_checkpoint("after_runtime_guard")
                            if policy is None or self._readonly_scan_without_shared_gate(
                                policy
                            ):
                                result = fn(*fn_args, **fn_kwargs)
                            else:
                                from vector_lake.heavy_task_gate import heavy_task

                                task_class, warn_after_seconds = policy
                                cancellation_checkpoint("before_heavy_gate")
                                with heavy_task(
                                    task_class,
                                    fn.__name__,
                                    origin="mcp",
                                    wait_timeout_seconds=self._heavy_task_wait,
                                    warn_after_seconds=warn_after_seconds,
                                ):
                                    cancellation_checkpoint("after_heavy_gate")
                                    result = fn(*fn_args, **fn_kwargs)
                            operation.mark_completed()
                            return result
                        except CooperativeCancellation:
                            raise
                        except BaseException as exc:
                            operation.mark_failed(exc)
                            raise

                call = functools.partial(
                    self._invoke_blocking_tool,
                    invoke_current_tool,
                )
                if policy is not None:
                    return await self._run_heavy_call(call, operation)
                return await self._run_blocking_call(call, operation)

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
            # Admission and execution share the bounded cached guard. A due refresh
            # is single-flight, while force remains reserved for runtime status.
            self.runtime_guard.assert_current(force=False)
        tool_arguments = dict(arguments or {})
        configured_deadline = tool_arguments.pop(
            _MCP_CALL_DEADLINE_ARGUMENT,
            None,
        )
        deadline_seconds = self._effective_call_deadline(configured_deadline)
        with request_deadline_scope(deadline_seconds):
            return await super().call_tool(name, tool_arguments)

# Global lock against stdout pollution
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', force=True)
from vector_lake import tool_memory  # noqa: E402
from vector_lake import tools  # noqa: E402
from vector_lake.governance_store import insert_governance_item_if_absent, _utc_now  # noqa: E402
from vector_lake.tool_timeline import search_timeline_events  # noqa: E402

mcp = ReloadAwareFastMCP("vector-lake")


def _mcp_surface_status(server: FastMCP) -> dict:
    effective_tools = tuple(
        sorted(tool.name for tool in server._tool_manager.list_tools())
    )
    requested_surface = str(
        os.environ.get(_MCP_SURFACE_ENV, "full")
    ).strip().lower()
    return {
        "configured_surface": str(
            getattr(server, "_vector_lake_configured_surface", requested_surface)
        ),
        "effective_surface": str(
            getattr(server, "_vector_lake_effective_surface", "unconfigured")
        ),
        "effective_tool_count": len(effective_tools),
        "effective_tools": list(effective_tools),
    }


@mcp.tool()
def mcp_runtime_status(operation_id: str = "") -> str:
    """Report runtime staleness separately from host-adapter drift."""
    status = mcp.runtime_guard.status(force=True)
    host_paths = _host_adapter_revision_paths(mcp.runtime_guard.source_root)
    current_host_revision = _revision_digest(host_paths)
    host_changed = (
        current_host_revision
        != mcp.runtime_guard.loaded_host_adapter_revision
    )
    status["runtime_revision"] = {
        "loaded": status["loaded_revision"],
        "current": status["current_revision"],
        "stale": status["stale"],
        "mcp_restart_required": status["restart_required"],
    }
    status["host_adapter_revision"] = {
        "loaded": mcp.runtime_guard.loaded_host_adapter_revision,
        "current": current_host_revision,
        "changed_since_start": host_changed,
        "path_count": len(host_paths),
        "mcp_restart_required": False,
        "host_reload_required": host_changed,
    }
    status["blocking_executor"] = mcp.blocking_executor_status()
    status["cancellation"] = mcp.cancellation_status(operation_id)
    from vector_lake.tool_search import search_performance_status

    status["search_performance"] = search_performance_status()
    from vector_lake.heavy_task_gate import heavy_task_gate_status

    status["heavy_task_gate"] = heavy_task_gate_status()
    from vector_lake.tool_auto_ingest import auto_ingest_budget_status as budget_status

    status["auto_ingest_budget"] = budget_status(include_actual_usage=False)
    status.update(_mcp_surface_status(mcp))
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
        entity_name=entity_name,
        sentiment=sentiment,
        action=action,
        limit=limit,
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
def recover_failed_mutation_outbox(
    outbox_ids: list[int],
    dry_run: bool = True,
) -> str:
    """Preview or explicitly recover exact failed mutation-outbox rows."""
    if not outbox_ids:
        raise ValueError("At least one outbox id is required.")
    if len(outbox_ids) > 100:
        raise ValueError("At most 100 outbox ids may be recovered at once.")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in outbox_ids
    ):
        raise ValueError("Outbox ids must be positive integers.")
    selected_ids = sorted(set(outbox_ids))
    from vector_lake import db_store

    result = (
        db_store.preview_failed_mutation_outbox_recovery(selected_ids)
        if dry_run
        else db_store.recover_failed_mutation_outbox(selected_ids)
    )
    return json.dumps(
        {
            "dry_run": dry_run,
            "requested_ids": selected_ids,
            **result,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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
        mode: 'page', 'memory', or fact-only operational-memory 'fact'. The
            legacy 'claim' value is a deprecated alias for 'fact' and does not
            return canonical Claim records.
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
    if include_evidence_text:
        _require_explicit_capability(
            _EVIDENCE_TEXT_EXPORT_ENV,
            "evidence text export",
        )
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
def semantic_readiness_campaign(limit: int = 50, cursor: str = "") -> str:
    """Page exact semantic debt bound to current canonical and graph generations."""
    return tools.semantic_readiness_campaign_report(limit=limit, cursor=cursor)


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
    from vector_lake.native_llm import peek_subagent_brain_root

    requested_path = Path(payload_file).expanduser()
    if not requested_path.is_absolute():
        raise ValueError("[Security Error] Payload path must be absolute")
    lexical_path = Path(os.path.abspath(str(requested_path)))
    configured_root = os.environ.get("VECTOR_LAKE_PAYLOAD_ROOT")
    allowed_root: Path | None = None
    if configured_root:
        candidate_root = Path(os.path.abspath(str(Path(configured_root).expanduser())))
        allowed = lexical_path.is_relative_to(candidate_root)
        if allowed:
            allowed_root = candidate_root
    else:
        allowed = False
        brain_roots = [
            Path(os.path.abspath(str(peek_subagent_brain_root()))),
        ]
        for root in brain_roots:
            if not lexical_path.is_relative_to(root):
                continue
            relative_parts = lexical_path.relative_to(root).parts
            if len(relative_parts) >= 3 and relative_parts[1].lower() == "scratch":
                allowed = True
                allowed_root = root
                break
    if not allowed:
        raise ValueError(f"[Security Error] Payload file must be within an approved agent sandbox: {payload_file}")
    assert allowed_root is not None
    root_stat = os.lstat(allowed_root)
    if allowed_root.is_symlink() or (
        getattr(root_stat, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("[Security Error] Approved payload root is a reparse point")
    current = allowed_root
    for part in lexical_path.relative_to(allowed_root).parts:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError as exc:
            raise ValueError(
                f"[Sandbox Error] Payload file not found: {payload_file}"
            ) from exc
        if current.is_symlink() or (
            getattr(current_stat, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError(
                f"[Security Error] Payload path contains a reparse point: {payload_file}"
            )
    abs_path = lexical_path.resolve(strict=True)
    if not abs_path.is_relative_to(allowed_root.resolve(strict=True)):
        raise ValueError(
            f"[Security Error] Payload resolved outside the approved sandbox: {payload_file}"
        )
    max_bytes = max(1, int(os.environ.get("VECTOR_LAKE_PAYLOAD_MAX_BYTES", str(5 * 1024 * 1024))))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(abs_path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("[Security Error] Payload must be a regular file")
        if before.st_size > max_bytes:
            raise ValueError(
                f"[Sandbox Error] Payload file exceeds {max_bytes} bytes: {payload_file}"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload_bytes = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload_bytes) > max_bytes:
            raise ValueError(
                f"[Sandbox Error] Payload file exceeds {max_bytes} bytes: {payload_file}"
            )
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("[Security Error] Payload changed while it was being read")
        path_after = os.stat(abs_path, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (path_after.st_dev, path_after.st_ino):
            raise ValueError("[Security Error] Payload identity changed during read")
    finally:
        os.close(descriptor)
    try:
        return payload_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("[Sandbox Error] Payload must be strict UTF-8") from exc

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
def memory_capabilities() -> str:
    """Return verbs available on this process's effective MCP tool surface."""
    return json.dumps(
        _memory_capability_manifest(mcp),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _memory_capability_manifest(server: FastMCP) -> dict:
    """Bind the protocol manifest to one server's effective tools/list."""
    from vector_lake.memory_protocol import capability_manifest

    status = _mcp_surface_status(server)
    effective_surface = str(status["effective_surface"])
    if effective_surface not in {"full", "memory", "readonly"}:
        # Before configure_mcp_surface runs, tools/list is still the full surface.
        effective_surface = "full"
    return capability_manifest(
        effective_surface=effective_surface,
        available_tools=status["effective_tools"],
    )


@mcp.tool()
def unsupported_claim_debt(
    dry_run: bool = True,
    review_days: int = 30,
    confirmation: str = "",
) -> str:
    """Preview or apply one exact runtime unsupported-claim governance plan."""
    return json.dumps(
        tools.register_unsupported_claim_debt(
            dry_run=dry_run,
            review_days=review_days,
            runtime_only=True,
            confirmation=confirmation,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def record_claim_assessment(
    claim_id: str,
    assessment_type: str,
    outcome: str,
    actor_id: str,
    method_version: str,
    reason: str,
    expected_claim_version: str,
    details_json: str = "{}",
) -> str:
    """Append a review result only when the reviewed claim version is current."""
    details = json.loads(details_json or "{}")
    if not isinstance(details, dict):
        raise ValueError("details_json must decode to a JSON object")
    return json.dumps(
        tools.record_claim_assessment(
            claim_id,
            assessment_type=assessment_type,
            outcome=outcome,
            actor_id=actor_id,
            method_version=method_version,
            reason=reason,
            details=details,
            expected_claim_version=expected_claim_version,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def recall(
    query: str,
    top_k: int = 5,
    mode: str = "page",
    include_history: bool = False,
) -> str:
    """Recall pages, memory, or facts; claim is a deprecated fact alias."""
    from vector_lake.memory_protocol import recall as recall_memory

    return json.dumps(
        recall_memory(
            query,
            top_k=top_k,
            mode=mode,
            include_history=include_history,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def remember(memory_type: str, payload_file: str) -> str:
    """Persist one governed operational-memory observation from a sandboxed file."""
    from vector_lake.memory_protocol import MEMORY_PROTOCOL_VERSION
    from vector_lake.memory_protocol import remember as remember_memory

    try:
        content = _read_payload(payload_file)
    except Exception as exc:
        logging.warning("remember payload rejected: %s", type(exc).__name__)
        return json.dumps(
            {
                "contract_version": MEMORY_PROTOCOL_VERSION,
                "verb": "remember",
                "ok": False,
                "committed": False,
                "error_code": "invalid_payload",
                "message": "Memory payload was rejected before mutation.",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    return json.dumps(
        remember_memory(memory_type, content),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def entity(name: str, limit: int = 10, include_history: bool = False) -> str:
    """Resolve exact page keys, canonical ids, titles, and aliases."""
    from vector_lake.memory_protocol import entity as resolve_memory_entity

    return json.dumps(
        resolve_memory_entity(
            name,
            limit=limit,
            include_history=include_history,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def synthesize(query: str) -> str:
    """Assemble proposal-only synthesis context without committing a page."""
    from vector_lake.memory_protocol import synthesize as synthesize_memory

    return json.dumps(
        synthesize_memory(query),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def context_pack(query: str, max_chars: int = 32000) -> str:
    """Build a server-budgeted context packet for an Agent session boundary."""
    from vector_lake.memory_protocol import context_pack as pack_memory_context

    return json.dumps(
        pack_memory_context(query, max_chars=max_chars),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@mcp.tool()
def delta(since: str, limit: int = 100) -> str:
    """Return current page projection updates since an ISO 8601 timestamp."""
    from vector_lake.memory_protocol import delta as memory_delta

    return json.dumps(
        memory_delta(since, limit=limit),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

@mcp.tool()
def sync_vector_lake() -> str:
    """(Legacy Alias) Trigger an ingestion batch scan. Replaced by the asynchronous Subagent pipeline, now wraps prepare_ingest_batch."""
    try:
        return tools.sync_vector_lake()
    except Exception as e:
        incident = uuid.uuid4().hex[:12]
        logging.exception("MCP sync_vector_lake failed incident=%s", incident)
        return f"MCP Exception: {type(e).__name__}; incident={incident}"

@mcp.tool()
def lint_vector_lake(auto_fix: bool = False) -> str:
    """Run self-healing audit on the Wiki nodes.
    
    Args:
        auto_fix: Automatically fix issues such as decaying notes.
    """
    try:
        return tools.lint_vector_lake(auto_fix=auto_fix)
    except Exception as e:
        incident = uuid.uuid4().hex[:12]
        logging.exception("MCP lint_vector_lake failed incident=%s", incident)
        return f"MCP Exception: {type(e).__name__}; incident={incident}"

@mcp.tool()
def query_logic_lake(query_str: str, dry_run: bool = True) -> str:
    """Read-only reasoning context by default; job creation is operator-gated.
    
    Args:
        query_str: The topic or command for reasoning.
        dry_run: Keep context in memory without creating a query job. Defaults to true.
    """
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if not dry_run:
        _require_explicit_capability(
            _MANUAL_QUERY_SYNTHESIS_ENV,
            "Manual query synthesis",
        )
    return tools.prepare_query_context(query_str, dry_run=dry_run)

@mcp.tool()
def finalize_query_synthesis(completion_json: str, query_str: str) -> str:
    """Atomically commit a prepared query job's bounded synthesis proposals.

    Args:
        completion_json: Strict JSON completion containing job_id, nonce, and
            inline Synthesis_ proposals with SHA-256 digests.
        query_str: The exact original query bound to the prepared job.
    """
    _require_explicit_capability(
        _MANUAL_QUERY_SYNTHESIS_ENV,
        "Manual query synthesis finalization",
    )
    return tools.finalize_query_synthesis(completion_json, query_str)

@mcp.tool()
def review_governance_list() -> str:
    """List pending items in the governance review queue (contradictions, gaps, merges)."""
    return tools.review_vector_lake(action="list")

@mcp.tool()
def resolve_governance_item(
    item_id: str,
    resolution: str,
    payload_file: str | None = None,
) -> str:
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
def trigger_autonomous_research(dry_run: bool = True) -> str:
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
def trigger_audit_graph(dry_run: bool = True, confirmation: str = "") -> str:
    """Preview topology insights, or apply an exact confirmed audit plan."""
    return tools.audit_graph(dry_run=dry_run, confirmation=confirmation)

@mcp.tool()
def delete_source(raw_path: str, dry_run: bool = True) -> str:
    """Cascade-delete a raw source and all related wiki pages.
    
    Args:
        raw_path: Path to the raw source file to remove.
        dry_run: Preview what would be deleted without making changes.
    """
    return tools.delete_source(raw_path, dry_run=dry_run)

@mcp.tool()
def doctor_vector_lake(mode: str = "quick") -> str:
    """Run bounded quick health or an explicit deep projection diagnosis."""
    normalized_mode = str(mode or "").strip().casefold()
    if normalized_mode == "quick":
        from vector_lake.tool_doctor import quick_doctor_vector_lake

        return quick_doctor_vector_lake()
    if normalized_mode == "deep":
        return tools.doctor_vector_lake()
    raise ValueError("mode must be 'quick' or 'deep'")

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
    _require_explicit_capability(_MANUAL_INGEST_ADMIN_ENV, "manual ingest claim")
    bounded_limit = max(1, min(5, int(limit)))
    bounded_lease = max(60, min(3600, int(lease_seconds)))
    return tools.claim_ingest_tasks(
        limit=bounded_limit,
        lease_seconds=bounded_lease,
    )

@mcp.tool()
def recover_terminal_ingest_outputs(
    payload_file: str,
    dry_run: bool = True,
    confirmation: str = "",
) -> str:
    """Preview or apply one exact, fingerprint-bound retained-output recovery batch."""
    try:
        manifest = json.loads(_read_payload(payload_file))
    except json.JSONDecodeError as exc:
        raise ValueError("terminal ingest recovery payload must be valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"contract", "selections"}:
        raise ValueError("terminal ingest recovery manifest fields are not exact")
    if manifest.get("contract") != "vector-lake-terminal-ingest-output-recovery/v1":
        raise ValueError("terminal ingest recovery manifest contract is unsupported")
    selections = manifest.get("selections")
    if not isinstance(selections, list):
        raise ValueError("terminal ingest recovery selections must be a list")
    if not dry_run:
        _require_explicit_capability(
            _MANUAL_INGEST_ADMIN_ENV,
            "terminal ingest output recovery",
        )
    return tools.recover_terminal_ingest_outputs(
        selections,
        dry_run=dry_run,
        confirmation=confirmation,
    )


@mcp.tool()
def expire_ingest_tasks(max_age_seconds: int = 86400) -> str:
    """Expire stale awaiting-subagent ingest jobs so they can be retried deliberately."""
    _require_explicit_capability(_MANUAL_INGEST_ADMIN_ENV, "manual ingest expiry")
    bounded_age = max(300, min(30 * 86400, int(max_age_seconds)))
    return tools.expire_ingest_tasks(max_age_seconds=bounded_age)


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
    files_written: list | None = None,
    processed_data: dict | None = None,
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
    return tools.check_duplicate_entity(
        candidate_title,
        candidate_type,
        candidate_summary,
        register_pending=(
            str(os.environ.get(_MCP_SURFACE_ENV, "full")).strip().lower()
            != "readonly"
        ),
    )


@mcp.tool()
def auto_ingest_budget_status() -> str:
    """Report exact rolling reservations and receipt-verified token usage."""
    from vector_lake.tool_auto_ingest import auto_ingest_budget_status as status

    return json.dumps(status(), ensure_ascii=False, indent=2, sort_keys=True)


@mcp.tool()
def auto_ingest_receipt_retention(
    apply: bool = False,
    confirm_fingerprint: str = "",
    plan_as_of: str = "",
    limit: int = 256,
) -> str:
    """Preview or apply bounded retention of expired terminal attempt receipts."""
    from vector_lake.tool_auto_ingest import auto_ingest_attempt_receipt_retention

    return json.dumps(
        auto_ingest_attempt_receipt_retention(
            apply=apply,
            confirm_fingerprint=confirm_fingerprint,
            plan_as_of=plan_as_of,
            limit=limit,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

@mcp.tool()
def visualize_vector_lake(output_dir: str | None = None) -> str:
    """Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard."""
    if output_dir:
        configured_roots = os.environ.get(
            "VECTOR_LAKE_AGENT_SANDBOX_ROOTS", ""
        ).strip()
        allowed_roots = [
            Path(value).expanduser().resolve()
            for value in configured_roots.split(os.pathsep)
            if value.strip() and Path(value).expanduser().is_absolute()
        ]
        abs_dir = Path(output_dir).expanduser().resolve()
        if not any(abs_dir.is_relative_to(root) for root in allowed_roots):
            return (
                "Error: Write operations must be contained within an approved "
                "agent sandbox configured by VECTOR_LAKE_AGENT_SANDBOX_ROOTS."
            )
    return tools.visualize_vector_lake(output_dir)

@mcp.tool()
def write_wiki_page(filename: str, payload_file: str) -> str:
    """Write or update a Vector Lake wiki page safely.
    
    Args:
        filename: The filename (e.g. 'Concept_Example.md').
        payload_file: Absolute path to a temporary file containing the full markdown content including YAML frontmatter.
    """
    def receipt(
        *,
        ok: bool,
        committed: bool,
        outbox_ids: list[int] | None = None,
        deferred: list[str] | None = None,
        post_commit_warnings: list[str] | None = None,
        error_code: str | None = None,
        message: str,
    ) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "ok": ok,
                "committed": committed,
                "outbox_ids": list(outbox_ids or []),
                "deferred": list(deferred or []),
                "post_commit_warnings": list(post_commit_warnings or []),
                "error_code": error_code,
                "message": message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    normalized_filename = (
        unicodedata.normalize("NFKC", filename).casefold()
        if isinstance(filename, str)
        else ""
    )
    if (
        normalized_filename.startswith("system_")
        and os.environ.get(_SYSTEM_PAGE_WRITE_ENV) != "1"
    ):
        return receipt(
            ok=False,
            committed=False,
            error_code="system_page_write_forbidden",
            message="System wiki page writes are disabled by default.",
        )

    try:
        content = _read_payload(payload_file)
    except Exception as exc:
        logging.warning(
            "write_wiki_page payload rejected: %s",
            type(exc).__name__,
        )
        return receipt(
            ok=False,
            committed=False,
            error_code="payload_rejected",
            message="Wiki payload could not be accepted.",
        )
    from vector_lake.wiki_utils import SafeWriteError
    try:
        from vector_lake.mutation_coordinator import execute_mutation_batch

        details = execute_mutation_batch(
            [{"filename": filename, "content": content, "is_delete": False}],
            origin="mcp_write_wiki_page",
            return_details=True,
        )
        if not isinstance(details, dict):
            raise RuntimeError("Mutation coordinator did not return detail fields.")
        outbox_ids = [int(value) for value in details.get("outbox_ids", [])]
        deferred = [str(value) for value in details.get("deferred", [])]
        raw_warnings = list(details.get("post_commit_warnings", []))
        public_warnings = [
            "post_commit_follow_up_warning" for _warning in raw_warnings
        ]
        committed = bool(details.get("committed"))
        ok = bool(details.get("ok")) and committed
        error_code = None if ok else "mutation_not_committed"
        message = (
            "Canonical wiki mutation committed; "
            f"outbox={len(outbox_ids)}; deferred={len(deferred)}; "
            f"warnings={len(public_warnings)}."
            if committed
            else "Canonical wiki mutation was not committed."
        )
        return receipt(
            ok=ok,
            committed=committed,
            outbox_ids=outbox_ids,
            deferred=deferred,
            post_commit_warnings=public_warnings,
            error_code=error_code,
            message=message,
        )
    except SafeWriteError as exc:
        logging.warning("write_wiki_page rejected: %s", type(exc).__name__)
        return receipt(
            ok=False,
            committed=False,
            error_code="write_rejected",
            message="Wiki mutation was rejected.",
        )
    except ValueError as exc:
        logging.warning("write_wiki_page invalid request: %s", type(exc).__name__)
        return receipt(
            ok=False,
            committed=False,
            error_code="invalid_request",
            message="Wiki mutation request is invalid.",
        )
    except Exception as exc:
        logging.warning("write_wiki_page failed: %s", type(exc).__name__)
        return receipt(
            ok=False,
            committed=False,
            error_code="write_failed",
            message="Wiki mutation failed before commit.",
        )

@mcp.tool()
def write_wiki_batch(
    payload_file: str,
    dry_run: bool = True,
    confirmation: str = "",
) -> str:
    """Preview or atomically commit one bounded canonical Wiki batch.

    The manifest and every page payload must be in an approved agent sandbox.
    Apply requires the exact fingerprint returned by a current dry-run.
    Markdown projections are post-commit and may be deferred for repair.
    """
    from vector_lake.tool_wiki_batch import (
        SchemaMaintenanceNotAuthorized,
        SystemPageWriteNotAuthorized,
        run_wiki_batch,
        schema_maintenance_allowlist_from_env,
    )

    try:
        manifest_text = _read_payload(payload_file)
        allowed_maintenance = schema_maintenance_allowlist_from_env(
            os.environ.get(_WIKI_BATCH_SCHEMA_MAINTENANCE_ENV)
        )
        result = run_wiki_batch(
            manifest_text,
            _read_payload,
            dry_run=dry_run,
            confirmation=confirmation,
            allow_system_pages=os.environ.get(_SYSTEM_PAGE_WRITE_ENV) == "1",
            allowed_schema_maintenance_filenames=allowed_maintenance,
        )
        return json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except SystemPageWriteNotAuthorized as exc:
        logging.warning("write_wiki_batch system write rejected: %s", type(exc).__name__)
        error_code = "system_page_write_forbidden"
        message = "System wiki page writes are disabled by default."
    except SchemaMaintenanceNotAuthorized as exc:
        logging.warning(
            "write_wiki_batch schema maintenance rejected: %s",
            type(exc).__name__,
        )
        error_code = "schema_maintenance_forbidden"
        message = "Wiki batch schema maintenance was not authorized by the host."
    except ValueError as exc:
        logging.warning("write_wiki_batch invalid request: %s", type(exc).__name__)
        error_code = "invalid_request"
        message = "Wiki batch request is invalid."
    except Exception as exc:
        logging.warning("write_wiki_batch failed: %s", type(exc).__name__)
        error_code = "write_failed"
        message = "Wiki batch failed before commit."
    return json.dumps(
        {
            "schema_version": 1,
            "ok": False,
            "dry_run": bool(dry_run),
            "committed": False,
            "operation_count": 0,
            "aggregate_payload_bytes": 0,
            "schema_maintenance_count": 0,
            "confirmation_required": True,
            "fingerprint": "",
            "operations": [],
            "outbox_ids": [],
            "deferred": [],
            "post_commit_warnings": [],
            "error_code": error_code,
            "message": message,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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
    from vector_lake.runtime_health import enforce_runtime_write_health

    enforce_runtime_write_health(validation_mode="full")
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


def configure_mcp_surface(
    server: FastMCP,
    surface: str | None = None,
) -> tuple[str, ...]:
    """Apply an exact fail-closed MCP surface before transport startup."""
    normalized = str(
        surface if surface is not None else os.environ.get(_MCP_SURFACE_ENV, "full")
    ).strip().lower()
    available = {
        tool.name for tool in server._tool_manager.list_tools()
    }
    if normalized == "full":
        target_tools = available
    else:
        target_tools = _MCP_SURFACE_ALLOWLISTS.get(normalized)
    if target_tools is None:
        raise RuntimeError(
            f"Unsupported {_MCP_SURFACE_ENV} value: {normalized!r}; "
            "expected 'full', 'memory', or 'readonly'"
        )
    missing = sorted(target_tools - available)
    if missing:
        raise RuntimeError(
            f"{normalized.capitalize()} MCP surface is incomplete; missing tools: "
            + ", ".join(missing)
        )
    for name in sorted(available - target_tools):
        server.remove_tool(name)
    remaining = {
        tool.name for tool in server._tool_manager.list_tools()
    }
    if remaining != target_tools:
        raise RuntimeError(
            f"{normalized.capitalize()} MCP surface filtering did not close exactly"
        )
    effective_tools = tuple(sorted(remaining))
    # Keep the database layer and wrapper behavior aligned for embedded servers,
    # not only for processes whose host pre-populated the environment.
    os.environ[_MCP_SURFACE_ENV] = normalized
    setattr(server, "_vector_lake_configured_surface", normalized)
    setattr(server, "_vector_lake_effective_surface", normalized)
    setattr(server, "_vector_lake_effective_tools", effective_tools)
    return effective_tools


def main() -> None:
    from vector_lake.runtime_paths import bootstrap_runtime_paths

    bootstrap_runtime_paths(caller="MCP server")
    configure_mcp_surface(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
