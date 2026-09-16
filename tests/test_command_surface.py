import tomllib
from pathlib import Path

from vector_lake import mcp_server


ROOT = Path(__file__).resolve().parents[1]


def _load_command(name: str) -> dict:
    with (ROOT / "commands" / f"{name}.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_query_and_timeline_compatibility_commands_match_mcp_tools():
    query = _load_command("query")
    timeline = _load_command("timeline")

    assert "query_logic_lake" in query["prompt"]
    assert "search_timeline" in timeline["prompt"]
    assert callable(mcp_server.query_logic_lake)
    assert callable(mcp_server.search_timeline)


def test_query_and_timeline_codex_skills_are_packaged():
    assert (ROOT / "skills" / "query" / "SKILL.md").is_file()
    assert (ROOT / "skills" / "timeline" / "SKILL.md").is_file()


def test_mcp_server_registers_its_tool_surface():
    """The server must import and register tools on the installed SDK.

    A CI red run came from exactly this surface disappearing: the declared mcp
    floor resolved to 2.x while the module still imported the 1.x FastMCP name,
    so every MCP-touching test module failed at import time.
    """
    names = mcp_server.registered_tool_names(mcp_server.mcp)

    assert len(names) >= 30, f"tool surface shrank unexpectedly: {len(names)}"
    assert "search_vector_lake" in names
    assert "finalize_ingest" in names
    assert "doctor_vector_lake" in names
