import asyncio
import gc
import logging
import os
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import threading
import time
import weakref

import anyio
import pytest

from vector_lake import governance_store, mcp_server, mutation_coordinator
from vector_lake.tool_search import (
    SearchIndexError,
    format_operational_memory_results,
    search_vector_lake,
)
from vector_lake.tool_ingest import get_ingest_target_directories
from vector_lake.index_snapshot import clear_index_snapshot_cache_for_tests
from vector_lake.watchdog_app import _watch_directories
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_meta_dir,
    get_outbox_signal_path,
    get_raw_dir,
    get_wiki_dir,
)


@pytest.mark.parametrize("configured_workers", [None, "invalid"])
def test_mcp_blocking_executor_uses_memory_safe_default(
    tmp_path,
    monkeypatch,
    configured_workers,
):
    if configured_workers is None:
        monkeypatch.delenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", raising=False)
    else:
        monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", configured_workers)
    monkeypatch.delenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", raising=False)
    server = mcp_server.ReloadAwareFastMCP(
        "memory-safe-default-test",
        runtime_guard=mcp_server.MCPRuntimeGuard(
            tmp_path,
            check_interval_seconds=60,
        ),
    )
    try:
        status = server.blocking_executor_status()
        assert status["workers"] == 1
        assert status["queue_capacity"] == 1
    finally:
        server.shutdown_blocking_executor(wait=True)


def test_operational_memory_xml_is_a_well_formed_document(isolated_memory, monkeypatch):
    monkeypatch.setattr(
        governance_store,
        "search_operational_memory",
        lambda *args, **kwargs: [
            {
                "memory_type": "fact'quoted",
                "validity_state": "active&review",
                "retrieval_score": 1.0,
                "text": "A < B & C > D",
                "source_page": "Source_'A&B'.md",
            },
            {
                "memory_type": "decision",
                "validity_state": "active",
                "retrieval_score": 0.5,
                "text": "Second item",
                "source_page": "Source_Second.md",
            },
        ],
    )

    payload = format_operational_memory_results("query", as_xml=True)
    root = ET.fromstring(payload)

    assert root.tag == "MemoryResults"
    assert len(root.findall("Memory_Item")) == 2
    assert root.findall("Memory_Item")[0].text == "A < B & C > D"


def test_signal_and_watch_paths_follow_active_memory_root(isolated_memory):
    assert get_outbox_signal_path() == get_meta_dir() / "outbox_signal.lock"
    assert _watch_directories() == {
        "wiki": get_wiki_dir(),
        "raw": get_raw_dir(),
        "diary": get_raw_dir() / "privacy" / "Diary",
        "raw_targets": get_ingest_target_directories(collapse_nested=True),
    }

    mutation_coordinator._signal_outbox_consumer()

    assert get_outbox_signal_path().read_text(encoding="utf-8") == "1"


def test_meta_dir_cache_is_keyed_by_active_memory_root(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(first_root))
    first_meta = get_meta_dir()
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(second_root))
    second_meta = get_meta_dir()

    assert first_meta == Path(first_root).resolve() / "wiki" / ".meta"
    assert second_meta == Path(second_root).resolve() / "wiki" / ".meta"
    assert second_meta != first_meta


def test_explicit_meta_dir_override_is_stable_and_cache_keyed(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    memory_root = tmp_path / "memory"
    first_meta = tmp_path / "first-meta"
    second_meta = tmp_path / "second-meta"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_root))
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(first_meta))

    assert get_meta_dir() == first_meta.resolve()

    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(second_meta))
    assert get_meta_dir() == second_meta.resolve()
    assert second_meta.is_dir()


def test_explicit_meta_dir_override_fails_closed_when_unwritable(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    blocked = tmp_path / "blocked"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(blocked))
    monkeypatch.setattr(
        wiki_utils,
        "_verify_writable_meta_dir",
        lambda _candidate: (_ for _ in ()).throw(PermissionError("blocked")),
    )

    with pytest.raises(RuntimeError, match="VECTOR_LAKE_META_DIR is not writable"):
        get_meta_dir()


def test_meta_dir_reuses_existing_fallback_before_empty_primary(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    memory_root = tmp_path / "memory"
    primary = memory_root / "wiki" / ".meta"
    fallback = tmp_path / "data" / "v8_meta"
    fallback.mkdir(parents=True)
    (fallback / "vector_lake.db").write_bytes(b"legacy canonical state")
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_root))
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    monkeypatch.setattr(wiki_utils, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(wiki_utils, "_uses_legacy_default_memory_root", lambda: True)
    verified = []

    def verify(candidate):
        verified.append(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    monkeypatch.setattr(wiki_utils, "_verify_writable_meta_dir", verify)

    assert get_meta_dir() == fallback
    assert verified == [fallback]
    assert primary.exists() is False


def test_primary_meta_override_refuses_to_strand_existing_fallback(
    tmp_path, monkeypatch
):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    memory_root = tmp_path / "memory"
    primary = memory_root / "wiki" / ".meta"
    fallback = tmp_path / "data" / "v8_meta"
    fallback.mkdir(parents=True)
    (fallback / "vector_lake.db").write_bytes(b"legacy canonical state")
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_root))
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(primary))
    monkeypatch.setattr(wiki_utils, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(wiki_utils, "_uses_legacy_default_memory_root", lambda: True)

    with pytest.raises(RuntimeError, match="Refusing to create a second"):
        get_meta_dir()
    assert primary.exists() is False


def test_meta_dir_refuses_split_brain_fallback_when_primary_db_exists(
    tmp_path, monkeypatch
):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    memory_root = tmp_path / "memory"
    primary = memory_root / "wiki" / ".meta"
    primary.mkdir(parents=True)
    (primary / "vector_lake.db").write_bytes(b"existing canonical state")
    fallback = tmp_path / "data" / "v8_meta"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_root))
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_ALLOW_META_FALLBACK", raising=False)
    monkeypatch.setattr(wiki_utils, "get_extension_root", lambda: tmp_path)

    def verify(candidate):
        if candidate == primary:
            raise PermissionError("primary is read-only")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    monkeypatch.setattr(wiki_utils, "_verify_writable_meta_dir", verify)

    with pytest.raises(RuntimeError, match="Refusing to select a different database"):
        get_meta_dir()
    assert fallback.exists() is False


def test_existing_meta_fallback_requires_explicit_opt_in(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    memory_root = tmp_path / "memory"
    primary = memory_root / "wiki" / ".meta"
    primary.mkdir(parents=True)
    (primary / "vector_lake.db").write_bytes(b"existing canonical state")
    expected_fallback = tmp_path / "data" / "v8_meta"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory_root))
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    monkeypatch.setenv("VECTOR_LAKE_ALLOW_META_FALLBACK", "1")
    monkeypatch.setattr(wiki_utils, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(wiki_utils, "_uses_legacy_default_memory_root", lambda: True)

    def verify(candidate):
        if candidate == primary:
            raise PermissionError("primary is read-only")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    monkeypatch.setattr(wiki_utils, "_verify_writable_meta_dir", verify)

    assert get_meta_dir() == expected_fallback


def test_custom_memory_roots_never_share_extension_global_fallback(
    tmp_path, monkeypatch
):
    from vector_lake import wiki_utils

    fallback = tmp_path / "data" / "v8_meta"
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    monkeypatch.setattr(wiki_utils, "get_extension_root", lambda: tmp_path)

    def verify(candidate):
        if candidate == fallback:
            raise AssertionError("custom roots must not reach the global fallback")
        raise PermissionError("custom primary is read-only")

    monkeypatch.setattr(wiki_utils, "_verify_writable_meta_dir", verify)
    for root_name in ("tenant-a", "tenant-b"):
        wiki_utils._META_DIR_CACHE = None
        monkeypatch.setenv(
            "VECTOR_LAKE_MEMORY_DIR",
            str(tmp_path / root_name),
        )
        with pytest.raises(RuntimeError, match="extension-global fallback"):
            get_meta_dir()

    assert fallback.exists() is False


def test_claim_graph_uses_documented_canonical_filename(isolated_memory):
    assert get_claim_graph_path() == get_wiki_dir() / "claim_graph.json"


def test_mcp_runtime_guard_detects_source_revision_drift(tmp_path):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=0,
    )

    initial = guard.status(force=True)
    source_path.write_text("VALUE = 2\n", encoding="utf-8")
    changed = guard.status(force=True)

    assert initial["stale"] is False
    assert changed["stale"] is True
    assert changed["loaded_revision"] != changed["current_revision"]
    with pytest.raises(RuntimeError, match="restart"):
        guard.assert_current()


def test_mcp_server_uses_reload_aware_dispatch():
    assert isinstance(mcp_server.mcp, mcp_server.ReloadAwareFastMCP)
    status = mcp_server.mcp_runtime_status()

    assert '"restart_required": false' in status


def test_reload_aware_dispatch_rejects_stale_source_tree(tmp_path):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=0,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "runtime-test",
        runtime_guard=guard,
    )
    source_path.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="restart"):
        anyio.run(server.call_tool, "any_tool", {})


def test_mcp_runtime_status_remains_callable_after_source_revision_drift(tmp_path):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "stale-runtime-status-test",
        runtime_guard=guard,
    )

    @server.tool()
    def mcp_runtime_status() -> str:
        return mcp_server.json.dumps(guard.status(force=True))

    source_path.write_text("VALUE = 2\n", encoding="utf-8")
    try:
        result = anyio.run(server.call_tool, "mcp_runtime_status", {})
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    unstructured = result[0] if isinstance(result, tuple) else result
    status = mcp_server.json.loads(unstructured[0].text)
    assert status["stale"] is True
    assert status["restart_required"] is True


def test_mcp_sync_tools_run_off_the_event_loop(tmp_path):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP("threaded-tool-test", runtime_guard=guard)
    started = threading.Event()
    release = threading.Event()

    @server.tool()
    def blocking_probe() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "ok"

    registered = server._tool_manager.get_tool("blocking_probe")
    assert registered is not None and registered.is_async is True

    async def scenario():
        results = []

        async def invoke():
            results.append(await server.call_tool("blocking_probe", {}))

        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            with anyio.fail_after(0.5):
                while not started.is_set():
                    await anyio.sleep(0.005)
            release.set()
        return results

    try:
        results = anyio.run(scenario)
    finally:
        release.set()
        server._blocking_executor.shutdown(wait=True, cancel_futures=True)

    assert results


def test_mcp_heavy_tool_busy_releases_executor_capacity(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    from vector_lake.heavy_task_gate import HeavyTaskBusy, heavy_task

    monkeypatch.setenv("VECTOR_LAKE_MCP_HEAVY_TASK_WAIT_SECONDS", "0.05")
    monkeypatch.setitem(
        mcp_server._MCP_HEAVY_TASKS,
        "heavy_gate_probe",
        ("scan", 60.0),
    )
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "heavy-gate-test",
        runtime_guard=guard,
    )
    tool_ran = threading.Event()

    @server.tool()
    def heavy_gate_probe() -> str:
        tool_ran.set()
        return "ok"

    registered = server._tool_manager.get_tool("heavy_gate_probe")
    assert registered is not None
    try:
        with heavy_task(
            "maintenance",
            "external-holder",
            origin="pytest",
            wait_timeout_seconds=0,
        ):
            with pytest.raises(HeavyTaskBusy):
                anyio.run(registered.fn)
        assert server.blocking_executor_status()["inflight"] == 0
        assert tool_ran.is_set() is False

        result = anyio.run(registered.fn)
        assert result == "ok"
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)


def test_all_known_mcp_rescan_entrypoints_are_heavy_task_gated():
    expected = {
        "doctor_vector_lake": ("scan", 900.0),
        "get_governance_debt": ("scan", 900.0),
        "lint_vector_lake": ("scan", 1800.0),
        "merge_suggestions_vector_lake": ("scan", 1800.0),
        "orphan_source_classify": ("scan", 900.0),
        "prepare_ingest_batch": ("ingest_scan", 1800.0),
        "projection_report": ("scan", 900.0),
        "reconcile_ingest_tasks": ("maintenance", 1800.0),
        "reconcile_orphan_ingest_packets": ("maintenance", 900.0),
        "sync_vector_lake": ("ingest_scan", 1800.0),
        "trigger_audit_graph": ("scan", 1800.0),
        "trigger_autonomous_research": ("ingest_scan", 1800.0),
        "visualize_vector_lake": ("scan", 900.0),
    }

    assert {name: mcp_server._MCP_HEAVY_TASKS[name] for name in expected} == expected
    for name in expected:
        registered = mcp_server.mcp._tool_manager.get_tool(name)
        assert registered is not None
        assert registered.is_async is True


def test_queued_sync_tool_rechecks_source_revision_at_execution(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "queued-stale-test",
        runtime_guard=guard,
    )
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    tool_ran = threading.Event()
    blocker = server._submit_blocking_call(
        lambda: blocker_started.set() or release_blocker.wait(timeout=2)
    )
    assert blocker_started.wait(timeout=2)

    @server.tool()
    def queued_probe() -> str:
        tool_ran.set()
        return "must-not-run"

    registered = server._tool_manager.get_tool("queued_probe")

    async def scenario():
        assert registered is not None
        task = asyncio.create_task(registered.fn())
        with anyio.fail_after(2):
            while server._blocking_executor.queued_work_items() < 1:
                await anyio.sleep(0.005)
        source_path.write_text("VALUE = 2\n", encoding="utf-8")
        release_blocker.set()
        with pytest.raises(RuntimeError, match="restart"):
            await task

    try:
        anyio.run(scenario)
        blocker.result(timeout=2)
    finally:
        release_blocker.set()
        server.shutdown_blocking_executor(wait=True)

    assert tool_ran.is_set() is False


def test_mcp_async_admission_wait_keeps_event_loop_responsive(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "0")
    monkeypatch.setenv("VECTOR_LAKE_MCP_ADMISSION_TIMEOUT_SECONDS", "0.5")
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "async-admission-test",
        runtime_guard=guard,
    )
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = server._submit_blocking_call(
        lambda: blocker_started.set() or release_blocker.wait(timeout=2)
    )
    assert blocker_started.wait(timeout=2)

    async def scenario():
        started_at = time.monotonic()
        waiter = asyncio.create_task(server._run_blocking_call(lambda: None))
        await asyncio.sleep(0.02)
        heartbeat_at = time.monotonic() - started_at
        with pytest.raises(RuntimeError, match="saturated"):
            await waiter
        rejected_at = time.monotonic() - started_at
        return heartbeat_at, rejected_at

    try:
        heartbeat_at, rejected_at = anyio.run(scenario)
    finally:
        release_blocker.set()
        blocker.result(timeout=2)
        server.shutdown_blocking_executor(wait=True)

    assert heartbeat_at < 0.2
    assert rejected_at >= 0.4


def test_mcp_worker_reuses_clean_connection_and_closes_open_transaction(
    tmp_path,
    monkeypatch,
):
    from vector_lake import db_store

    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(tmp_path / "worker-reuse.db"))
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    db_store.init_db()
    db_store.close_connection()
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "worker-connection-test",
        runtime_guard=guard,
    )
    observed_connections = []

    @server.tool()
    def clean_db_probe() -> str:
        conn = db_store.get_connection()
        observed_connections.append(conn)
        conn.execute("SELECT 1").fetchone()
        return "ok"

    @server.tool()
    def open_transaction_probe() -> str:
        conn = db_store.get_connection()
        observed_connections.append(conn)
        conn.execute("BEGIN")
        return "ok"

    async def scenario():
        await server.call_tool("clean_db_probe", {})
        await server.call_tool("clean_db_probe", {})
        await server.call_tool("open_transaction_probe", {})

    try:
        anyio.run(scenario)
    finally:
        server._blocking_executor.shutdown(wait=True, cancel_futures=True)

    assert observed_connections[0] is observed_connections[1]
    assert observed_connections[2] is observed_connections[1]
    assert id(observed_connections[2]) not in db_store._CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        observed_connections[2].execute("SELECT 1")


def test_mcp_async_tool_cleanup_covers_success_and_failure(tmp_path, monkeypatch):
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "async-tool-cleanup-test",
        runtime_guard=guard,
    )
    cleanup_calls = []
    monkeypatch.setattr(
        server,
        "_cleanup_tool_connection",
        lambda *, failed: cleanup_calls.append(failed),
    )

    @server.tool()
    async def successful_async_probe() -> str:
        return "ok"

    @server.tool()
    async def failing_async_probe() -> str:
        raise RuntimeError("injected async failure")

    successful = server._tool_manager.get_tool("successful_async_probe")
    failing = server._tool_manager.get_tool("failing_async_probe")

    async def scenario():
        assert successful is not None
        assert failing is not None
        assert await successful.fn() == "ok"
        with pytest.raises(RuntimeError, match="injected async failure"):
            await failing.fn()

    try:
        anyio.run(scenario)
    finally:
        server.shutdown_blocking_executor(wait=True)

    assert cleanup_calls == [False, True]


def test_mcp_async_tool_cancellation_runs_failure_cleanup(tmp_path, monkeypatch):
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "async-cancellation-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    cleanup_calls = []
    monkeypatch.setattr(
        server,
        "_cleanup_tool_connection",
        lambda *, failed: cleanup_calls.append(failed),
    )

    @server.tool()
    async def cancellable_async_probe() -> str:
        started.set()
        await asyncio.Future()
        return "unreachable"

    registered = server._tool_manager.get_tool("cancellable_async_probe")

    async def scenario():
        assert registered is not None
        task = asyncio.create_task(registered.fn())
        with anyio.fail_after(2):
            while not started.is_set():
                await anyio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        anyio.run(scenario)
    finally:
        server.shutdown_blocking_executor(wait=True)

    assert cleanup_calls == [True]


def test_mcp_cancelled_blocking_waiter_still_cleans_worker(tmp_path, monkeypatch):
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "blocking-cancellation-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    cleanup_calls = []

    def cleanup(*, failed):
        cleanup_calls.append(failed)
        cleaned.set()

    monkeypatch.setattr(
        mcp_server.ReloadAwareFastMCP,
        "_cleanup_tool_connection",
        staticmethod(cleanup),
    )

    @server.tool()
    def cancellable_blocking_probe() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "ok"

    registered = server._tool_manager.get_tool("cancellable_blocking_probe")

    async def scenario():
        assert registered is not None
        task = asyncio.create_task(registered.fn())
        with anyio.fail_after(2):
            while not started.is_set():
                await anyio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        with anyio.fail_after(2):
            while not cleaned.is_set():
                await anyio.sleep(0.005)

    try:
        anyio.run(scenario)
    finally:
        release.set()
        server.shutdown_blocking_executor(wait=True)

    assert cleanup_calls == [False]


def test_mcp_concurrent_shutdown_fences_new_submissions(tmp_path):
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "concurrent-shutdown-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    release = threading.Event()
    future = server._submit_blocking_call(
        lambda: started.set() or release.wait(timeout=2)
    )
    assert started.wait(timeout=2)
    shutdown_threads = [
        threading.Thread(
            target=server.shutdown_blocking_executor,
            kwargs={"wait": True},
        )
        for _ in range(4)
    ]
    for thread in shutdown_threads:
        thread.start()
    deadline = time.monotonic() + 2
    while not server._executor_shutdown_started and time.monotonic() < deadline:
        time.sleep(0.005)

    with pytest.raises(RuntimeError, match="shutting down"):
        server._submit_blocking_call(lambda: None)

    release.set()
    assert future.result(timeout=2) is True
    for thread in shutdown_threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert server._executor_finalizer.alive is False


def test_mcp_executor_shutdown_is_idempotent_and_stops_workers(tmp_path):
    guard = mcp_server.MCPRuntimeGuard(
        tmp_path,
        check_interval_seconds=60,
    )
    server = mcp_server.ReloadAwareFastMCP(
        "executor-shutdown-test",
        runtime_guard=guard,
    )
    worker_name = server._blocking_executor.submit_tracked(
        lambda: threading.current_thread().name,
        lambda: None,
    ).result(timeout=2)

    assert all(worker.daemon for worker in server._blocking_executor._threads)

    assert worker_name.startswith("vector-lake-mcp")
    server.shutdown_blocking_executor(wait=False)
    server.shutdown_blocking_executor(wait=True)

    assert server._executor_finalizer.alive is False
    assert all(not worker.is_alive() for worker in server._blocking_executor._threads)
    with pytest.raises(RuntimeError, match="cannot schedule new futures"):
        server._blocking_executor.submit_tracked(lambda: None, lambda: None)


def test_mcp_normal_transport_exit_waits_for_running_blocking_call(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_SHUTDOWN_TIMEOUT_SECONDS", "2")
    transport_ended = threading.Event()

    def end_transport(self, transport="stdio", mount_path=None):
        transport_ended.set()
        return "transport-ended"

    monkeypatch.setattr(mcp_server.FastMCP, "run", end_transport)
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "graceful-exit-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    release = threading.Event()
    future = server._submit_blocking_call(
        lambda: started.set() or release.wait(timeout=2)
    )
    assert started.wait(timeout=2)
    result = []
    runner = threading.Thread(target=lambda: result.append(server.run()))
    runner.start()

    assert transport_ended.wait(timeout=2)
    assert runner.is_alive()
    release.set()
    runner.join(timeout=2)

    assert not runner.is_alive()
    assert result == ["transport-ended"]
    assert future.result(timeout=2) is True
    status = server.blocking_executor_status()
    assert status["shutdown_started"] is True
    assert status["shutdown_completed"] is True
    assert status["shutdown_timed_out"] is False
    assert status["shutdown_timeout_seconds"] == 2.0


def test_mcp_transport_exit_timeout_never_blocks_indefinitely(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_SHUTDOWN_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    monkeypatch.setattr(
        mcp_server.FastMCP,
        "run",
        lambda self, transport="stdio", mount_path=None: "transport-ended",
    )
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "bounded-exit-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    release = threading.Event()
    future = server._submit_blocking_call(
        lambda: started.set() or release.wait(timeout=2)
    )
    assert started.wait(timeout=2)
    queued = server._submit_blocking_call(lambda: "must-not-run")

    started_at = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert server.run() == "transport-ended"
    elapsed = time.monotonic() - started_at

    try:
        assert elapsed < 1
        status = server.blocking_executor_status()
        assert status["shutdown_started"] is True
        assert status["shutdown_completed"] is False
        assert status["shutdown_timed_out"] is True
        assert status["queued_items"] == 0
        assert queued.cancelled() is True
        assert "did not drain" in caplog.text
    finally:
        release.set()
        assert future.result(timeout=2) is True
        deadline = time.monotonic() + 2
        final_status = server.blocking_executor_status()
        while not final_status["shutdown_completed"] and time.monotonic() < deadline:
            time.sleep(0.005)
            final_status = server.blocking_executor_status()
        assert final_status["shutdown_completed"] is True
        assert final_status["shutdown_timed_out"] is True
        server.shutdown_blocking_executor(wait=True, timeout=2)


def test_daemon_executor_cancel_callback_runs_outside_state_lock():
    executor = mcp_server._DaemonThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="lock-order-test",
    )
    started = threading.Event()
    release = threading.Event()
    callback_finished = threading.Event()
    snapshots = []
    running = executor.submit_tracked(
        lambda: started.set() or release.wait(timeout=2),
        lambda: None,
    )
    assert started.wait(timeout=2)
    queued = executor.submit_tracked(
        lambda: "must-not-run",
        lambda: (
            snapshots.append(executor.status_snapshot()),
            callback_finished.set(),
        ),
    )

    shutdown_thread = threading.Thread(
        target=lambda: executor.shutdown(wait=False, cancel_futures=True)
    )
    shutdown_thread.start()
    try:
        assert callback_finished.wait(timeout=2)
        shutdown_thread.join(timeout=2)
        assert not shutdown_thread.is_alive()
        assert queued.cancelled() is True
        assert snapshots[0]["queued_items"] == 0
    finally:
        release.set()
        shutdown_thread.join(timeout=2)
        assert running.result(timeout=2) is True
        executor.shutdown(wait=True, timeout=2)


def test_mcp_status_snapshots_executor_before_server_lock(tmp_path, monkeypatch):
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "status-lock-order-test",
        runtime_guard=guard,
    )
    original_snapshot = server._blocking_executor.status_snapshot
    server_lock_was_free = []

    def inspected_snapshot():
        acquired = server._executor_shutdown_lock.acquire(blocking=False)
        server_lock_was_free.append(acquired)
        if acquired:
            server._executor_shutdown_lock.release()
        return original_snapshot()

    monkeypatch.setattr(
        server._blocking_executor,
        "status_snapshot",
        inspected_snapshot,
    )
    try:
        status = server.blocking_executor_status()
        assert status["queued_items"] == 0
        assert server_lock_was_free == [True]
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)


@pytest.mark.parametrize("invalid_timeout", [-1, float("nan"), "invalid"])
def test_mcp_invalid_manual_shutdown_timeout_does_not_poison_server(
    tmp_path,
    invalid_timeout,
):
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "invalid-shutdown-timeout-test",
        runtime_guard=guard,
    )

    try:
        with pytest.raises(ValueError, match="finite non-negative"):
            server.shutdown_blocking_executor(
                wait=True,
                timeout=invalid_timeout,
            )
        status = server.blocking_executor_status()
        assert status["shutdown_started"] is False
        assert (
            server._submit_blocking_call(lambda: "still-running").result(timeout=2)
            == "still-running"
        )
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)


@pytest.mark.parametrize(
    ("configured_timeout", "expected_timeout"),
    (("-10", 0.1), ("999", 30.0)),
)
def test_mcp_shutdown_timeout_configuration_is_bounded(
    tmp_path,
    monkeypatch,
    configured_timeout,
    expected_timeout,
):
    monkeypatch.setenv(
        "VECTOR_LAKE_MCP_SHUTDOWN_TIMEOUT_SECONDS",
        configured_timeout,
    )
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "shutdown-timeout-bound-test",
        runtime_guard=guard,
    )

    try:
        status = server.blocking_executor_status()
        assert status["shutdown_timeout_seconds"] == expected_timeout
        assert status["shutdown_completed"] is False
        assert status["shutdown_timed_out"] is False
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)


def test_mcp_running_worker_does_not_delay_process_exit(tmp_path):
    child_memory = tmp_path / "child-memory"
    child_meta = tmp_path / "child-meta"
    child_ready = tmp_path / "child-worker-ready"
    script = "\n".join(
        (
            "import os",
            "import threading",
            "from pathlib import Path",
            "from vector_lake.mcp_server import MCPRuntimeGuard, ReloadAwareFastMCP",
            "server = ReloadAwareFastMCP(",
            "    'exit-test',",
            "    runtime_guard=MCPRuntimeGuard(Path.cwd(), check_interval_seconds=60),",
            ")",
            "started = threading.Event()",
            "server._submit_blocking_call(",
            "    lambda: started.set() or threading.Event().wait(timeout=60)",
            ")",
            "assert started.wait(timeout=2)",
            "Path(os.environ['VECTOR_LAKE_EXIT_READY_FILE']).write_text(",
            "    'ready',",
            "    encoding='utf-8',",
            ")",
        )
    )
    env = os.environ.copy()
    env["VECTOR_LAKE_MEMORY_DIR"] = str(child_memory)
    env["VECTOR_LAKE_META_DIR"] = str(child_meta)
    env["VECTOR_LAKE_DB_PATH"] = str(child_meta / "vector_lake.db")
    env["VECTOR_LAKE_EXIT_READY_FILE"] = str(child_ready)
    env.pop("GEMINI_API_KEY", None)

    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Windows process/import startup can be delayed by antivirus and memory
        # pressure in the full suite. The contract under test starts only after
        # the ready marker; its five-second process-exit bound remains strict.
        startup_timeout_seconds = 45
        startup_deadline = time.monotonic() + startup_timeout_seconds
        while not child_ready.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                pytest.fail(
                    "MCP exit probe stopped before its worker started: "
                    f"returncode={process.returncode}, stdout={stdout!r}, "
                    f"stderr={stderr!r}"
                )
            if time.monotonic() >= startup_deadline:
                pytest.fail(
                    "MCP exit probe did not start its worker within "
                    f"{startup_timeout_seconds} seconds"
                )
            time.sleep(0.02)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "An active daemon MCP worker delayed process exit beyond 5 seconds"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, (
        f"returncode={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
    )


def test_mcp_blocking_executor_rejects_unbounded_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_ADMISSION_TIMEOUT_SECONDS", "0")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "bounded-executor-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    release = threading.Event()

    first = server._submit_blocking_call(
        lambda: started.set() or release.wait(timeout=2)
    )
    assert started.wait(timeout=2)
    second = server._submit_blocking_call(lambda: "queued")

    try:
        status = server.blocking_executor_status()
        assert status["workers"] == 1
        assert status["queue_capacity"] == 1
        assert status["inflight"] == 2
        with pytest.raises(RuntimeError, match="saturated"):
            server._submit_blocking_call(lambda: "unbounded")
        release.set()
        assert first.result(timeout=2) is True
        assert second.result(timeout=2) == "queued"
    finally:
        release.set()
        server.shutdown_blocking_executor(wait=True)

    assert server.blocking_executor_status()["inflight"] == 0


def test_mcp_cancelled_queue_cannot_bypass_physical_admission_bound(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_ADMISSION_TIMEOUT_SECONDS", "0")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "cancelled-queue-test",
        runtime_guard=guard,
    )
    started = threading.Event()
    release = threading.Event()

    first = server._submit_blocking_call(
        lambda: started.set() or release.wait(timeout=2)
    )
    assert started.wait(timeout=2)
    cancelled = server._submit_blocking_call(lambda: "cancelled")
    assert cancelled.cancel() is True

    for _attempt in range(100):
        with pytest.raises(RuntimeError, match="saturated"):
            server._submit_blocking_call(lambda: "must-not-queue")
    status = server.blocking_executor_status()
    assert status["inflight"] == 2
    assert status["queued_items"] == 1

    release.set()
    try:
        assert first.result(timeout=2) is True
        deadline = time.monotonic() + 2
        while (
            server.blocking_executor_status()["inflight"] != 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert server.blocking_executor_status()["inflight"] == 0
        replacement = server._submit_blocking_call(lambda: "replacement")
        assert replacement.result(timeout=2) == "replacement"
    finally:
        release.set()
        server.shutdown_blocking_executor(wait=True)

    final_status = server.blocking_executor_status()
    assert final_status["inflight"] == 0
    assert final_status["queued_items"] == 0


def test_mcp_idle_worker_releases_payload_and_abandoned_server(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "idle-release-test",
        runtime_guard=guard,
    )

    class Payload:
        pass

    payload = Payload()
    payload_ref = weakref.ref(payload)
    future = server._submit_blocking_call(lambda value=payload: value)
    assert future.result(timeout=2) is payload
    workers = list(server._blocking_executor._threads)
    server_ref = weakref.ref(server)

    del future, payload
    deadline = time.monotonic() + 2
    while payload_ref() is not None and time.monotonic() < deadline:
        gc.collect()
        time.sleep(0.01)
    assert payload_ref() is None

    del server
    deadline = time.monotonic() + 2
    while server_ref() is not None and time.monotonic() < deadline:
        gc.collect()
        time.sleep(0.01)
    assert server_ref() is None
    deadline = time.monotonic() + 2
    while any(worker.is_alive() for worker in workers) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(worker.is_alive() for worker in workers)


def test_mcp_runtime_status_exposes_blocking_executor_capacity():
    status = mcp_server.json.loads(mcp_server.mcp_runtime_status())

    assert status["blocking_executor"]["workers"] >= 1
    assert status["blocking_executor"]["queue_capacity"] >= 0
    assert status["blocking_executor"]["queued_items"] >= 0
    assert status["blocking_executor"]["workers_daemon"] is True
    assert status["blocking_executor"]["running_workers"] >= 0
    assert status["heavy_task_gate"]["physical_state"] in {"free", "locked"}


def test_mcp_worker_closes_connection_after_tool_failure(tmp_path, monkeypatch):
    from vector_lake import db_store

    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(tmp_path / "worker-failure.db"))
    db_store.init_db()
    db_store.close_connection()
    observed_connections = []

    def fail_after_opening_connection():
        observed_connections.append(db_store.get_connection())
        raise RuntimeError("injected tool failure")

    with pytest.raises(RuntimeError, match="injected tool failure"):
        mcp_server.ReloadAwareFastMCP._invoke_blocking_tool(
            fail_after_opening_connection
        )

    assert id(observed_connections[0]) not in db_store._CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        observed_connections[0].execute("SELECT 1")


def test_corrupt_search_index_raises_typed_runtime_error(isolated_memory):
    index_path = get_wiki_dir() / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SearchIndexError, match="could not be read"):
        search_vector_lake("test")


def test_projection_consumers_fail_closed_on_uncommitted_index(isolated_memory):
    import json

    from vector_lake import (
        db_store,
        indexer,
        runtime_health,
        tool_doctor,
        tool_graph,
        tool_ingest,
        tool_piea,
        tool_projection,
        tool_research,
    )

    db_store.init_db()
    index_path = get_wiki_dir() / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "nodes": {},
                "graph_insights": [
                    {"type": "isolated_node", "node": "Concept_Uncommitted"}
                ],
            }
        ),
        encoding="utf-8",
    )

    assert tool_research.research_vector_lake().startswith(
        "Error: committed projection is not ready"
    )
    assert tool_graph.audit_graph().startswith(
        "Error: committed graph projection is not ready"
    )
    duplicate = json.loads(
        tool_piea.check_duplicate_entity("Uncommitted", "concept")
    )
    assert duplicate["status"] == "not_ready"
    assert duplicate["is_duplicate"] is None
    assert duplicate["error"] == "projection_not_committed"
    with pytest.raises(indexer.ProjectionPairContractError):
        tool_projection.embedding_backfill_projection(dry_run=True)
    with pytest.raises(indexer.ProjectionPairContractError):
        tool_ingest._prepare_relevant_index_context()

    health = runtime_health.assess_runtime_health()
    assert health["ok"] is False
    assert health["detail"]["projection_pair"] == "invalid"
    assert any(
        issue.startswith("projection_pair_invalid:")
        for issue in health["issues"]
    )
    doctor = tool_doctor.doctor_vector_lake()
    projection_line = next(
        line for line in doctor.splitlines() if "Projection Pair:" in line
    )
    assert projection_line.startswith("[FAIL]")


def test_dirty_graph_is_excluded_from_search_expansion(isolated_memory, monkeypatch):
    import json
    from vector_lake import index_snapshot, tool_search

    wiki_dir = get_wiki_dir()
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "Concept_Seed.md").write_text("# Seed", encoding="utf-8")
    (wiki_dir / "Concept_Expanded.md").write_text("# Expanded", encoding="utf-8")
    index_path = wiki_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "nodes": {
                    "Concept_Seed": {
                        "title": "Seed",
                        "type": "concept",
                        "status": "Active",
                    },
                    "Concept_Expanded": {
                        "title": "Expanded",
                        "type": "concept",
                        "status": "Active",
                    },
                },
                "weighted_edges": [
                    {
                        "source": "Concept_Seed",
                        "target": "Concept_Expanded",
                        "weight": 5.0,
                    }
                ],
                "graph_state": {"dirty": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *args, **kwargs: [{"node_key": "Concept_Seed", "rank": -1.0}],
    )
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda *_: None)
    monkeypatch.setattr(tool_search, "_expand_query_locally", lambda *_: ["seed"])
    monkeypatch.setattr(
        tool_search,
        "read_committed_index_snapshot",
        index_snapshot.load_index_snapshot,
    )
    clear_index_snapshot_cache_for_tests()

    dirty_result = tool_search.search_vector_lake("seed", top_k=2)

    assert "Seed" in dirty_result
    assert "Expanded" not in dirty_result


def test_assemble_context_reuses_search_index_snapshot(isolated_memory, monkeypatch):
    import json

    from vector_lake import index_snapshot, tool_search

    wiki_dir = get_wiki_dir()
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "Concept_Seed.md").write_text("# Seed", encoding="utf-8")
    index_path = wiki_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "nodes": {
                    "Concept_Seed": {
                        "title": "Seed",
                        "type": "concept",
                        "status": "Active",
                    }
                },
                "weighted_edges": [],
                "graph_state": {"dirty": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_search,
        "build_memory_packet",
        lambda *_args, **_kwargs: {
            "packet": "",
            "memory_count": 0,
            "warning_count": 0,
            "omitted_count": 0,
        },
    )
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *args, **kwargs: [{"node_key": "Concept_Seed", "rank": -1.0}],
    )
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda *_: None)
    monkeypatch.setattr(tool_search, "_expand_query_locally", lambda *_: ["seed"])
    monkeypatch.setattr(
        tool_search,
        "read_committed_index_snapshot",
        index_snapshot.load_index_snapshot,
    )
    clear_index_snapshot_cache_for_tests()

    real_decode = index_snapshot._decode_index_snapshot
    index_parses = []

    def tracking_decode(path):
        index_parses.append(1)
        return real_decode(path)

    monkeypatch.setattr(index_snapshot, "_decode_index_snapshot", tracking_decode)

    context = tool_search.assemble_context("seed", max_chars=12000)

    assert context["index_summary"] == "[concept] Seed"
    assert len(index_parses) == 1


def test_search_and_runtime_health_share_index_snapshot(isolated_memory):
    from vector_lake import db_store, indexer, runtime_health, tool_search

    index_path = get_wiki_dir() / "index.json"
    db_store.init_db()
    indexer.generate_index()
    clear_index_snapshot_cache_for_tests()

    search_snapshot = tool_search._load_search_index(index_path)
    health_snapshot, error = runtime_health._index_snapshot(index_path)

    assert error is None
    assert health_snapshot is search_snapshot

    indexer.generate_index()
    refreshed_snapshot = tool_search._load_search_index(index_path)

    assert refreshed_snapshot is not search_snapshot
    assert (
        refreshed_snapshot["projection_manifest"]["generation"]
        != search_snapshot["projection_manifest"]["generation"]
    )


def test_search_discards_backend_scores_when_projection_generation_changes(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_search

    index_path = get_wiki_dir() / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    (get_wiki_dir() / "Concept_New.md").write_text("# New", encoding="utf-8")
    old_snapshot = {
        "projection_manifest": {"generation": "old"},
        "nodes": {
            "Concept_Old": {
                "title": "Old",
                "type": "concept",
                "status": "Active",
            }
        },
        "weighted_edges": [],
    }
    new_snapshot = {
        "projection_manifest": {"generation": "new"},
        "nodes": {
            "Concept_New": {
                "title": "New seed",
                "summary": "seed",
                "type": "concept",
                "status": "Active",
            }
        },
        "weighted_edges": [],
    }
    snapshots = iter((old_snapshot, new_snapshot))
    monkeypatch.setattr(tool_search, "_load_search_index", lambda *_: next(snapshots))
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: [{"node_key": "Concept_Old", "rank": -100.0}],
    )
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda *_: None)
    monkeypatch.setattr(tool_search, "_expand_query_locally", lambda *_: ["seed"])

    result = tool_search.search_vector_lake("seed", top_k=2)

    assert "New seed" in result
    assert "**Old**" not in result
    assert "projection_generation_changed" in result
