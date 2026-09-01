import json
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
    assert template["max_tokens_per_task"] == 81920
    assert template["max_reserved_tokens_per_hour"] == 100 * 81920
    assert template["max_reserved_tokens_per_24h"] == 65536000
    assert "absolute/path" in template["codex_executable"]
    for key in (
        "required_codex_sha256",
        "required_system_skills_sha256",
        "required_models_cache_sha256",
        "required_auth_identity_sha256",
    ):
        assert template[key] == "0" * 64
