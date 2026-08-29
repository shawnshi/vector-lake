import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from vector_lake import cli_app, db_store, mcp_server


_EXPECTED_READONLY_TOOLS = frozenset(
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

_EXPECTED_READONLY_DENIED_TOOLS = frozenset(
    {
        "auto_ingest_receipt_retention",
        "backup_retention",
        "batch_replace_links",
        "bulk_reconciliation",
        "canonical_backfill",
        "canonical_reconcile_content",
        "claim_ingest_tasks",
        "compact_change_set_history",
        "delete_source",
        "embedding_backfill",
        "evidence_foundation_backfill",
        "expire_ingest_tasks",
        "finalize_ingest",
        "finalize_query_synthesis",
        "gc_vector_lake",
        "history_retention",
        "lint_vector_lake",
        "merge_suggestions_vector_lake",
        "operational_memory_cleanup",
        "operational_memory_search_index",
        "orphan_source_classify",
        "prepare_ingest_batch",
        "projection_rebuild_index",
        "propose_schema_mutation",
        "query_logic_lake",
        "rebuild_timeline_events",
        "reconcile_ingest_tasks",
        "reconcile_orphan_ingest_packets",
        "record_claim_assessment",
        "remember",
        "rename_entity",
        "resolve_governance_item",
        "sync_critical_decision_registry",
        "sync_vector_lake",
        "topology_queue_cleanup",
        "trigger_audit_graph",
        "trigger_autonomous_research",
        "unsupported_claim_debt",
        "update_operational_memory",
        "visualize_vector_lake",
        "wiki_restore",
        "write_wiki_page",
    }
)


def _server_with_tools(names) -> FastMCP:
    server = FastMCP("readonly-surface-test")
    for name in names:
        def make_tool(tool_name):
            def tool_result():
                return tool_name

            tool_result.__name__ = tool_name
            return tool_result

        server.tool()(make_tool(name))
    return server


def test_readonly_surface_has_an_exhaustive_positive_and_negative_contract(
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "full")
    public_tools = {
        tool.name for tool in mcp_server.mcp._tool_manager.list_tools()
    }

    assert mcp_server._READONLY_MCP_SURFACE_TOOLS == _EXPECTED_READONLY_TOOLS
    assert _EXPECTED_READONLY_TOOLS.isdisjoint(_EXPECTED_READONLY_DENIED_TOOLS)
    assert _EXPECTED_READONLY_TOOLS | _EXPECTED_READONLY_DENIED_TOOLS == public_tools

    server = _server_with_tools(public_tools)
    effective = mcp_server.configure_mcp_surface(server, "readonly")

    assert set(effective) == _EXPECTED_READONLY_TOOLS
    assert {
        tool.name for tool in server._tool_manager.list_tools()
    } == _EXPECTED_READONLY_TOOLS
    assert mcp_server._mcp_surface_status(server) == {
        "configured_surface": "readonly",
        "effective_surface": "readonly",
        "effective_tool_count": len(_EXPECTED_READONLY_TOOLS),
        "effective_tools": sorted(_EXPECTED_READONLY_TOOLS),
    }


def test_readonly_surface_fails_closed_when_a_required_tool_is_missing():
    server = _server_with_tools(
        _EXPECTED_READONLY_TOOLS - {"doctor_vector_lake"}
    )

    with pytest.raises(
        RuntimeError,
        match=r"Readonly MCP surface is incomplete; missing tools: doctor_vector_lake",
    ):
        mcp_server.configure_mcp_surface(server, "readonly")


def test_readonly_surface_database_handle_enforces_query_only(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "readonly")

    connection = db_store.get_connection()

    assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute(
            "INSERT INTO runtime_generations (surface, generation) VALUES (?, ?)",
            ("readonly-test", 1),
        )
    with pytest.raises(
        RuntimeError,
        match="readonly_mcp_surface_disallows_write_transaction",
    ):
        with db_store.transaction():
            pass


def test_readonly_surface_never_initializes_a_missing_database(
    isolated_memory,
    monkeypatch,
):
    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "readonly")
    database_path = isolated_memory / "wiki" / ".meta" / "vector_lake.db"

    with pytest.raises(db_store.ReadOnlySnapshotUnavailable, match="database_missing"):
        db_store.get_connection()
    with pytest.raises(
        RuntimeError,
        match="readonly_mcp_surface_disallows_database_initialization",
    ):
        db_store.init_db()

    assert not database_path.exists()


def _logical_business_fingerprint(memory_root: Path) -> str:
    database_path = memory_root / "wiki" / ".meta" / "vector_lake.db"
    connection = sqlite3.connect(database_path)
    try:
        logical_rows = []
        tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for table_name, schema_sql in tables:
            if " using vec0" in str(schema_sql or "").casefold() or str(
                table_name
            ).startswith("embedding_vectors_"):
                continue
            quoted = '"' + str(table_name).replace('"', '""') + '"'
            rows = sorted(
                repr(tuple(row))
                for row in connection.execute(f"SELECT * FROM {quoted}")
            )
            logical_rows.append((str(table_name), str(schema_sql), rows))
        database_dump = repr(logical_rows).encode("utf-8")
    finally:
        connection.close()
    digest = hashlib.sha256(database_dump)
    for path in sorted(item for item in memory_root.rglob("*") if item.is_file()):
        if path.name.startswith("vector_lake.db"):
            continue
        relative = path.relative_to(memory_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_readonly_allowlist_real_calls_preserve_business_state(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.indexer import generate_index

    db_store.init_db()
    generate_index()
    db_store.close_all_connections()
    before = _logical_business_fingerprint(isolated_memory)
    public_tools = {
        tool.name for tool in mcp_server.mcp._tool_manager.list_tools()
    }
    server = _server_with_tools(public_tools)
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "full")
    mcp_server.configure_mcp_surface(server, "readonly")
    calls = {
        "auto_ingest_budget_status": mcp_server.auto_ingest_budget_status,
        "check_duplicate_entity": lambda: mcp_server.check_duplicate_entity(
            "Preview Candidate", "concept", "preview only"
        ),
        "context_pack": lambda: mcp_server.context_pack("preview"),
        "delta": lambda: mcp_server.delta("2000-01-01T00:00:00Z"),
        "doctor_vector_lake": mcp_server.doctor_vector_lake,
        "entity": lambda: mcp_server.entity("Preview Candidate"),
        "export_evidence_packet": lambda: mcp_server.export_evidence_packet(
            "missing-claim"
        ),
        "get_governance_debt": mcp_server.get_governance_debt,
        "list_ingest_tasks": mcp_server.list_ingest_tasks,
        "mcp_runtime_status": mcp_server.mcp_runtime_status,
        "memory_capabilities": mcp_server.memory_capabilities,
        "projection_report": mcp_server.projection_report,
        "recall": lambda: mcp_server.recall("preview"),
        "review_governance_list": mcp_server.review_governance_list,
        "review_strategic_purpose": mcp_server.review_strategic_purpose,
        "search_timeline": mcp_server.search_timeline,
        "search_vector_lake": lambda: mcp_server.search_vector_lake("preview"),
        "semantic_readiness": mcp_server.semantic_readiness,
        "semantic_readiness_campaign": mcp_server.semantic_readiness_campaign,
        "synthesize": lambda: mcp_server.synthesize("preview"),
        "trace_vector_lake": lambda: mcp_server.trace_vector_lake("preview"),
    }
    assert set(calls) == _EXPECTED_READONLY_TOOLS
    failures = {}
    for name, call in calls.items():
        try:
            call()
        except (RuntimeError, ValueError) as exc:
            detail = str(exc)
            if (
                "readonly_mcp_surface_disallows_database_initialization" in detail
                or "attempt to write a readonly database" in detail
            ):
                failures[name] = detail
    db_store.close_all_connections()

    assert failures == {}
    assert _logical_business_fingerprint(isolated_memory) == before


def test_mcp_main_never_starts_transport_after_surface_validation_failure(monkeypatch):
    failure = RuntimeError("readonly contract incomplete")
    run = Mock()
    monkeypatch.setattr(
        mcp_server,
        "configure_mcp_surface",
        Mock(side_effect=failure),
    )
    monkeypatch.setattr(mcp_server.mcp, "run", run)

    with pytest.raises(RuntimeError, match="readonly contract incomplete"):
        mcp_server.main()

    run.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "expected_enqueue"),
    [
        (["cli.py", "merge-suggestions"], False),
        (["cli.py", "merge-suggestions", "--preview"], False),
        (["cli.py", "merge-suggestions", "--apply"], True),
    ],
)
def test_merge_suggestions_cli_writes_only_with_explicit_apply(
    monkeypatch,
    argv,
    expected_enqueue,
):
    merge = Mock(return_value="merge report")
    monkeypatch.setattr(cli_app, "_cli_heavy_task_policy", lambda _args: None)
    monkeypatch.setattr(cli_app.tools, "merge_suggestions_vector_lake", merge)

    with patch("sys.argv", argv):
        assert cli_app.main() == 0

    merge.assert_called_once_with(limit=20, enqueue=expected_enqueue)


@pytest.mark.parametrize(
    ("command", "argv", "tool_name", "expected_dry_run"),
    [
        ("query", ["query", "topic"], "prepare_query_context", True),
        ("query", ["query", "topic", "--dry-run"], "prepare_query_context", True),
        ("query", ["query", "topic", "--apply"], "prepare_query_context", False),
        ("research", ["research"], "research_vector_lake", True),
        ("research", ["research", "--dry-run"], "research_vector_lake", True),
        ("research", ["research", "--apply"], "research_vector_lake", False),
    ],
)
def test_query_and_research_cli_are_preview_first(
    monkeypatch,
    command,
    argv,
    tool_name,
    expected_dry_run,
):
    call = Mock(return_value="preview")
    monkeypatch.setattr(cli_app, "_cli_heavy_task_policy", lambda _args: None)
    monkeypatch.setattr(cli_app.tools, tool_name, call)
    monkeypatch.setattr("sys.argv", ["cli.py", *argv])

    assert cli_app.main() == 0

    if command == "query":
        call.assert_called_once_with("topic", expected_dry_run)
    else:
        call.assert_called_once_with(expected_dry_run)


def test_research_defaults_are_preview_first_across_tool_and_mcp():
    from vector_lake import tool_research

    assert inspect.signature(tool_research.research_vector_lake).parameters[
        "dry_run"
    ].default is True
    assert inspect.signature(mcp_server.trigger_autonomous_research).parameters[
        "dry_run"
    ].default is True


@pytest.mark.parametrize(
    ("report", "expected_exit"),
    [
        (
            "[OK] Database: ready\n"
            "Infrastructure Summary: healthy\n"
            "Semantic Readiness: not_ready",
            0,
        ),
        (
            "[WARN] Gemini Embedding: unavailable\n"
            "Infrastructure Summary: healthy with warnings\n"
            "Semantic Readiness: degraded",
            0,
        ),
        (
            "[FAIL] Schema Migrations: pending\n"
            "Infrastructure Summary: issues detected\n"
            "Semantic Readiness: ready",
            2,
        ),
    ],
)
def test_doctor_cli_exit_code_tracks_infrastructure_only(
    monkeypatch,
    report,
    expected_exit,
):
    monkeypatch.setattr(cli_app, "_cli_heavy_task_policy", lambda _args: None)
    monkeypatch.setattr(cli_app.tools, "doctor_vector_lake", lambda: report)

    with patch("sys.argv", ["cli.py", "doctor"]):
        assert cli_app.main() == expected_exit


@pytest.mark.parametrize(
    ("status", "ready", "expected_exit"),
    [
        ("ready", True, 0),
        ("degraded", False, 2),
        ("not_ready", False, 2),
    ],
)
def test_readiness_cli_exit_code_is_machine_actionable(
    monkeypatch,
    status,
    ready,
    expected_exit,
):
    report = json.dumps(
        {"status": status, "ready": ready, "issues": [], "warnings": []}
    )
    monkeypatch.setattr(
        cli_app.tools,
        "semantic_readiness_vector_lake",
        lambda _decision_id: report,
    )

    with patch("sys.argv", ["cli.py", "readiness"]):
        assert cli_app.main() == expected_exit


def test_malformed_readiness_report_remains_an_execution_error(monkeypatch):
    monkeypatch.setattr(
        cli_app.tools,
        "semantic_readiness_vector_lake",
        lambda _decision_id: "not-json",
    )

    with patch("sys.argv", ["cli.py", "readiness"]):
        assert cli_app.main() == 1
