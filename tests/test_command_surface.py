import inspect
import json
from pathlib import Path

from vector_lake import mcp_server


ROOT = Path(__file__).resolve().parents[1]


def test_query_and_timeline_mcp_tools_are_registered():
    """The two capabilities the old compat layer mapped to must still exist."""
    names = mcp_server.registered_tool_names(mcp_server.mcp)

    assert "query_logic_lake" in names
    assert "search_timeline" in names
    assert callable(mcp_server.query_logic_lake)
    assert callable(mcp_server.search_timeline)


def test_query_and_timeline_codex_skills_are_packaged():
    assert (ROOT / "skills" / "vector-lake-query" / "SKILL.md").is_file()
    assert (ROOT / "skills" / "vector-lake-timeline" / "SKILL.md").is_file()


def test_all_packaged_skills_use_vector_lake_prefix():
    """All skills exposed by Vector Lake must be namespaced with vector-lake-* to avoid global collisions."""
    skills_dir = ROOT / "skills"
    assert skills_dir.is_dir()
    for child in skills_dir.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            assert child.name.startswith("vector-lake-"), f"Skill {child.name} lacks vector-lake-* prefix"


def test_the_mcp_query_tool_forwards_the_documented_dry_run_switch(monkeypatch):
    """The prompt template documents ``dry_run: true``; the wrapper dropped the argument.

    ``prepare_query_context`` has always accepted it and the CLI passes it, so an MCP caller
    had no way to reach the behaviour the template promises.
    """
    parameters = inspect.signature(mcp_server.query_logic_lake).parameters
    assert "dry_run" in parameters, parameters

    seen = []

    def fake(query_str, dry_run=False):
        seen.append((query_str, dry_run))
        return "rendered"

    monkeypatch.setattr(mcp_server.tools, "prepare_query_context", fake)

    assert mcp_server.query_logic_lake("q") == "rendered"
    assert mcp_server.query_logic_lake("q", dry_run=True) == "rendered"
    assert seen == [("q", False), ("q", True)], seen


def test_host_mcp_manifests_are_identical():
    """Hosts look for different manifest filenames; keep one source, assert the mirror.

    ``.mcp.json`` is canonical and ``mcp_config.json`` is a byte-identical
    mirror for the other host convention.  They are not produced by a generator
    script, so this test is the only thing preventing silent drift.
    """
    canonical = (ROOT / ".mcp.json").read_bytes()
    assert (ROOT / "mcp_config.json").read_bytes() == canonical, (
        ".mcp.json and mcp_config.json diverged; .mcp.json is canonical"
    )

    servers = json.loads(canonical)["mcpServers"]
    assert set(servers) == {"vector-lake-mcp"}
    entry = servers["vector-lake-mcp"]
    assert entry["args"] == ["-m", "vector_lake.mcp_server"]
    assert entry["env"]["PYTHONPATH"] == "."


def test_no_slash_command_compat_layer_ships():
    """`commands/` was removed on purpose; the MCP surface is the command surface.

    It held two `commands/*.toml` files that no code read, and CONTEXT.md section 4
    advertised sixteen of them at one point.  This guard fires on a silent
    re-introduction and on documentation that drifts back to claiming they ship.
    """
    assert not (ROOT / "commands").exists(), (
        "commands/ reappeared; the slash-command compat layer was removed in the "
        "host-convention batch. Update CONTEXT.md section 4 and README.md before "
        "bringing it back"
    )

    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    assert "commands/*.toml` files ship" not in context
    assert "Gemini CLI compatibility commands" not in context

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "commands/query.toml" not in readme
    assert "commands/timeline.toml" not in readme


def test_mcp_server_registers_its_tool_surface():
    """The server must register exactly the 18 core tools."""
    names = mcp_server.registered_tool_names(mcp_server.mcp)

    assert len(names) == 18, f"expected exactly 18 core tools, got {len(names)}: {names}"
    assert "search_vector_lake" in names
    assert "query_logic_lake" in names
    assert "doctor_vector_lake" in names
