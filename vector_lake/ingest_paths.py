"""Ingest root and configuration resolution.

Depends only on the base layer, so storage-tier callers can resolve ingest roots
without importing the ingest engine that lives in ``tool_ingest``. That import was
the reason ``db_store`` pointed at a handler, and it is also the first piece of the
larger split that ``tool_ingest`` still needs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from vector_lake import get_extension_root
from vector_lake.wiki_utils import get_raw_dir


def load_ingest_config() -> dict:
    """Load and validate ``config.json``, failing closed on malformed values."""
    config_path = get_extension_root() / "config.json"
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ingest_config_invalid:{config_path}:{type(exc).__name__}:{exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"ingest_config_invalid:{config_path}:root_must_be_object")
    for field_name in (
        "target_directories",
        "exclude_paths",
        "supported_extensions",
    ):
        value = loaded.get(field_name)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise RuntimeError(
                f"ingest_config_invalid:{config_path}:{field_name}_must_be_string_list"
            )
    return loaded


def get_ingest_target_directories(
    config: dict | None = None,
    *,
    collapse_nested: bool = False,
) -> list[Path]:
    """Resolve every ingest root and optionally collapse nested watch trees."""
    loaded = load_ingest_config() if config is None else config
    candidates = []
    for configured in loaded.get("target_directories", []):
        configured_path = Path(configured)
        candidates.append(
            (
                configured_path
                if configured_path.is_absolute()
                else get_extension_root() / configured_path
            ).resolve()
        )
    candidates.append(get_raw_dir().resolve())
    unique = []
    seen = set()
    for candidate in candidates:
        identity = os.path.normcase(str(candidate))
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    if not collapse_nested:
        return unique
    collapsed = []
    for candidate in sorted(
        unique,
        key=lambda value: (len(value.parts), os.path.normcase(str(value))),
    ):
        if any(
            candidate == parent or candidate.is_relative_to(parent)
            for parent in collapsed
        ):
            continue
        collapsed.append(candidate)
    return collapsed
