import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from vector_lake import cli_app, memory_protocol, mcp_server


def test_capability_manifest_is_explicit_about_governance_boundary():
    manifest = memory_protocol.capability_manifest()

    assert manifest["contract_version"] == "vector-lake-agent-memory/v1"
    assert manifest["effective_surface"] == "full"
    assert manifest["available_verbs"] == list(
        memory_protocol.MEMORY_PROTOCOL_VERBS
    )
    assert manifest["omitted_by_surface"] == []
    assert tuple(manifest["verbs"]) == memory_protocol.MEMORY_PROTOCOL_VERBS
    assert "forget" in manifest["omitted_verbs"]
    assert manifest["verbs"]["remember"]["mutability"] == "governed_write"
    assert manifest["verbs"]["recall"]["modes"] == ["page", "memory", "fact"]
    claim_alias = manifest["verbs"]["recall"]["deprecated_mode_aliases"][
        "claim"
    ]
    assert claim_alias["effective_mode"] == "fact"
    assert claim_alias["canonical_claim_records"] is False


def test_entity_verb_preserves_ambiguous_exact_matches(monkeypatch):
    monkeypatch.setattr(
        memory_protocol,
        "resolve_exact_entities",
        lambda *_args, **_kwargs: [
            {"key": "Concept_A", "score": 112.0},
            {"key": "Concept_B", "score": 112.0},
        ],
    )

    result = memory_protocol.entity("shared")

    assert result["ambiguous"] is True
    assert [item["key"] for item in result["matches"]] == [
        "Concept_A",
        "Concept_B",
    ]


def test_delta_is_timezone_strict_and_bounded(monkeypatch):
    monkeypatch.setattr(
        "vector_lake.indexer.read_committed_index_snapshot",
        lambda **_kwargs: {
            "nodes": {
                "Concept_New": {
                    "title": "New",
                    "type": "concept",
                    "status": "Active",
                    "updated_at": "2026-08-22T02:00:00+00:00",
                },
                "Concept_Old": {
                    "title": "Old",
                    "updated_at": "2026-08-20T02:00:00+00:00",
                },
            }
        },
    )

    result = memory_protocol.delta("2026-08-21T00:00:00Z", limit=1)

    assert [item["key"] for item in result["changes"]] == ["Concept_New"]
    assert result["includes_deletions"] is False
    with pytest.raises(ValueError, match="timezone"):
        memory_protocol.delta("2026-08-21T00:00:00")


@pytest.mark.parametrize("lightweight", [False, True])
@pytest.mark.parametrize("purpose_size", [0, 950, 1000, 1800])
def test_context_reserves_complete_purpose_within_budget(
    isolated_memory, monkeypatch, lightweight, purpose_size,
):
    from vector_lake import db_store, indexer, purpose_contract, tool_search

    db_store.init_db()
    indexer.generate_index()
    monkeypatch.setattr(
        purpose_contract, "render_strategy_directive", lambda: "p" * purpose_size,
    )
    monkeypatch.setattr(
        tool_search.governance_store,
        "search_operational_memory_views",
        lambda *_args, **_kwargs: ([], []),
    )
    if purpose_size > 1000:
        with pytest.raises(ValueError, match="strategic purpose"):
            tool_search.assemble_context("audit", max_chars=1000, lightweight=lightweight)
        return

    result = tool_search.assemble_context("audit", max_chars=1000, lightweight=lightweight)

    assert result["purpose"] == "p" * purpose_size
    used = sum(len(result[key]) for key in (
        "memory_packet", "wiki_context", "index_summary", "purpose",
    ))
    assert result["budget_used"] == used <= result["budget_max"] == 1000


@pytest.mark.parametrize("lightweight", [False, True])
def test_context_reports_missing_purpose_without_hiding_retrieval(
    isolated_memory, lightweight,
):
    from vector_lake import db_store, indexer, tool_search

    db_store.init_db()
    indexer.generate_index()
    assert not (isolated_memory / "purpose.md").exists()

    result = tool_search.assemble_context("audit", max_chars=1000, lightweight=lightweight)

    assert "[STRATEGIC PURPOSE STATUS: missing]" in result["purpose"]
    assert "strategic alignment has not been checked" in result["purpose"]
    assert result["budget_used"] <= result["budget_max"] == 1000


@pytest.mark.parametrize("lightweight", [False, True])
@pytest.mark.parametrize("failure", [PermissionError, ValueError])
def test_context_does_not_mask_other_purpose_errors(monkeypatch, lightweight, failure):
    from vector_lake import purpose_contract, tool_search

    def unreadable_purpose():
        raise purpose_contract.PurposeContractError("purpose invalid") from failure("probe")

    monkeypatch.setattr(purpose_contract, "render_strategy_directive", unreadable_purpose)

    with pytest.raises(purpose_contract.PurposeContractError, match="purpose invalid"):
        tool_search.assemble_context("audit", max_chars=1000, lightweight=lightweight)


@pytest.mark.parametrize("budget", [0, 1, 40, 100, 1000])
@pytest.mark.parametrize("unavailable", [False, True])
def test_memory_packet_text_respects_even_small_budgets(monkeypatch, budget, unavailable):
    from vector_lake import tool_search

    def memories(*_args, **_kwargs):
        if unavailable:
            raise tool_search.governance_store.OperationalMemoryNotReady("index_unready")
        return [], []

    monkeypatch.setattr(tool_search.governance_store, "search_operational_memory_views", memories)
    result = tool_search.build_memory_packet("audit", max_chars=budget)

    assert len(result["packet"]) <= budget


def _server_with_tools(names) -> FastMCP:
    server = FastMCP("surface-test")
    for name in names:
        def make_tool(tool_name):
            def tool_result():
                return tool_name

            tool_result.__name__ = tool_name
            return tool_result

        server.tool()(make_tool(name))
    return server


@pytest.mark.parametrize(
    ("surface", "expected_omitted"),
    [
        ("full", []),
        ("memory", []),
        ("readonly", ["remember"]),
    ],
)
def test_memory_capabilities_match_effective_tools_list(
    monkeypatch,
    surface,
    expected_omitted,
):
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "full")
    public_tools = {
        tool.name for tool in mcp_server.mcp._tool_manager.list_tools()
    }
    server = _server_with_tools(public_tools)
    effective_tools = mcp_server.configure_mcp_surface(server, surface)
    monkeypatch.setattr(mcp_server, "mcp", server)

    manifest = json.loads(mcp_server.memory_capabilities())
    expected_verbs = [
        verb
        for verb in memory_protocol.MEMORY_PROTOCOL_VERBS
        if verb in effective_tools
    ]

    assert manifest["effective_surface"] == surface
    assert manifest["available_verbs"] == expected_verbs
    assert set(manifest["verbs"]) == set(expected_verbs)
    assert manifest["omitted_by_surface"] == expected_omitted
    if surface == "readonly":
        assert "remember" not in manifest["verbs"]
        assert "remember" not in manifest["available_verbs"]


def test_recall_claim_mode_reports_fact_alias_semantics(monkeypatch):
    calls = []

    def fake_search(*_args, **kwargs):
        calls.append(kwargs)
        return "deprecated fact-only result"

    monkeypatch.setattr(memory_protocol, "search_vector_lake", fake_search)

    result = memory_protocol.recall("query", mode="claim")

    assert calls[0]["mode"] == "claim"
    assert result["requested_mode"] == "claim"
    assert result["mode"] == "fact"
    assert result["deprecated_alias"] is True
    assert "not canonical Claim records" in result["semantic_warning"]


def test_memory_surface_is_exact_and_fail_closed(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "full")
    server = _server_with_tools(
        [*mcp_server._MEMORY_MCP_SURFACE_TOOLS, "dangerous_extra"]
    )

    names = mcp_server.configure_mcp_surface(server, "memory")

    assert set(names) == mcp_server._MEMORY_MCP_SURFACE_TOOLS
    assert "dangerous_extra" not in {
        tool.name for tool in server._tool_manager.list_tools()
    }
    with pytest.raises(RuntimeError, match="Unsupported"):
        mcp_server.configure_mcp_surface(server, "unknown")


def test_public_surface_counts_match_documented_contract():
    parser = cli_app.build_parser()
    subcommands = next(
        choices
        for choices in (
            getattr(action, "choices", None) for action in parser._actions
        )
        if isinstance(choices, dict)
    )
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    # Freeze names as well as counts: the full surface exposes 70 tools, including the
    # two guarded repair/recovery tools added after the earlier 67-tool surface.
    expected_full = set("""
        auto_ingest_budget_status auto_ingest_receipt_retention backup_retention
        batch_replace_links bulk_reconciliation canonical_backfill canonical_reconcile_content
        check_duplicate_entity claim_ingest_tasks claim_placeholder_cleanup claim_provenance_repair compact_change_set_history
        context_pack delete_source delta doctor_vector_lake embedding_backfill entity
        evidence_foundation_backfill expire_ingest_tasks export_evidence_packet
        finalize_exact_reviewed_ingest_outputs finalize_ingest finalize_query_synthesis
        gc_vector_lake get_governance_debt history_retention lint_vector_lake list_ingest_tasks
        mcp_runtime_status memory_capabilities merge_suggestions_vector_lake
        operational_memory_cleanup operational_memory_search_index orphan_source_classify
        prepare_ingest_batch projection_rebuild_index projection_report propose_schema_mutation
        query_logic_lake rebuild_timeline_events recall reconcile_ingest_tasks
        reconcile_orphan_ingest_packets record_claim_assessment recover_failed_mutation_outbox
        recover_terminal_ingest_outputs remember rename_entity resolve_governance_item
        retry_terminal_ingest_job review_governance_list review_strategic_purpose search_timeline
        search_vector_lake semantic_readiness semantic_readiness_campaign
        sync_critical_decision_registry sync_vector_lake synthesize topology_queue_cleanup
        trace_vector_lake trigger_audit_graph trigger_autonomous_research unsupported_claim_debt
        update_operational_memory visualize_vector_lake wiki_restore write_wiki_batch write_wiki_page
    """.split())
    tools = mcp_server.mcp._tool_manager.list_tools()
    assert {tool.name for tool in tools} == expected_full
    assert len(tools) == 70
    assert len(mcp_server._MEMORY_MCP_SURFACE_TOOLS) == 9
    assert len(mcp_server._READONLY_MCP_SURFACE_TOOLS) == 21
    assert len(subcommands) == 42
    assert (
        "70 MCP tools (`full`) / 9 MCP tools (`memory`) / "
        "21 MCP tools (`readonly`) / 42 CLI commands"
    ) in readme


def test_remember_wrapper_rejects_payload_without_leaking_exception(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_read_payload",
        lambda _path: (_ for _ in ()).throw(ValueError("C:/private/secret")),
    )

    result = json.loads(mcp_server.remember("fact", "C:/private/secret"))

    assert result["ok"] is False
    assert result["committed"] is False
    assert "C:/private" not in json.dumps(result)
