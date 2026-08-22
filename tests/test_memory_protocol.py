import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from vector_lake import cli_app, memory_protocol, mcp_server


def test_capability_manifest_is_explicit_about_governance_boundary():
    manifest = memory_protocol.capability_manifest()

    assert manifest["contract_version"] == "vector-lake-agent-memory/v1"
    assert tuple(manifest["verbs"]) == memory_protocol.MEMORY_PROTOCOL_VERBS
    assert "forget" in manifest["omitted_verbs"]
    assert manifest["verbs"]["remember"]["mutability"] == "governed_write"


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


def test_memory_surface_is_exact_and_fail_closed():
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
        action.choices
        for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict)
    )
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert len(mcp_server.mcp._tool_manager.list_tools()) == 60
    assert len(mcp_server._MEMORY_MCP_SURFACE_TOOLS) == 8
    assert len(subcommands) == 35
    assert "60 MCP tools (`full`) / 8 MCP tools (`memory`) / 35 CLI commands" in readme


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
