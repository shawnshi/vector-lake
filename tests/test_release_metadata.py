import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_release_metadata_and_runtime_config_are_consistent():
    codex_manifest = _load_json(".codex-plugin/plugin.json")
    gemini_manifest = _load_json("gemini-extension.json")
    runtime_config = _load_json("config.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    package_version = codex_manifest["version"]
    base_version = package_version.split("+", 1)[0]

    assert package_version in readme
    assert f"# Vector Lake {base_version}" in changelog
    assert gemini_manifest["version"] == base_version
    assert gemini_manifest["mcpServers"]["vector-lake-mcp"]["cwd"] == (
        "${extensionPath}"
    )
    assert set(runtime_config) == {
        "target_directories",
        "exclude_paths",
        "supported_extensions",
    }
    assert "processed_files_path" not in runtime_config
