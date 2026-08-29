"""Fail-closed runtime-path authority bootstrap for standalone entrypoints."""

from __future__ import annotations

import json
import os
from pathlib import Path


MCP_SERVER_NAME = "vector-lake-mcp"
RUNTIME_PATH_KEYS = (
    "VECTOR_LAKE_MEMORY_DIR",
    "VECTOR_LAKE_META_DIR",
)
DATABASE_PATH_KEY = "VECTOR_LAKE_DB_PATH"


def _bind_default_database_path() -> None:
    """Prevent a later dotenv load from replacing the selected database root."""
    if os.environ.get(DATABASE_PATH_KEY, "").strip():
        return
    meta_dir = os.environ.get("VECTOR_LAKE_META_DIR", "").strip()
    if not meta_dir:
        raise RuntimeError("Vector Lake runtime meta path is unavailable")
    os.environ[DATABASE_PATH_KEY] = str(
        Path(meta_dir).expanduser() / "vector_lake.db"
    )


def bootstrap_runtime_paths(
    config_path: str | os.PathLike[str] | None = None,
    *,
    caller: str = "Vector Lake",
) -> dict[str, str]:
    """Bind a complete explicit pair or atomically load the packaged manifest."""
    explicit = {
        key: os.environ.get(key, "").strip()
        for key in RUNTIME_PATH_KEYS
        if os.environ.get(key, "").strip()
    }
    if len(explicit) == len(RUNTIME_PATH_KEYS):
        _bind_default_database_path()
        return {}
    if explicit:
        missing = next(key for key in RUNTIME_PATH_KEYS if key not in explicit)
        raise RuntimeError(
            f"{caller} runtime path overrides must be set together; missing {missing}"
        )

    manifest_path = (
        Path(config_path)
        if config_path is not None
        else Path(__file__).resolve().parents[1] / ".mcp.json"
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        configured_env = payload["mcpServers"][MCP_SERVER_NAME]["env"]
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{caller} runtime path manifest not found: {manifest_path}"
        ) from exc
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"{caller} runtime path manifest is invalid: {manifest_path}"
        ) from exc

    configured: dict[str, str] = {}
    for key in RUNTIME_PATH_KEYS:
        value = configured_env.get(key) if isinstance(configured_env, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"{caller} runtime path manifest is missing {key}: {manifest_path}"
            )
        configured[key] = value.strip()

    for key, value in configured.items():
        os.environ[key] = value
    _bind_default_database_path()
    return configured
