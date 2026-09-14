import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_release_metadata_runtime_profile_and_host_adapters_are_consistent():
    agent_manifest = _load_json("plugin.json")
    agent_mcp = _load_json("mcp.json")
    codex_manifest = _load_json(".codex-plugin/plugin.json")
    gemini_manifest = _load_json("gemini-extension.json")
    codex_mcp = _load_json(".codex-plugin/mcp.json")
    shared_mcp = _load_json(".mcp.json")
    compatibility_mcp = _load_json("mcp_config.json")
    runtime_profiles = _load_json("runtime_profiles.json")
    runtime_config = _load_json("config.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    package_version = codex_manifest["version"]
    base_version = package_version.split("+", 1)[0]

    assert package_version in readme
    assert f"# Vector Lake {base_version}" in changelog
    assert agent_manifest["version"] == base_version
    assert gemini_manifest["version"] == base_version
    assert codex_manifest["mcpServers"] == "./.codex-plugin/mcp.json"
    assert agent_manifest["$schema"] == (
        "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    )
    assert agent_mcp["$schema"] == (
        "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    )
    assert gemini_manifest["runtime"] == {
        "launcher": "scripts/vector_lake_mcp.py",
        "profile": "default",
        "python_version": ">=3.11",
        "setup": "pip install -r requirements.txt",
    }
    assert set(runtime_config) == {
        "target_directories",
        "exclude_paths",
        "supported_extensions",
    }
    assert runtime_config["target_directories"] == []
    assert "processed_files_path" not in runtime_config

    relative_args = [
        "scripts/vector_lake_mcp.py",
        "--profile",
        "default",
        "--surface",
        "full",
    ]
    codex_server = codex_mcp["mcpServers"]["vector-lake-mcp"]
    shared_server = shared_mcp["mcpServers"]["vector-lake-mcp"]
    compatibility_server = compatibility_mcp["mcpServers"]["vector-lake-mcp"]
    agent_server = agent_mcp["mcpServers"]["vector-lake-mcp"]
    gemini_server = gemini_manifest["mcpServers"]["vector-lake-mcp"]

    assert (
        codex_server["args"]
        == shared_server["args"]
        == compatibility_server["args"]
        == relative_args
    )
    assert (
        codex_server["cwd"]
        == shared_server["cwd"]
        == compatibility_server["cwd"]
        == "."
    )
    assert codex_server["env"] == {
        "VECTOR_LAKE_PAYLOAD_ROOT": "~/.codex/brain",
        "VECTOR_LAKE_AGENT_SANDBOX_ROOTS": "~/.codex",
    }
    assert agent_server["type"] == "stdio"
    assert agent_server["args"] == [
        "${PLUGIN_ROOT}/scripts/vector_lake_mcp.py",
        *relative_args[1:],
    ]
    assert agent_server["cwd"] == "${PLUGIN_ROOT}"
    assert gemini_server["args"] == [
        "${extensionPath}/scripts/vector_lake_mcp.py",
        *relative_args[1:],
    ]
    assert gemini_server["cwd"] == "${extensionPath}"
    assert agent_server["env"] == {
        "VECTOR_LAKE_PAYLOAD_ROOT": "${PLUGIN_DATA}/payloads",
        "VECTOR_LAKE_AGENT_SANDBOX_ROOTS": "${PLUGIN_DATA}",
    }
    assert gemini_server["env"] == {
        "VECTOR_LAKE_PAYLOAD_ROOT": "~/.gemini/tmp",
        "VECTOR_LAKE_AGENT_SANDBOX_ROOTS": "~/.gemini",
    }
    for server in (
        codex_server,
        shared_server,
        compatibility_server,
        agent_server,
        gemini_server,
    ):
        assert "PYTHONPATH" not in server.get("env", {})
        assert "VECTOR_LAKE_MEMORY_DIR" not in server.get("env", {})
        assert "VECTOR_LAKE_META_DIR" not in server.get("env", {})

    profile_env = runtime_profiles["profiles"]["default"]["env"]
    assert runtime_profiles["schema_version"] == 1
    assert profile_env["VECTOR_LAKE_MEMORY_DIR"] == "~/MEMORY"
    assert profile_env["VECTOR_LAKE_META_DIR"] == "~/MEMORY/wiki/.meta"
    assert profile_env["VECTOR_LAKE_OPERATIONAL_MEMORY_FTS"] == "1"
    assert profile_env["VECTOR_LAKE_DURABILITY_PROFILE"] == "full"

    positioning = "healthcare digitalization"
    assert positioning in codex_manifest["description"].lower()
    assert positioning in codex_manifest["interface"]["longDescription"].lower()
    assert positioning in gemini_manifest["description"].lower()
    assert positioning in agent_manifest["description"].lower()


def _readme_contract_cell(label: str) -> str:
    """Return the `Current contract` cell of one README Runtime Contract row."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"^\|\s*" + re.escape(label) + r"\s*\|\s*(.+?)\s*\|\s*$",
        readme,
        flags=re.MULTILINE,
    )
    assert match is not None, f"README Runtime Contract row missing: {label}"
    return match.group(1)


def _backticked_numbers(cell: str) -> list[str]:
    """Numbers declared inside backticked spans, in reading order.

    Handles both ``the `7` value`` and ``the `CONST = 7` value`` shapes.
    """
    return [
        match.group(1)
        for span in re.findall(r"`([^`]+)`", cell)
        for match in [re.search(r"([0-9]+)\s*$", span)]
        if match is not None
    ]


def test_readme_runtime_contract_numbers_match_their_source_constants():
    """The Runtime Contract table is an operator's guide to stop-the-world
    migrations. A stale number there is worse than a missing one, so every
    value with a source constant must be derived rather than transcribed.
    """
    from vector_lake import db_store, indexer, projection_format_v2
    from vector_lake import projection_store_v2, tool_evidence, tool_ingest

    ingest = _readme_contract_cell("Ingest payload")
    assert f"`INGEST_CONTRACT_VERSION = {tool_ingest.INGEST_CONTRACT_VERSION}`" == (
        ingest
    )

    migration = _readme_contract_cell("SQLite migration schema")
    assert f"`PRAGMA user_version = {db_store._SCHEMA_VERSION}`" == migration

    projection = _readme_contract_cell("Index projection")
    assert _backticked_numbers(projection) == [
        str(indexer.PROJECTION_CONTRACT_VERSION),
        str(projection_format_v2.FORMAT_VERSION),
    ], projection
    # The two projection stores declare the same physical format; if they ever
    # disagree the README cannot be correct for both readers.
    assert projection_store_v2.FORMAT_VERSION == projection_format_v2.FORMAT_VERSION

    packet = _readme_contract_cell("EvidencePacket")
    assert f"`{tool_evidence.EVIDENCE_PACKET_CONTRACT_VERSION}`" == packet


def test_readme_public_surface_counts_match_the_live_surfaces():
    """Surface counts are the cheapest thing in the table to let drift, and the
    most misleading: an unavailable tool is indistinguishable from a missing
    one to the reader. Count the live definitions instead of trusting the row.
    """
    from vector_lake import cli_app, mcp_server  # noqa: F401

    server_source = (ROOT / "vector_lake" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    cli_source = (ROOT / "vector_lake" / "cli_app.py").read_text(encoding="utf-8")
    full_tools = server_source.count("@mcp.tool()")
    memory_tools = len(mcp_server._MEMORY_MCP_SURFACE_TOOLS)
    readonly_tools = len(mcp_server._READONLY_MCP_SURFACE_TOOLS)
    cli_commands = len(re.findall(r"add_parser\(", cli_source))
    skills = [
        path
        for path in (ROOT / "skills").iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    ]

    # The row emphasises the surface names with backticks; fold them away so a
    # pure naming choice cannot fail the count check.
    cell = _readme_contract_cell("Public surfaces").replace("`", "")
    for expected, name in (
        (full_tools, "MCP tools (full)"),
        (memory_tools, "MCP tools (memory)"),
        (readonly_tools, "MCP tools (readonly)"),
        (cli_commands, "CLI commands"),
        (len(skills), "Agent skills"),
    ):
        assert f"{expected} {name}" in cell, f"{name}: README says {cell!r}"


def test_governance_schema_version_is_a_separate_axis_from_user_version():
    """`Canonical governance schema` is the version of schema.md itself, not of
    the SQLite store. The two axes move independently, so neither must be
    compared against the other -- but both documents must agree with each other.
    """
    from vector_lake import db_store

    cell = _readme_contract_cell("Canonical governance schema")
    governance_version = cell.strip().strip("`")
    assert governance_version != str(db_store._SCHEMA_VERSION), (
        "governance schema and SQLite user_version are independent axes; "
        "if they are set to the same number the distinction has been lost"
    )

    schema_doc = (ROOT / "schema.md").read_text(encoding="utf-8")
    header = schema_doc.splitlines()[0]
    assert f"Schema V{governance_version}" in header, (
        f"schema.md header {header!r} disagrees with the README row {cell!r}"
    )


def test_readonly_docs_do_not_claim_physical_zero_write():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")

    assert "只读 MCP 报告面" not in readme
    assert "CLI 的重任务诊断入口" in readme
    assert "VECTOR_LAKE_MCP_SURFACE=readonly" in readme
    assert "不写 canonical meta" in readme
    assert "不承诺整个 meta 目录物理零写入" in readme
    assert "exact 21-tool physical-read surface" in context
    assert "bypass the canonical-meta file gate" in context
    assert "CLI diagnostics and the other MCP surfaces may still publish" in context


def test_gemini_thin_adapter_does_not_restore_legacy_slash_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")

    assert not list((ROOT / "commands").glob("*.toml"))
    assert "Gemini thin adapter" in context
    assert "Gemini 薄适配器" in readme
    assert "commands/*.toml" in readme
    assert "commands/*.toml" in context


def test_auto_ingest_template_is_explicitly_disabled_and_unapproved():
    template = _load_json("templates/auto_ingest_config.example.json")

    assert template["schema_version"] == 1
    assert template["enabled"] is False
    assert template["allow_model_processing_raw_text"] is False
    assert template["auto_finalize_rejected"] is False
    assert template["max_tasks_per_hour"] == 100
    assert template["max_tasks_per_24h"] == 2000
    assert template["max_tokens_per_task"] == 262144
    # 13107200 is the fixed hourly hard cap, so the example config sits on it.
    assert template["max_reserved_tokens_per_hour"] == 13107200
    assert template["max_reserved_tokens_per_hour"] == min(100 * 262144, 13107200)
    assert template["max_reserved_tokens_per_24h"] == 65536000
    assert "absolute/path" in template["codex_executable"]
    for key in (
        "required_codex_sha256",
        "required_system_skills_sha256",
        "required_models_cache_sha256",
        "required_auth_identity_sha256",
    ):
        assert template[key] == "0" * 64
