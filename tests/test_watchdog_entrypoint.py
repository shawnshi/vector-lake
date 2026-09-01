import json
import os
from pathlib import Path

import pytest

import watchdog_sync


def _write_manifest(
    path: Path,
    env: dict[str, str] | None,
    *,
    profile: str = "default",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": {
                    profile: {
                        "env": env,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _clear_runtime_env(monkeypatch) -> None:
    for name in (
        "VECTOR_LAKE_MEMORY_DIR",
        "VECTOR_LAKE_META_DIR",
        "VECTOR_LAKE_DB_PATH",
        "VECTOR_LAKE_OPERATIONAL_MEMORY_FTS",
        "VECTOR_LAKE_DURABILITY_PROFILE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_bootstrap_runtime_paths_uses_profile_when_process_env_is_missing(
    tmp_path,
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "runtime_profiles.json",
        {
            "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
            "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
            "VECTOR_LAKE_OPERATIONAL_MEMORY_FTS": "1",
            "VECTOR_LAKE_DURABILITY_PROFILE": "full",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        },
    )

    applied = watchdog_sync._bootstrap_runtime_paths(manifest)

    assert applied == {
        "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
        "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
        "VECTOR_LAKE_OPERATIONAL_MEMORY_FTS": "1",
        "VECTOR_LAKE_DURABILITY_PROFILE": "full",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
    assert os.environ["VECTOR_LAKE_MEMORY_DIR"] == "~/MEMORY"
    assert os.environ["VECTOR_LAKE_META_DIR"] == "~/MEMORY/wiki/.meta"
    assert Path(os.environ["VECTOR_LAKE_DB_PATH"]) == (
        Path("~/MEMORY/wiki/.meta").expanduser() / "vector_lake.db"
    )


def test_bootstrap_runtime_paths_preserves_process_overrides(
    tmp_path,
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    explicit_memory = tmp_path / "explicit-memory"
    explicit_meta = explicit_memory / "wiki" / ".meta"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(explicit_memory))
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", str(explicit_meta))
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "balanced")
    manifest = _write_manifest(
        tmp_path / "runtime_profiles.json",
        {
            "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
            "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
            "VECTOR_LAKE_DURABILITY_PROFILE": "full",
        },
    )

    applied = watchdog_sync._bootstrap_runtime_paths(manifest)

    assert applied == {}
    assert os.environ["VECTOR_LAKE_MEMORY_DIR"] == str(explicit_memory)
    assert os.environ["VECTOR_LAKE_META_DIR"] == str(explicit_meta)
    assert os.environ["VECTOR_LAKE_DURABILITY_PROFILE"] == "balanced"
    assert Path(os.environ["VECTOR_LAKE_DB_PATH"]) == (
        explicit_meta / "vector_lake.db"
    )


def test_bootstrap_runtime_paths_selects_named_profile(
    tmp_path,
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    manifest = {
        "schema_version": 1,
        "profiles": {
            "default": {
                "env": {
                    "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
                    "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
                }
            },
            "isolated": {
                "env": {
                    "VECTOR_LAKE_MEMORY_DIR": "~/isolated-memory",
                    "VECTOR_LAKE_META_DIR": "~/isolated-memory/wiki/.meta",
                }
            },
        },
    }
    path = tmp_path / "runtime_profiles.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("VECTOR_LAKE_RUNTIME_PROFILE", "isolated")

    watchdog_sync._bootstrap_runtime_paths(path)

    assert os.environ["VECTOR_LAKE_MEMORY_DIR"] == "~/isolated-memory"
    assert os.environ["VECTOR_LAKE_META_DIR"] == "~/isolated-memory/wiki/.meta"


def test_bootstrap_runtime_paths_rejects_partial_process_override(
    tmp_path,
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path / "explicit-memory"))

    with pytest.raises(RuntimeError, match="must be set together"):
        watchdog_sync._bootstrap_runtime_paths(tmp_path / "runtime_profiles.json")


def test_bootstrap_runtime_paths_rejects_relative_profile_roots_before_mutation(
    tmp_path,
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "runtime_profiles.json",
        {
            "VECTOR_LAKE_MEMORY_DIR": "memory",
            "VECTOR_LAKE_META_DIR": "memory/wiki/.meta",
        },
    )

    with pytest.raises(RuntimeError, match="must resolve to an absolute path"):
        watchdog_sync._bootstrap_runtime_paths(manifest)

    assert "VECTOR_LAKE_MEMORY_DIR" not in os.environ
    assert "VECTOR_LAKE_META_DIR" not in os.environ
    assert "VECTOR_LAKE_DB_PATH" not in os.environ


def test_bootstrap_runtime_paths_rejects_relative_process_overrides(
    tmp_path,
    monkeypatch,
):
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", "memory")
    monkeypatch.setenv("VECTOR_LAKE_META_DIR", "memory/wiki/.meta")
    manifest = _write_manifest(
        tmp_path / "runtime_profiles.json",
        {
            "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
            "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
        },
    )

    with pytest.raises(RuntimeError, match="must resolve to an absolute path"):
        watchdog_sync._bootstrap_runtime_paths(manifest)

    assert "VECTOR_LAKE_DB_PATH" not in os.environ


@pytest.mark.parametrize(
    ("manifest_payload", "expected_message"),
    [
        (
            {
                "schema_version": 1,
                "profiles": {
                    "default": {
                        "env": {"VECTOR_LAKE_MEMORY_DIR": "~/MEMORY"}
                    }
                },
            },
            "VECTOR_LAKE_META_DIR",
        ),
        ({}, "manifest is invalid"),
        (
            {
                "schema_version": 1,
                "profiles": {
                    "default": {
                        "env": {
                            "VECTOR_LAKE_MEMORY_DIR": "~/MEMORY",
                            "VECTOR_LAKE_META_DIR": "~/MEMORY/wiki/.meta",
                            "UNREVIEWED_SECRET": "value",
                        }
                    }
                },
            },
            "unsupported environment keys",
        ),
    ],
)
def test_bootstrap_runtime_paths_fails_closed_for_invalid_profile(
    tmp_path,
    monkeypatch,
    manifest_payload,
    expected_message,
):
    _clear_runtime_env(monkeypatch)
    manifest = tmp_path / "runtime_profiles.json"
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=expected_message):
        watchdog_sync._bootstrap_runtime_paths(manifest)
    assert "VECTOR_LAKE_MEMORY_DIR" not in os.environ
    assert "VECTOR_LAKE_META_DIR" not in os.environ
