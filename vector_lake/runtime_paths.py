"""Fail-closed runtime-profile bootstrap for standalone entrypoints."""

from __future__ import annotations

import json
import os
from pathlib import Path


RUNTIME_PROFILE_SCHEMA_VERSION = 1
DEFAULT_RUNTIME_PROFILE = "default"
RUNTIME_PROFILE_ENV = "VECTOR_LAKE_RUNTIME_PROFILE"
RUNTIME_PROFILE_PATH_ENV = "VECTOR_LAKE_RUNTIME_PROFILE_PATH"
RUNTIME_PATH_KEYS = (
    "VECTOR_LAKE_MEMORY_DIR",
    "VECTOR_LAKE_META_DIR",
)
RUNTIME_PROFILE_ENV_KEYS = frozenset(
    {
        *RUNTIME_PATH_KEYS,
        "VECTOR_LAKE_OPERATIONAL_MEMORY_FTS",
        "VECTOR_LAKE_DURABILITY_PROFILE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
    }
)
DATABASE_PATH_KEY = "VECTOR_LAKE_DB_PATH"


def _require_absolute_runtime_roots(
    values: dict[str, str],
    *,
    caller: str,
    source: str,
) -> None:
    """Reject roots whose meaning would change with the process CWD."""
    for key in RUNTIME_PATH_KEYS:
        value = values.get(key, "")
        if not Path(value).expanduser().is_absolute():
            raise RuntimeError(
                f"{caller} {source} {key} must resolve to an absolute path"
            )


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


def _runtime_profile_path(
    config_path: str | os.PathLike[str] | None,
) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser()
    configured = os.environ.get(RUNTIME_PROFILE_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "runtime_profiles.json"


def _load_runtime_profile(
    manifest_path: Path,
    profile_name: str,
    *,
    caller: str,
) -> dict[str, str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{caller} runtime profile manifest not found: {manifest_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{caller} runtime profile manifest is invalid: {manifest_path}"
        ) from exc

    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != RUNTIME_PROFILE_SCHEMA_VERSION
    ):
        raise RuntimeError(
            f"{caller} runtime profile manifest is invalid: {manifest_path}"
        )
    profiles = payload.get("profiles")
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    configured_env = profile.get("env") if isinstance(profile, dict) else None
    if not isinstance(configured_env, dict):
        raise RuntimeError(
            f"{caller} runtime profile is missing or invalid: {profile_name}"
        )

    unknown = sorted(set(configured_env) - RUNTIME_PROFILE_ENV_KEYS)
    if unknown:
        raise RuntimeError(
            f"{caller} runtime profile contains unsupported environment keys: "
            + ", ".join(unknown)
        )

    configured: dict[str, str] = {}
    for key, value in configured_env.items():
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"{caller} runtime profile has an invalid value for {key}: "
                f"{manifest_path}"
            )
        configured[key] = value.strip()
    for key in RUNTIME_PATH_KEYS:
        if key not in configured:
            raise RuntimeError(
                f"{caller} runtime profile is missing {key}: {manifest_path}"
            )
    _require_absolute_runtime_roots(
        configured,
        caller=caller,
        source=f"runtime profile {profile_name!r}",
    )
    return configured


def bootstrap_runtime_paths(
    config_path: str | os.PathLike[str] | None = None,
    *,
    caller: str = "Vector Lake",
    profile: str | None = None,
) -> dict[str, str]:
    """Apply one validated profile while preserving complete process overrides."""
    explicit_paths = {
        key: os.environ.get(key, "").strip()
        for key in RUNTIME_PATH_KEYS
        if os.environ.get(key, "").strip()
    }
    if explicit_paths and len(explicit_paths) != len(RUNTIME_PATH_KEYS):
        missing = next(key for key in RUNTIME_PATH_KEYS if key not in explicit_paths)
        raise RuntimeError(
            f"{caller} runtime path overrides must be set together; missing {missing}"
        )
    if explicit_paths:
        _require_absolute_runtime_roots(
            explicit_paths,
            caller=caller,
            source="runtime path override",
        )

    profile_name = str(
        profile
        if profile is not None
        else os.environ.get(RUNTIME_PROFILE_ENV, DEFAULT_RUNTIME_PROFILE)
    ).strip()
    if not profile_name:
        raise RuntimeError(f"{caller} runtime profile name must be non-empty")
    manifest_path = _runtime_profile_path(config_path)
    configured = _load_runtime_profile(
        manifest_path,
        profile_name,
        caller=caller,
    )

    applied: dict[str, str] = {}
    for key, value in configured.items():
        if os.environ.get(key, "").strip():
            continue
        os.environ[key] = value
        applied[key] = value
    _bind_default_database_path()
    return applied
