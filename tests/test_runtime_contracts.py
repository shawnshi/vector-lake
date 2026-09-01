import asyncio
import gc
import functools
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
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
from tests.test_mutation_coordinator import (
    _source_content,
    _write_purpose_contract,
)


def test_merge_public_defaults_remain_explicit_and_unchanged():
    from vector_lake import cli_app, tool_merge

    cli_args = cli_app.build_parser().parse_args(["merge-suggestions"])
    mcp_enqueue = inspect.signature(
        mcp_server.merge_suggestions_vector_lake
    ).parameters["enqueue"]
    tool_enqueue = inspect.signature(
        tool_merge.merge_suggestions_vector_lake
    ).parameters["enqueue"]

    assert cli_args.limit == 20
    assert cli_args.preview is False
    assert cli_args.apply is False
    assert mcp_enqueue.default is False
    assert tool_enqueue.default is True


@pytest.mark.parametrize("configured_workers", [None, "invalid"])
def test_mcp_blocking_executor_uses_bounded_parallel_default(
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
        assert status["workers"] == 2
        assert status["queue_capacity"] == 4
        assert status["heavy_lane"]["workers"] == 1
        assert status["heavy_lane"]["queue_capacity"] == 1
    finally:
        server.shutdown_blocking_executor(wait=True)


def test_mcp_heavy_lane_does_not_block_fast_read_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "0")
    monkeypatch.setenv("VECTOR_LAKE_MCP_HEAVY_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_HEAVY_QUEUE_CAPACITY", "0")
    server = mcp_server.ReloadAwareFastMCP(
        "split-lane-test",
        runtime_guard=mcp_server.MCPRuntimeGuard(
            tmp_path,
            check_interval_seconds=60,
        ),
    )
    heavy_started = threading.Event()
    heavy_release = threading.Event()
    heavy = server._submit_heavy_call(
        lambda: heavy_started.set() or heavy_release.wait(timeout=2)
    )
    assert heavy_started.wait(timeout=2)

    try:
        fast = server._submit_blocking_call(lambda: "fast-result")
        assert fast.result(timeout=1) == "fast-result"
        status = server.blocking_executor_status()
        assert status["heavy_lane"]["inflight"] == 1
        assert status["fast_lane"]["inflight"] == 0
    finally:
        heavy_release.set()
        assert heavy.result(timeout=2) is True
        server.shutdown_blocking_executor(wait=True)
        shutdown_status = server.blocking_executor_status()
        assert shutdown_status["fast_lane"]["shutdown_completed"] is True
        assert shutdown_status["heavy_lane"]["shutdown_completed"] is True
        assert shutdown_status["shutdown_completed"] is True


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


def test_write_wiki_page_returns_committed_mutation_receipt(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    monkeypatch.setattr(mcp_server, "_read_payload", lambda _path: _source_content())

    raw_receipt = mcp_server.write_wiki_page(
        "Source_Public-Receipt.md",
        "C:/approved/scratch/payload.md",
    )
    receipt = json.loads(raw_receipt)

    assert set(receipt) == {
        "schema_version",
        "ok",
        "committed",
        "outbox_ids",
        "deferred",
        "post_commit_warnings",
        "error_code",
        "message",
    }
    assert receipt["schema_version"] == 1
    assert receipt["ok"] is True
    assert receipt["committed"] is True
    assert len(receipt["outbox_ids"]) == 1
    assert receipt["deferred"] == []
    assert receipt["post_commit_warnings"] == []
    assert receipt["error_code"] is None
    row = mutation_coordinator.db_store.get_connection().execute(
        "SELECT id, status FROM mutation_outbox WHERE filename = ?",
        ("Source_Public-Receipt.md",),
    ).fetchone()
    assert dict(row) == {
        "id": receipt["outbox_ids"][0],
        "status": "pending",
    }


def test_write_wiki_page_receipt_preserves_outcome_without_leaking_paths(
    monkeypatch,
):
    monkeypatch.setattr(mcp_server, "_read_payload", lambda _path: "payload")
    monkeypatch.setattr(
        mutation_coordinator,
        "execute_mutation_batch",
        lambda *_args, **_kwargs: {
            "ok": True,
            "committed": True,
            "outbox_ids": [17],
            "deferred": ["Source_Deferred.md"],
            "post_commit_warnings": [
                "projection failed at C:/private/operator/wiki/Source_Deferred.md"
            ],
        },
    )

    raw_receipt = mcp_server.write_wiki_page(
        "Source_Deferred.md",
        "C:/approved/scratch/payload.md",
    )
    receipt = json.loads(raw_receipt)

    assert receipt == {
        "schema_version": 1,
        "ok": True,
        "committed": True,
        "outbox_ids": [17],
        "deferred": ["Source_Deferred.md"],
        "post_commit_warnings": ["post_commit_follow_up_warning"],
        "error_code": None,
        "message": (
            "Canonical wiki mutation committed; outbox=1; deferred=1; warnings=1."
        ),
    }
    assert "C:/private" not in raw_receipt


@pytest.mark.parametrize(
    ("phase", "error_code"),
    [
        ("payload", "payload_rejected"),
        ("mutation", "write_failed"),
    ],
)
def test_write_wiki_page_failure_receipt_is_stable_and_sanitized(
    monkeypatch,
    phase,
    error_code,
):
    leaked = "C:/private/operator/secret.md traceback sentinel"
    if phase == "payload":
        monkeypatch.setattr(
            mcp_server,
            "_read_payload",
            lambda _path: (_ for _ in ()).throw(ValueError(leaked)),
        )
    else:
        monkeypatch.setattr(mcp_server, "_read_payload", lambda _path: "payload")
        monkeypatch.setattr(
            mutation_coordinator,
            "execute_mutation_batch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(leaked)),
        )

    raw_receipt = mcp_server.write_wiki_page(
        "Source_Failed.md",
        "C:/approved/scratch/payload.md",
    )
    receipt = json.loads(raw_receipt)

    assert receipt["ok"] is False
    assert receipt["committed"] is False
    assert receipt["outbox_ids"] == []
    assert receipt["deferred"] == []
    assert receipt["post_commit_warnings"] == []
    assert receipt["error_code"] == error_code
    assert "C:/private" not in raw_receipt
    assert "traceback sentinel" not in raw_receipt


@pytest.mark.parametrize(
    "filename",
    [
        "System_Community-L1-deadbeef.md",
        "system_Community-L1-deadbeef.md",
        "SYSTEM_Community-L1-deadbeef.md",
        "Ｓｙｓｔｅｍ＿Community-L1-deadbeef.md",
        "Syſtem_Community-L1-deadbeef.md",
    ],
)
def test_public_write_wiki_page_rejects_system_identity_before_any_write(
    monkeypatch,
    filename,
):
    monkeypatch.delenv("VECTOR_LAKE_ALLOW_SYSTEM_PAGE_WRITE", raising=False)
    observed = []

    def forbidden(*_args, **_kwargs):
        observed.append("called")
        raise AssertionError("System write gate was bypassed")

    monkeypatch.setattr(mcp_server, "_read_payload", forbidden)
    monkeypatch.setattr(mutation_coordinator, "execute_mutation_batch", forbidden)

    receipt = json.loads(
        mcp_server.write_wiki_page(filename, "C:/approved/scratch/payload.md")
    )

    assert receipt == {
        "schema_version": 1,
        "ok": False,
        "committed": False,
        "outbox_ids": [],
        "deferred": [],
        "post_commit_warnings": [],
        "error_code": "system_page_write_forbidden",
        "message": "System wiki page writes are disabled by default.",
    }
    assert observed == []


def test_operator_capability_allows_system_page_to_enter_existing_write_path(
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_ALLOW_SYSTEM_PAGE_WRITE", "1")
    monkeypatch.setattr(mcp_server, "_read_payload", lambda _path: "payload")
    captured = []

    def commit(mutations, **kwargs):
        captured.append((mutations, kwargs))
        return {
            "ok": True,
            "committed": True,
            "outbox_ids": [23],
            "deferred": [],
            "post_commit_warnings": [],
        }

    monkeypatch.setattr(mutation_coordinator, "execute_mutation_batch", commit)

    receipt = json.loads(
        mcp_server.write_wiki_page(
            "System_Community-L1-deadbeef.md",
            "C:/approved/scratch/payload.md",
        )
    )

    assert receipt["ok"] is True
    assert receipt["committed"] is True
    assert captured == [
        (
            [
                {
                    "filename": "System_Community-L1-deadbeef.md",
                    "content": "payload",
                    "is_delete": False,
                }
            ],
            {"origin": "mcp_write_wiki_page", "return_details": True},
        )
    ]


def test_meta_dir_cache_is_keyed_by_active_memory_root(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
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


def test_mcp_source_tree_revision_preserves_legacy_digest_contract(tmp_path):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_bytes(b"VALUE = 1\n")
    config_path = tmp_path / "config.json"
    config_path.write_bytes(b"{}\n")
    template_path = tmp_path / "templates" / "query_prompt.md"
    template_path.parent.mkdir()
    template_path.write_bytes(b"prompt\n")
    expected = hashlib.sha256()
    for relative_path, path in sorted(
        (
            ("config.json", config_path),
            ("templates/query_prompt.md", template_path),
            ("vector_lake/runtime_probe.py", source_path),
        )
    ):
        expected.update(relative_path.encode("utf-8"))
        expected.update(b"\x00")
        expected.update(path.read_bytes())
        expected.update(b"\x00")

    assert mcp_server._source_tree_revision(package_root) == expected.hexdigest()


@pytest.mark.parametrize(
    ("relative_path", "initial", "changed"),
    [
        ("config.json", "{}\n", '{"changed": true}\n'),
        ("templates/query_prompt.md", "before\n", "after\n"),
        ("runtime_profiles.json", "{}\n", '{"schema_version": 1}\n'),
    ],
)
def test_mcp_runtime_guard_detects_restart_sensitive_asset_drift(
    tmp_path,
    relative_path,
    initial,
    changed,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    asset_path = tmp_path / relative_path
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_text(initial, encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(package_root, check_interval_seconds=0)

    asset_path.write_text(changed, encoding="utf-8")

    with pytest.raises(RuntimeError, match="restart"):
        guard.assert_current()


def test_revision_paths_partition_runtime_from_host_adapters(tmp_path):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime_profiles.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text("{}\n", encoding="utf-8")
    skill_path = tmp_path / "skills" / "query" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("skill\n", encoding="utf-8")

    runtime_paths = {
        relative for relative, _path in mcp_server._runtime_revision_paths(package_root)
    }
    host_paths = {
        relative
        for relative, _path in mcp_server._host_adapter_revision_paths(package_root)
    }

    assert "vector_lake/runtime_probe.py" in runtime_paths
    assert "runtime_profiles.json" in runtime_paths
    assert ".mcp.json" not in runtime_paths
    assert "skills/query/SKILL.md" not in runtime_paths
    assert ".mcp.json" in host_paths
    assert "skills/query/SKILL.md" in host_paths
    assert runtime_paths.isdisjoint(host_paths)


def test_host_adapter_drift_requires_host_reload_not_mcp_restart(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    skill_path = tmp_path / "skills" / "query" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("before\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(package_root, check_interval_seconds=0)

    skill_path.write_text("after\n", encoding="utf-8")

    runtime_status = guard.status(force=True)
    assert runtime_status["stale"] is False
    assert (
        mcp_server._host_adapter_revision(package_root)
        != guard.loaded_host_adapter_revision
    )

    monkeypatch.setattr(mcp_server.mcp, "runtime_guard", guard)
    reported = json.loads(mcp_server.mcp_runtime_status())
    assert reported["runtime_revision"]["stale"] is False
    assert reported["runtime_revision"]["mcp_restart_required"] is False
    assert reported["host_adapter_revision"]["changed_since_start"] is True
    assert reported["host_adapter_revision"]["mcp_restart_required"] is False
    assert reported["host_adapter_revision"]["host_reload_required"] is True


def test_mcp_call_tool_uses_cached_admission_and_execution_checks(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(package_root, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP("revision-count-test", runtime_guard=guard)
    original_status = guard.status
    real_refresh = mcp_server._refresh_runtime_revision_inventory
    force_values = []
    refresh_calls = 0

    def observed_status(*, force=False):
        force_values.append(force)
        return original_status(force=force)

    def counted_refresh(inventory):
        nonlocal refresh_calls
        refresh_calls += 1
        return real_refresh(inventory)

    monkeypatch.setattr(guard, "status", observed_status)
    monkeypatch.setattr(
        mcp_server,
        "_refresh_runtime_revision_inventory",
        counted_refresh,
    )
    guard._last_checked -= 61

    @server.tool()
    def revision_probe() -> str:
        return "ok"

    try:
        anyio.run(server.call_tool, "revision_probe", {})
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert force_values == [False, False]
    assert refresh_calls == 1


def test_mcp_runtime_guard_ttl_path_does_not_scan_or_hash(tmp_path, monkeypatch):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
    )

    def forbidden_probe(*_args, **_kwargs):
        raise AssertionError("cached guard performed filesystem work")

    monkeypatch.setattr(
        mcp_server,
        "_refresh_runtime_revision_inventory",
        forbidden_probe,
    )
    monkeypatch.setattr(mcp_server, "_source_tree_revision", forbidden_probe)

    guard.assert_current()
    status = guard.status()

    assert status["served_from_cache"] is True
    assert status["metadata_checks"] == 1
    assert status["full_hashes"] == 1
    assert status["cached_checks"] == 2


def test_mcp_runtime_guard_due_metadata_probe_does_not_use_path_rglob(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    nested_root = package_root / "nested"
    nested_root.mkdir(parents=True)
    (nested_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=0,
        full_hash_interval_seconds=3600,
    )

    def forbidden_rglob(*_args, **_kwargs):
        raise AssertionError("metadata probe performed a recursive glob")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)

    status = guard.status()

    assert status["stale"] is False
    assert status["last_check_kind"] == "metadata"
    assert status["metadata_checks"] == 2
    assert status["inventory_rebuilds"] == 1


def test_mcp_runtime_guard_rebuilds_inventory_only_after_directory_token_change(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=0,
        full_hash_interval_seconds=3600,
    )
    real_build = mcp_server._build_runtime_revision_inventory
    build_calls = 0

    def counted_build(source_root):
        nonlocal build_calls
        build_calls += 1
        return real_build(source_root)

    monkeypatch.setattr(
        mcp_server,
        "_build_runtime_revision_inventory",
        counted_build,
    )

    unchanged = guard.status()
    (package_root / "added.py").write_text("ADDED = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="restart"):
        guard.assert_current()

    assert unchanged["last_check_kind"] == "metadata"
    assert build_calls == 1
    assert guard.status()["inventory_rebuilds"] == 2


def test_mcp_runtime_guard_inventory_enforces_file_limit(tmp_path, monkeypatch):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (package_root / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_RUNTIME_REVISION_MAX_FILES", 1)

    with pytest.raises(RuntimeError, match="file limit"):
        mcp_server.MCPRuntimeGuard(package_root)


def test_mcp_runtime_guard_inventory_enforces_directory_limit(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    (package_root / "nested").mkdir(parents=True)
    (package_root / "nested" / "probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_server, "_RUNTIME_REVISION_MAX_DIRECTORIES", 1)

    with pytest.raises(RuntimeError, match="directory limit"):
        mcp_server.MCPRuntimeGuard(package_root)


@pytest.mark.parametrize("operation", ["add", "modify", "delete"])
def test_mcp_runtime_guard_metadata_change_triggers_full_hash(
    tmp_path,
    operation,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=0,
        full_hash_interval_seconds=3600,
    )

    if operation == "add":
        (package_root / "added.py").write_text("ADDED = 1\n", encoding="utf-8")
    elif operation == "modify":
        source_path.write_text("VALUE = 200\n", encoding="utf-8")
    else:
        source_path.unlink()

    with pytest.raises(RuntimeError, match="restart"):
        guard.assert_current()

    status = guard.status()
    assert status["stale"] is True
    assert status["last_check_kind"] in {"metadata", "metadata_changed_full"}
    assert status["full_hashes"] == 2


def test_mcp_runtime_guard_periodic_hash_detects_metadata_preserving_tamper(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=0,
        full_hash_interval_seconds=60,
    )
    original_inventory = guard._revision_inventory
    source_path.write_text("VALUE = 200\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp_server,
        "_refresh_runtime_revision_inventory",
        lambda _inventory: (original_inventory, False),
    )
    guard._last_full_hash_monotonic -= 61

    status = guard.status()

    assert status["stale"] is True
    assert status["last_check_kind"] == "periodic_full"
    assert status["loaded_revision"] != status["current_revision"]


def test_mcp_runtime_guard_force_is_exact_even_with_cached_metadata(
    tmp_path,
    monkeypatch,
):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    source_path = package_root / "runtime_probe.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
        full_hash_interval_seconds=3600,
    )
    original_inventory = guard._revision_inventory
    source_path.write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp_server,
        "_refresh_runtime_revision_inventory",
        lambda _inventory: (original_inventory, False),
    )

    cached = guard.status()
    exact = guard.status(force=True)

    assert cached["stale"] is False
    assert cached["served_from_cache"] is True
    assert exact["stale"] is True
    assert exact["last_check_kind"] == "forced_full"
    assert exact["loaded_revision"] != exact["current_revision"]


def test_mcp_runtime_guard_refresh_is_single_flight(tmp_path, monkeypatch):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
        full_hash_interval_seconds=60,
    )
    guard._last_checked -= 61
    guard._last_full_hash_monotonic -= 61
    real_revision = mcp_server._source_tree_revision
    hash_started = threading.Event()
    release_hash = threading.Event()
    hash_calls = 0
    hash_lock = threading.Lock()

    def slow_revision(source_root):
        nonlocal hash_calls
        with hash_lock:
            hash_calls += 1
        hash_started.set()
        assert release_hash.wait(timeout=2)
        return real_revision(source_root)

    monkeypatch.setattr(mcp_server, "_source_tree_revision", slow_revision)
    start = threading.Barrier(8)
    statuses = []

    def inspect_status():
        start.wait(timeout=2)
        statuses.append(guard.status())

    threads = [threading.Thread(target=inspect_status) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert hash_started.wait(timeout=2)
    time.sleep(0.05)
    release_hash.set()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert hash_calls == 1
    assert len(statuses) == 8
    assert all(status["stale"] is False for status in statuses)
    assert guard.status()["singleflight_waits"] >= 1


def test_mcp_runtime_guard_strict_env_forces_each_check(tmp_path, monkeypatch):
    package_root = tmp_path / "vector_lake"
    package_root.mkdir()
    (package_root / "runtime_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VECTOR_LAKE_MCP_REVISION_STRICT", "1")
    guard = mcp_server.MCPRuntimeGuard(
        package_root,
        check_interval_seconds=60,
        full_hash_interval_seconds=3600,
    )
    real_revision = mcp_server._source_tree_revision
    hash_calls = 0

    def counted_revision(source_root):
        nonlocal hash_calls
        hash_calls += 1
        return real_revision(source_root)

    monkeypatch.setattr(mcp_server, "_source_tree_revision", counted_revision)

    guard.assert_current()
    guard.assert_current()

    assert hash_calls == 2
    assert guard.strict is True
    assert guard._last_check_kind == "strict_full"


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
    source_path.write_text("VALUE = 200\n", encoding="utf-8")

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


def test_mcp_runtime_status_bypasses_saturated_heavy_tool_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "0")
    monkeypatch.setenv("VECTOR_LAKE_MCP_ADMISSION_TIMEOUT_SECONDS", "0.05")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP("status-lane-test", runtime_guard=guard)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = server._submit_blocking_call(
        lambda: blocker_started.set() or release_blocker.wait(timeout=2)
    )
    assert blocker_started.wait(timeout=2)

    @server.tool()
    def mcp_runtime_status() -> str:
        return mcp_server.json.dumps(guard.status(force=True))

    started_at = time.monotonic()
    try:
        result = anyio.run(server.call_tool, "mcp_runtime_status", {})
        elapsed = time.monotonic() - started_at
    finally:
        release_blocker.set()
        blocker.result(timeout=2)
        server.shutdown_blocking_executor(wait=True, timeout=2)

    unstructured = result[0] if isinstance(result, tuple) else result
    status = mcp_server.json.loads(unstructured[0].text)
    assert status["stale"] is False
    assert elapsed < 0.5


def test_mcp_runtime_status_hashing_runs_off_the_event_loop(tmp_path, monkeypatch):
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "status-event-loop-test",
        runtime_guard=guard,
    )
    release = threading.Event()
    original_status = guard.status

    def slow_status(force=False):
        assert release.wait(timeout=2)
        return original_status(force=force)

    monkeypatch.setattr(guard, "status", slow_status)

    @server.tool()
    def mcp_runtime_status() -> str:
        return mcp_server.json.dumps(guard.status(force=True))

    async def scenario():
        results = []

        async def invoke():
            results.append(await server.call_tool("mcp_runtime_status", {}))

        timer = threading.Timer(0.25, release.set)
        timer.start()
        started_at = time.monotonic()
        heartbeat_elapsed = float("inf")
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(invoke)
                await anyio.sleep(0.05)
                heartbeat_elapsed = time.monotonic() - started_at
        finally:
            timer.cancel()
            release.set()
        return heartbeat_elapsed, results

    try:
        heartbeat_elapsed, results = anyio.run(scenario)
    finally:
        release.set()
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert heartbeat_elapsed < 0.15
    assert results


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


def test_mcp_doctor_defaults_to_quick_and_deep_is_explicit(monkeypatch):
    from vector_lake import tool_doctor

    monkeypatch.setattr(
        tool_doctor,
        "quick_doctor_vector_lake",
        lambda: "quick-report",
    )
    monkeypatch.setattr(
        mcp_server.tools,
        "doctor_vector_lake",
        lambda: "deep-report",
    )

    assert mcp_server.doctor_vector_lake() == "quick-report"
    assert mcp_server.doctor_vector_lake(mode="QUICK") == "quick-report"
    assert mcp_server.doctor_vector_lake(mode="deep") == "deep-report"
    with pytest.raises(ValueError, match="quick.*deep"):
        mcp_server.doctor_vector_lake(mode="full")


def test_mcp_quick_doctor_bypasses_heavy_gate_but_deep_does_not(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    from vector_lake.heavy_task_gate import HeavyTaskBusy, heavy_task

    monkeypatch.setenv("VECTOR_LAKE_MCP_HEAVY_TASK_WAIT_SECONDS", "0.05")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "doctor-mode-gate-test",
        runtime_guard=guard,
    )

    @server.tool()
    def doctor_vector_lake(mode: str = "quick") -> str:
        return mode

    registered = server._tool_manager.get_tool("doctor_vector_lake")
    assert registered is not None
    try:
        with heavy_task(
            "maintenance",
            "external-holder",
            origin="pytest",
            wait_timeout_seconds=0,
        ):
            assert anyio.run(registered.fn) == "quick"
            with pytest.raises(HeavyTaskBusy):
                anyio.run(functools.partial(registered.fn, mode="deep"))
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)


def test_quick_doctor_marks_semantic_readiness_unchecked(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_doctor

    observed: dict[str, object] = {}

    def shallow_health(**kwargs):
        observed.update(kwargs)
        return {"ok": True, "issues": [], "warnings": [], "detail": {}}

    monkeypatch.setattr(tool_doctor, "assess_runtime_health", shallow_health)

    report = json.loads(tool_doctor.quick_doctor_vector_lake())

    assert observed == {"deep_projection_checks": False}
    assert report["mode"] == "quick"
    assert report["ok"] is True
    assert report["semantic_readiness"] == {
        "status": "not_checked",
        "reason": "requires_deep_doctor",
    }
    assert report["paths"]["memory"] == str(isolated_memory)


def test_all_known_mcp_rescan_entrypoints_are_heavy_task_gated():
    expected = {
        "doctor_vector_lake": ("scan", 900.0),
        "finalize_query_synthesis": ("projection", 900.0),
        "get_governance_debt": ("scan", 900.0),
        "lint_vector_lake": ("scan", 1800.0),
        "merge_suggestions_vector_lake": ("scan", 1800.0),
        "orphan_source_classify": ("scan", 900.0),
        "prepare_ingest_batch": ("ingest_scan", 1800.0),
        "projection_report": ("scan", 900.0),
        "propose_schema_mutation": ("maintenance", 900.0),
        "reconcile_ingest_tasks": ("maintenance", 1800.0),
        "reconcile_orphan_ingest_packets": ("maintenance", 900.0),
        "sync_vector_lake": ("ingest_scan", 1800.0),
        "sync_critical_decision_registry": ("maintenance", 900.0),
        "trigger_audit_graph": ("scan", 1800.0),
        "trigger_autonomous_research": ("ingest_scan", 1800.0),
        "visualize_vector_lake": ("scan", 900.0),
    }

    assert {name: mcp_server._MCP_HEAVY_TASKS[name] for name in expected} == expected
    for name in expected:
        registered = mcp_server.mcp._tool_manager.get_tool(name)
        assert registered is not None
        assert registered.is_async is True


def test_manual_ingest_admin_endpoints_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_ALLOW_MANUAL_INGEST_ADMIN", raising=False)
    with pytest.raises(PermissionError, match="disabled by default"):
        mcp_server.claim_ingest_tasks(limit=999, lease_seconds=999999)
    with pytest.raises(PermissionError, match="disabled by default"):
        mcp_server.expire_ingest_tasks(max_age_seconds=1)


def test_manual_ingest_admin_endpoints_apply_hard_bounds(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_ALLOW_MANUAL_INGEST_ADMIN", "1")
    observed = {}

    def claim(*, limit, lease_seconds):
        observed["claim"] = (limit, lease_seconds)
        return "[]"

    def expire(*, max_age_seconds):
        observed["expire"] = max_age_seconds
        return "ok"

    monkeypatch.setattr(mcp_server.tools, "claim_ingest_tasks", claim)
    monkeypatch.setattr(mcp_server.tools, "expire_ingest_tasks", expire)
    assert mcp_server.claim_ingest_tasks(limit=999, lease_seconds=999999) == "[]"
    assert mcp_server.expire_ingest_tasks(max_age_seconds=1) == "ok"
    assert observed == {"claim": (5, 3600), "expire": 300}


def test_audit_graph_defaults_to_stable_read_only_preview(monkeypatch):
    from vector_lake import tool_graph

    monkeypatch.setattr(tool_graph.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        tool_graph,
        "read_committed_index_snapshot",
        lambda path: {
            "graph_insights": [
                {
                    "type": "isolated_node",
                    "node": "Concept_Preview",
                    "description": "preview only",
                }
            ]
        },
    )

    def forbidden_write(*args, **kwargs):
        raise AssertionError("audit preview attempted a governance write")

    monkeypatch.setattr(
        tool_graph.governance_store,
        "insert_governance_item_if_absent",
        forbidden_write,
    )
    first = tool_graph.audit_graph()
    second = tool_graph.audit_graph()
    assert first == second
    assert first.startswith("Audit preview: 1 topology insight")


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
        check_interval_seconds=0,
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


def test_mcp_cancelled_queued_tool_records_zero_execution(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "queued-cooperative-cancel-test",
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
    def queued_cancel_probe() -> str:
        tool_ran.set()
        return "must-not-run"

    registered = server._tool_manager.get_tool("queued_cancel_probe")

    async def scenario():
        assert registered is not None
        task = asyncio.create_task(registered.fn())
        with anyio.fail_after(2):
            while server._blocking_executor.queued_work_items() < 1:
                await anyio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        anyio.run(scenario)
        snapshot = server.cancellation_status()
        operation = snapshot["operations"][0]
        release_blocker.set()
        blocker.result(timeout=2)
    finally:
        release_blocker.set()
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert tool_ran.is_set() is False
    assert operation["tool_name"] == "queued_cancel_probe"
    assert operation["status"] == "cancelled"
    assert operation["started_at"] is None
    assert operation["detached"] is False


def test_mcp_queued_deadline_expires_without_tool_execution(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "queued-deadline-test",
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
    def queued_deadline_probe() -> str:
        tool_ran.set()
        return "must-not-run"

    async def scenario():
        with pytest.raises(Exception, match="deadline exceeded") as rejected:
            await server.call_tool(
                "queued_deadline_probe",
                {mcp_server._MCP_CALL_DEADLINE_ARGUMENT: 0.05},
            )
        return str(rejected.value)

    try:
        rejection = anyio.run(scenario)
        operation_id = re.search(r"operation_id=([a-z0-9_-]+)", rejection).group(1)
        operation = server.cancellation_status(operation_id)["operation"]
        release_blocker.set()
        blocker.result(timeout=2)
    finally:
        release_blocker.set()
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert tool_ran.is_set() is False
    assert operation["status"] == "cancelled"
    assert operation["cancellation_reason"] == "deadline_exceeded"
    assert operation["started_at"] is None


def test_mcp_configured_deadline_is_a_non_bypassable_hard_cap(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_TOOL_DEADLINE_SECONDS", "0.25")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "configured-deadline-cap-test",
        runtime_guard=guard,
    )
    try:
        assert server._effective_call_deadline(None) == 0.25
        assert server._effective_call_deadline(0) == 0.25
        assert server._effective_call_deadline(10) == 0.25
        assert server._effective_call_deadline(0.05) == 0.05
        with pytest.raises(ValueError, match="finite non-negative"):
            server._effective_call_deadline(-1)
        with pytest.raises(ValueError, match="hard limit"):
            server._effective_call_deadline(3601)
        status = server.cancellation_status()
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert status["default_deadline_seconds"] == 0.25
    assert status["deadline_max_seconds"] == 3600.0
    assert status["deadline_argument"] == mcp_server._MCP_CALL_DEADLINE_ARGUMENT


@pytest.mark.parametrize("configured", ["invalid", "-1", "nan", "3601"])
def test_mcp_invalid_configured_deadline_fails_closed(
    tmp_path,
    monkeypatch,
    configured,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_TOOL_DEADLINE_SECONDS", configured)
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)

    with pytest.raises(ValueError, match="deadline"):
        mcp_server.ReloadAwareFastMCP(
            "invalid-configured-deadline-test",
            runtime_guard=guard,
        )


def test_readonly_heavy_scan_bypasses_canonical_meta_file_gate(
    tmp_path,
    monkeypatch,
):
    guard_root = tmp_path / "source"
    guard_root.mkdir()
    meta_root = tmp_path / "meta"
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(meta_root))
    guard = mcp_server.MCPRuntimeGuard(guard_root, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "readonly-physical-zero-write-test",
        runtime_guard=guard,
    )
    setattr(server, "_vector_lake_effective_surface", "readonly")

    @server.tool()
    def doctor_vector_lake() -> str:
        return "ok"

    def forbidden_gate(*_args, **_kwargs):
        raise AssertionError("readonly scan must not acquire the shared file gate")

    monkeypatch.setattr("vector_lake.heavy_task_gate.heavy_task", forbidden_gate)
    try:
        result = anyio.run(server.call_tool, "doctor_vector_lake", {})
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert result[0][0].text == "ok"
    assert not meta_root.exists()


def test_mcp_running_scan_honors_deadline_at_next_checkpoint(
    tmp_path,
    monkeypatch,
):
    from vector_lake.cancellation import (
        cancellation_checkpoint,
        current_operation_id,
    )

    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "running-deadline-test",
        runtime_guard=guard,
    )
    processed = []
    stopped = threading.Event()

    @server.tool()
    def cooperative_scan_probe() -> str:
        operation_id = current_operation_id()
        try:
            for item in range(100):
                cancellation_checkpoint(f"scan_batch:{item}")
                processed.append(item)
                time.sleep(0.01)
        finally:
            stopped.set()
        return operation_id

    async def scenario():
        started_at = time.monotonic()
        with pytest.raises(Exception, match="deadline exceeded") as rejected:
            await server.call_tool(
                "cooperative_scan_probe",
                {mcp_server._MCP_CALL_DEADLINE_ARGUMENT: 0.06},
            )
        return str(rejected.value), time.monotonic() - started_at

    try:
        rejection, response_elapsed = anyio.run(scenario)
        operation_id = re.search(r"operation_id=([a-z0-9_-]+)", rejection).group(1)
        assert stopped.wait(timeout=0.25)
        operation = server.cancellation_status(operation_id)["operation"]
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert response_elapsed < 0.2
    assert len(processed) <= 10
    assert operation["status"] == "cancelled"
    assert operation["detached"] is True
    assert operation["checkpoints"] >= 2
    assert operation["cancellation_reason"] == "deadline_exceeded"


def test_mcp_running_scan_honors_client_cancel_at_next_checkpoint(
    tmp_path,
    monkeypatch,
):
    from vector_lake.cancellation import cancellation_checkpoint

    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "running-client-cancel-test",
        runtime_guard=guard,
    )
    first_batch = threading.Event()
    stopped = threading.Event()
    processed = []

    @server.tool()
    def cancellable_batch_scan_probe() -> str:
        try:
            for item in range(100):
                cancellation_checkpoint(f"batch:{item}")
                processed.append(item)
                first_batch.set()
                time.sleep(0.01)
        finally:
            stopped.set()
        return "complete"

    registered = server._tool_manager.get_tool("cancellable_batch_scan_probe")

    async def scenario():
        assert registered is not None
        task = asyncio.create_task(registered.fn())
        with anyio.fail_after(1):
            while not first_batch.is_set():
                await anyio.sleep(0.005)
        processed_at_cancel = len(processed)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return processed_at_cancel

    try:
        processed_at_cancel = anyio.run(scenario)
        assert stopped.wait(timeout=0.25)
        operation = server.cancellation_status()["operations"][0]
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert len(processed) <= processed_at_cancel + 1
    assert operation["status"] == "cancelled"
    assert operation["detached"] is True
    assert operation["cancellation_reason"] == "client_cancelled"


def test_atomic_phase_entry_is_linearized_against_cancellation(monkeypatch):
    from vector_lake.cancellation import CancellationOperation

    operation = CancellationOperation(
        tool_name="atomic-entry-race-probe",
        lane="write",
        deadline=None,
    )
    operation.mark_running()
    checkpoint_completed = threading.Event()
    allow_atomic_transition = threading.Event()
    cancellation_started = threading.Event()
    cancellation_returned = threading.Event()
    errors = []
    original_checkpoint = operation.checkpoint

    def pause_after_checkpoint(label):
        result = original_checkpoint(label)
        checkpoint_completed.set()
        assert allow_atomic_transition.wait(timeout=2)
        return result

    monkeypatch.setattr(operation, "checkpoint", pause_after_checkpoint)

    def enter_atomic_phase():
        try:
            operation.begin_atomic_phase("publish")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def cancel_operation():
        cancellation_started.set()
        operation.request_cancellation("client_cancelled", detached=True)
        cancellation_returned.set()

    worker = threading.Thread(target=enter_atomic_phase)
    canceller = threading.Thread(target=cancel_operation)
    worker.start()
    assert checkpoint_completed.wait(timeout=2)
    canceller.start()
    assert cancellation_started.wait(timeout=2)
    cancellation_won_gap = cancellation_returned.wait(timeout=0.1)
    allow_atomic_transition.set()
    worker.join(timeout=2)
    canceller.join(timeout=2)

    assert worker.is_alive() is False
    assert canceller.is_alive() is False
    assert errors == []
    assert cancellation_won_gap is False
    snapshot = operation.snapshot()
    assert snapshot["atomic_phase_active"] is True
    assert snapshot["status"] == "cancellation_pending"
    assert snapshot["cancellation_pending"] is True


def test_mcp_atomic_phase_records_pending_then_completes_after_cancellation(
    tmp_path,
    monkeypatch,
):
    from vector_lake.cancellation import (
        cancellation_checkpoint,
        non_interruptible_phase,
    )

    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "atomic-cancellation-test",
        runtime_guard=guard,
    )
    entered_publish = threading.Event()
    release_publish = threading.Event()
    committed = []

    @server.tool()
    def atomic_publish_probe() -> str:
        cancellation_checkpoint("before_publish")
        with non_interruptible_phase("publish"):
            entered_publish.set()
            assert release_publish.wait(timeout=2)
            committed.append("atomic")
        return "committed"

    async def scenario():
        task = asyncio.create_task(
            server.call_tool(
                "atomic_publish_probe",
                {mcp_server._MCP_CALL_DEADLINE_ARGUMENT: 0.06},
            )
        )
        with anyio.fail_after(1):
            while not entered_publish.is_set():
                await anyio.sleep(0.005)
        with pytest.raises(Exception, match="deadline exceeded") as rejected:
            await task
        return str(rejected.value)

    try:
        rejection = anyio.run(scenario)
        operation_id = re.search(r"operation_id=([a-z0-9_-]+)", rejection).group(1)
        pending = server.cancellation_status(operation_id)["operation"]
        release_publish.set()
        deadline = time.monotonic() + 1
        completed = server.cancellation_status(operation_id)["operation"]
        while completed["terminal"] is False and time.monotonic() < deadline:
            time.sleep(0.005)
            completed = server.cancellation_status(operation_id)["operation"]
    finally:
        release_publish.set()
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert pending["status"] == "cancellation_pending"
    assert pending["phase"] == "publish"
    assert pending["detached"] is True
    assert pending["atomic_phase_started"] is True
    assert pending["atomic_phase_active"] is True
    assert committed == ["atomic"]
    assert completed["status"] == "completed_after_cancellation"
    assert completed["terminal"] is True
    assert completed["atomic_phase_active"] is False


def test_mcp_cancel_after_atomic_phase_stops_at_later_checkpoint(
    tmp_path,
    monkeypatch,
):
    from vector_lake.cancellation import (
        cancellation_checkpoint,
        non_interruptible_phase,
    )

    monkeypatch.setenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", "1")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "post-atomic-cancellation-test",
        runtime_guard=guard,
    )
    atomic_completed = threading.Event()
    stopped = threading.Event()
    processed = []

    @server.tool()
    def post_atomic_scan_probe() -> str:
        try:
            with non_interruptible_phase("publish"):
                processed.append("committed")
            atomic_completed.set()
            for item in range(100):
                cancellation_checkpoint(f"post_publish_scan:{item}")
                processed.append(item)
                time.sleep(0.01)
        finally:
            stopped.set()
        return "complete"

    registered = server._tool_manager.get_tool("post_atomic_scan_probe")

    async def scenario():
        assert registered is not None
        task = asyncio.create_task(registered.fn())
        with anyio.fail_after(1):
            while not atomic_completed.is_set() or len(processed) < 2:
                await anyio.sleep(0.005)
        processed_at_cancel = len(processed)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return processed_at_cancel

    try:
        processed_at_cancel = anyio.run(scenario)
        assert stopped.wait(timeout=0.25)
        operation = server.cancellation_status()["operations"][0]
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)

    assert len(processed) <= processed_at_cancel + 1
    assert operation["status"] == "cancelled"
    assert operation["atomic_phase_started"] is True
    assert operation["atomic_phase_active"] is False
    assert operation["phase"] is None


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
        with pytest.raises(RuntimeError, match="saturated") as rejected:
            await waiter
        rejected_at = time.monotonic() - started_at
        return heartbeat_at, rejected_at, str(rejected.value)

    try:
        heartbeat_at, rejected_at, rejection = anyio.run(scenario)
    finally:
        release_blocker.set()
        blocker.result(timeout=2)
        server.shutdown_blocking_executor(wait=True)

    assert heartbeat_at < 0.2
    assert rejected_at >= 0.4
    assert "retry_after_seconds=0.500" in rejection
    assert "lane=fast" in rejection


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


def test_mcp_fast_lane_default_capacity_accepts_six_and_rejects_seventh(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("VECTOR_LAKE_MCP_BLOCKING_WORKERS", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_MCP_BLOCKING_QUEUE_CAPACITY", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_MCP_ADMISSION_TIMEOUT_SECONDS", raising=False)
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP(
        "default-capacity-test",
        runtime_guard=guard,
    )
    release = threading.Event()
    running_lock = threading.Lock()
    running = 0
    two_running = threading.Event()

    def blocked_call():
        nonlocal running
        with running_lock:
            running += 1
            if running == 2:
                two_running.set()
        return release.wait(timeout=2)

    futures = [server._submit_blocking_call(blocked_call) for _ in range(6)]
    assert two_running.wait(timeout=2)

    try:
        status = server.blocking_executor_status()
        assert status["workers"] == 2
        assert status["queue_capacity"] == 4
        assert status["inflight"] == 6
        assert status["queued_items"] == 4
        started_at = time.monotonic()
        with pytest.raises(RuntimeError) as rejected:
            server._submit_blocking_call(lambda: "seventh")
        elapsed = time.monotonic() - started_at
        message = str(rejected.value)
        assert message.startswith(
            "Vector Lake MCP blocking executor is saturated; retry later"
        )
        assert "retry_after_seconds=0.050" in message
        assert "lane=fast" in message
        assert elapsed >= 0.04
        assert elapsed < 0.5
    finally:
        release.set()
        assert all(future.result(timeout=2) is True for future in futures)
        server.shutdown_blocking_executor(wait=True)

    final_status = server.blocking_executor_status()
    assert final_status["inflight"] == 0
    assert final_status["metrics"]["admission_rejections"] == 1


def test_mcp_blocking_executor_env_restores_legacy_one_by_one_capacity(
    tmp_path,
    monkeypatch,
):
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
        with pytest.raises(RuntimeError) as rejected:
            server._submit_blocking_call(lambda: "unbounded")
        message = str(rejected.value)
        assert message.startswith(
            "Vector Lake MCP blocking executor is saturated; retry later"
        )
        assert "retry_after_seconds=0.000" in message
        assert "lane=fast" in message
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
    assert status["blocking_executor"]["fast_lane"]["workers"] >= 1
    assert status["blocking_executor"]["heavy_lane"]["workers"] >= 1
    assert status["search_performance"]["result_char_limit"] >= 1_000
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
        lambda path, **_kwargs: (
            index_snapshot.load_legacy_index_snapshot_for_migration(path)
        ),
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
        lambda path, **_kwargs: (
            index_snapshot.load_legacy_index_snapshot_for_migration(path)
        ),
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
    from vector_lake import (
        db_store,
        governance_store,
        indexer,
        runtime_health,
        tool_search,
    )

    index_path = get_wiki_dir() / "index.json"
    db_store.init_db()
    indexer.generate_index()
    clear_index_snapshot_cache_for_tests()

    search_snapshot = tool_search._load_search_index(index_path)
    health_snapshot, error = runtime_health._index_snapshot(index_path)

    assert error is None
    assert health_snapshot is search_snapshot

    # Rebuilding the same roots/generation is an exact v2 no-op, so every
    # reader must retain the same shared snapshot object.
    indexer.generate_index()
    noop_snapshot = tool_search._load_search_index(index_path)
    assert noop_snapshot is search_snapshot

    governance_store.upsert_entity(
        "entity_shared_snapshot_refresh",
        {
            "entity_id": "entity_shared_snapshot_refresh",
            "canonical_name": "Shared Snapshot Refresh",
            "page_key": "Concept_Shared-Snapshot-Refresh",
            "type": "concept",
            "raw_text": "A canonical change must rotate the commit marker.",
        },
    )
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


@pytest.mark.parametrize(
    ("module_name", "dispatch_name"),
    [
        ("scripts.semantic_dedup_daemon", "_run_legacy_daemon"),
        ("scripts.community_clustering_daemon", "_run_legacy_clustering"),
    ],
)
def test_legacy_operator_daemons_fail_closed_before_storage_access(
    tmp_path,
    monkeypatch,
    capsys,
    module_name,
    dispatch_name,
):
    from vector_lake import db_store, wiki_utils

    module = importlib.import_module(module_name)
    monkeypatch.delenv("VECTOR_LAKE_ENABLE_LEGACY_UNSAFE_DAEMONS", raising=False)
    observed = []

    def forbidden(*_args, **_kwargs):
        observed.append("storage_or_dispatch")
        raise AssertionError("legacy daemon reached DB/index/governance access")

    for owner, attribute in (
        (db_store, "get_connection"),
        (wiki_utils, "get_index_path"),
        (wiki_utils, "get_meta_dir"),
        (wiki_utils, "get_wiki_dir"),
        (governance_store, "load_governance_queue"),
        (governance_store, "save_governance_queue"),
        (mutation_coordinator, "execute_mutation_batch"),
        (module, dispatch_name),
    ):
        monkeypatch.setattr(owner, attribute, forbidden)

    monkeypatch.chdir(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert module.main() == 78

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    stderr = capsys.readouterr().err
    assert observed == []
    assert after == before
    assert "DEPRECATED/UNSUPPORTED" in stderr
    assert "disabled by default" in stderr


@pytest.mark.parametrize(
    ("module_name", "dispatch_name"),
    [
        ("scripts.semantic_dedup_daemon", "_run_legacy_daemon"),
        ("scripts.community_clustering_daemon", "_run_legacy_clustering"),
    ],
)
def test_legacy_operator_daemon_opt_in_reaches_only_dispatch_boundary(
    monkeypatch,
    capsys,
    module_name,
    dispatch_name,
):
    module = importlib.import_module(module_name)
    monkeypatch.setenv("VECTOR_LAKE_ENABLE_LEGACY_UNSAFE_DAEMONS", "1")
    observed = []
    monkeypatch.setattr(module, dispatch_name, lambda: observed.append("dispatch"))

    assert module.main() == 0

    stderr = capsys.readouterr().err
    assert observed == ["dispatch"]
    assert "DEPRECATED/UNSUPPORTED" in stderr
    assert "never run" in stderr
    assert "watchdog" in stderr


def test_active_runtime_does_not_import_legacy_operator_daemons():
    root = Path(__file__).resolve().parents[1]
    forbidden_names = (
        "semantic_dedup_daemon",
        "community_clustering_daemon",
    )
    active_paths = [root / "watchdog_sync.py", *sorted((root / "vector_lake").glob("*.py"))]

    for path in active_paths:
        content = path.read_text(encoding="utf-8")
        assert all(name not in content for name in forbidden_names), path
