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
