import inspect
import json
import re
import shlex
from pathlib import Path

from vector_lake import mcp_server


ROOT = Path(__file__).resolve().parents[1]

#: The published MCP server key, shared by both host manifests and the README example.
EXPECTED_MCP_SERVER = "mentat-mind-mcp"


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

    The published server name is pinned here *and* cross-checked against the README, because the
    2026-09 rename (``vector-lake-mcp`` -> ``mentat-mind-mcp``) landed in the manifests and the
    README but left this assertion behind: the guard then failed on the new name instead of
    catching a rename that only reached one surface.  A name is published in three places, so the
    assertion has to look at all three.
    """
    canonical = (ROOT / ".mcp.json").read_bytes()
    assert (ROOT / "mcp_config.json").read_bytes() == canonical, (
        ".mcp.json and mcp_config.json diverged; .mcp.json is canonical"
    )

    servers = json.loads(canonical)["mcpServers"]
    assert set(servers) == {EXPECTED_MCP_SERVER}, (
        f"the host manifests must publish exactly {EXPECTED_MCP_SERVER!r}, got {sorted(servers)}"
    )
    entry = servers[EXPECTED_MCP_SERVER]
    assert entry["args"] == ["-m", "vector_lake.mcp_server"]
    assert entry["env"]["PYTHONPATH"] == "."

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'"{EXPECTED_MCP_SERVER}"' in readme, (
        f"README must document the published MCP server name {EXPECTED_MCP_SERVER!r}"
    )
    stale = "vector-lake-mcp"
    stale_files = [
        str(path.relative_to(ROOT))
        for path in list(ROOT.glob("skills/*/SKILL.md"))
        if stale in path.read_text(encoding="utf-8")
    ]
    assert stale_files == [], (
        f"these shipped skills still call the MCP server {stale!r}: {stale_files}"
    )


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
    """The server registers the inline preview alongside its proposal tools."""
    names = mcp_server.registered_tool_names(mcp_server.mcp)

    assert len(names) == 19, f"expected exactly 19 core tools, got {len(names)}: {names}"
    assert "preview_query_context" in names
    assert "search_vector_lake" in names
    assert "query_logic_lake" in names
    assert "doctor_vector_lake" in names


def test_readme_maintenance_examples_match_parser_and_bind_approval():
    from scripts import repair_orphan_memory

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = [line for line in readme.splitlines() if line.startswith("python scripts/repair_orphan_memory.py ")]
    assert len(commands) == 5
    parser_source = inspect.getsource(repair_orphan_memory.main)
    documented_modes = set()
    for line in commands:
        documented_modes.add(re.search(r"--mode ([a-z-]+)", line).group(1))
        for option in re.findall(r"--[a-z0-9-]+", line):
            assert f'"{option}"' in parser_source
        if "--mode apply " in line or "--mode restore " in line or "--mode refresh-triggers " in line:
            assert "--approved-count $ApprovedCount" in line
            assert "--approved-sha256 $ApprovedScopeHash" in line
    assert documented_modes == {"freeze", "dry-run", "refresh-triggers", "apply", "restore"}


def test_readme_json_examples_are_parseable_and_use_the_published_module():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*\n(.*?)```", readme, re.DOTALL)
    assert blocks, "README must include a copyable MCP configuration"
    configs = []
    for block in blocks:
        parsed = json.loads(block)
        if "mcpServers" in parsed:
            configs.append(parsed)
    assert len(configs) == 1
    entry = configs[0]["mcpServers"][EXPECTED_MCP_SERVER]
    assert entry["args"] == ["-m", "vector_lake.mcp_server"]
    assert entry["env"]["PYTHONPATH"] == "C:/path/to/vector-lake"
    assert "GEMINI_API_KEY" not in entry["env"]


def test_readme_tool_table_matches_registered_tools():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Commands", 1)[1].split("基础体检：", 1)[0]
    names = set()
    for line in table.splitlines():
        if line.startswith("|**"):
            tool_cell = line.split("|")[2]
            names.update(re.findall(r"`([a-z_]+)`", tool_cell))
    assert names == set(mcp_server.registered_tool_names(mcp_server.mcp))


def test_readme_cli_examples_parse_without_running_commands():
    from vector_lake.cli_app import build_parser

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    parser = build_parser()
    examples = 0
    for block in re.findall(r"```(?:powershell|bash|sh)\s*\n(.*?)```", readme, re.DOTALL):
        assert r"\_" not in block, "Markdown escapes inside code break copy/paste"
        for line in block.splitlines():
            command = line.strip()
            if command.startswith("python cli.py "):
                tokens = shlex.split(command)
                parser.parse_args(tokens[2:])
                examples += 1
    assert examples >= 20, "the operational CLI examples must remain covered"
