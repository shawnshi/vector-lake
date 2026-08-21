import json
from pathlib import Path

import pytest

import watchdog_sync


def _write_manifest(path: Path, env: dict[str, str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "vector-lake-mcp": {
                        "env": env,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_bootstrap_runtime_paths_uses_mcp_manifest_when_process_env_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("VECTOR_LAKE_MEMORY_DIR", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    manifest = _write_manifest(
        tmp_path / ".mcp.json",
        {
            "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
            "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
        },
    )

    applied = watchdog_sync._bootstrap_runtime_paths(manifest)

    assert applied == {
        "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
        "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
    }
    assert watchdog_sync.os.environ["VECTOR_LAKE_MEMORY_DIR"] == "~/MEMORY"
    assert watchdog_sync.os.environ["VECTOR_LAKE_META_DIR"] == "~/MEMORY/wiki/.meta"


def test_bootstrap_runtime_paths_preserves_explicit_process_env_pair(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", "D:/explicit-memory")
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", "D:/explicit-memory/wiki/.meta")
    manifest = _write_manifest(
        tmp_path / ".mcp.json",
        {
            "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
            "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
        },
    )

    applied = watchdog_sync._bootstrap_runtime_paths(manifest)

    assert applied == {}
    assert watchdog_sync.os.environ["VECTOR_LAKE_MEMORY_DIR"] == "D:/explicit-memory"
    assert watchdog_sync.os.environ["VECTOR_LAKE_META_DIR"] == "D:/explicit-memory/wiki/.meta"


def test_bootstrap_runtime_paths_rejects_partial_process_override(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", "D:/explicit-memory")
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)

    with pytest.raises(RuntimeError, match="must be set together"):
        watchdog_sync._bootstrap_runtime_paths(tmp_path / ".mcp.json")


@pytest.mark.parametrize(
    "manifest_env, expected_message",
    [
        ({"VECTOR_LAKE_MEMORY_DIR": "~/MEMORY"}, "VECTOR_LAKE_META_DIR"),
        (None, "manifest is invalid"),
    ],
)
def test_bootstrap_runtime_paths_fails_closed_for_invalid_manifest(
    tmp_path,
    monkeypatch,
    manifest_env,
    expected_message,
):
    monkeypatch.delenv("VECTOR_LAKE_MEMORY_DIR", raising=False)
    monkeypatch.delenv("VECTOR_LAKE_META_DIR", raising=False)
    manifest = tmp_path / ".mcp.json"
    if manifest_env is None:
        manifest.write_text("{}", encoding="utf-8")
    else:
        _write_manifest(manifest, manifest_env)

    with pytest.raises(RuntimeError, match=expected_message):
        watchdog_sync._bootstrap_runtime_paths(manifest)
    assert "VECTOR_LAKE_MEMORY_DIR" not in watchdog_sync.os.environ
    assert "VECTOR_LAKE_META_DIR" not in watchdog_sync.os.environ
