import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_release_metadata_and_runtime_config_are_consistent():
    codex_manifest = _load_json(".codex-plugin/plugin.json")
    gemini_manifest = _load_json("gemini-extension.json")
    codex_mcp = _load_json(".mcp.json")
    compatibility_mcp = _load_json("mcp_config.json")
    runtime_config = _load_json("config.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    package_version = codex_manifest["version"]
    base_version = package_version.split("+", 1)[0]

    assert package_version in readme
    assert f"# Vector Lake {base_version}" in changelog
    assert gemini_manifest["version"] == base_version
    assert gemini_manifest["runtime"]["python_version"] == ">=3.11"
    assert gemini_manifest["mcpServers"]["vector-lake-mcp"]["cwd"] == (
        "${extensionPath}"
    )
    assert set(runtime_config) == {
        "target_directories",
        "exclude_paths",
        "supported_extensions",
    }
    assert runtime_config["target_directories"] == []
    assert "processed_files_path" not in runtime_config

    codex_env = codex_mcp["mcpServers"]["vector-lake-mcp"]["env"]
    compatibility_env = compatibility_mcp["mcpServers"]["vector-lake-mcp"]["env"]
    gemini_env = gemini_manifest["mcpServers"]["vector-lake-mcp"]["env"]
    functional_env_names = (
        "VECTOR_LAKE_MEMORY_DIR",
        "VECTOR_LAKE_META_DIR",
        "VECTOR_LAKE_OPERATIONAL_MEMORY_FTS",
        "VECTOR_LAKE_DURABILITY_PROFILE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
    )
    for name in functional_env_names:
        assert gemini_env[name] == codex_env[name] == compatibility_env[name]
    assert gemini_env["VECTOR_LAKE_OPERATIONAL_MEMORY_FTS"] == "1"
    assert gemini_env["VECTOR_LAKE_DURABILITY_PROFILE"] == "full"

    positioning = "healthcare digitalization"
    assert positioning in codex_manifest["description"].lower()
    assert positioning in codex_manifest["interface"]["longDescription"].lower()
    assert positioning in gemini_manifest["description"].lower()


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


def test_command_documentation_matches_the_exact_gemini_inventory():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    commands = sorted(path.stem for path in (ROOT / "commands").glob("*.toml"))

    assert len(commands) == 19
    for command in commands:
        assert f"`/{command}`" in readme
    assert "名称并非逐项相同" in readme
    assert "`/vl_sync`" not in context


def test_auto_ingest_template_is_explicitly_disabled_and_unapproved():
    template = _load_json("templates/auto_ingest_config.example.json")

    assert template["schema_version"] == 1
    assert template["enabled"] is False
    assert template["allow_model_processing_raw_text"] is False
    assert template["auto_finalize_rejected"] is False
    assert template["max_tasks_per_hour"] == 100
    assert template["max_tasks_per_24h"] == 2000
    assert template["max_tokens_per_task"] == 32768
    assert template["max_reserved_tokens_per_hour"] == 100 * 32768
    assert template["max_reserved_tokens_per_24h"] == 2000 * 32768
    assert "absolute/path" in template["codex_executable"]
    for key in (
        "required_codex_sha256",
        "required_system_skills_sha256",
        "required_models_cache_sha256",
        "required_auth_identity_sha256",
    ):
        assert template[key] == "0" * 64
