import json
import os
import sys
from pathlib import Path


_MCP_SERVER_NAME = "vector-lake-mcp"
_RUNTIME_PATH_KEYS = (
    "VECTOR_LAKE_MEMORY_DIR",
    "VECTOR_LAKE_META_DIR",
)


def _bootstrap_runtime_paths(config_path: Path | None = None) -> dict[str, str]:
    """Fill missing standalone runtime paths from the packaged MCP manifest."""
    explicit = {
        key: os.environ.get(key, "").strip()
        for key in _RUNTIME_PATH_KEYS
        if os.environ.get(key, "").strip()
    }
    if len(explicit) == len(_RUNTIME_PATH_KEYS):
        return {}
    if explicit:
        missing = next(key for key in _RUNTIME_PATH_KEYS if key not in explicit)
        raise RuntimeError(
            "Watchdog runtime path overrides must be set together; "
            f"missing {missing}"
        )

    manifest_path = (
        Path(config_path)
        if config_path is not None
        else Path(__file__).resolve().with_name(".mcp.json")
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        configured_env = payload["mcpServers"][_MCP_SERVER_NAME]["env"]
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Watchdog runtime path manifest not found: {manifest_path}"
        ) from exc
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Watchdog runtime path manifest is invalid: {manifest_path}"
        ) from exc

    configured = {}
    for key in _RUNTIME_PATH_KEYS:
        value = configured_env.get(key) if isinstance(configured_env, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"Watchdog runtime path manifest is missing {key}: {manifest_path}"
            )
        configured[key] = value

    applied = {}
    for key, value in configured.items():
        os.environ[key] = value
        applied[key] = value
    return applied


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--stop"]):
        raise SystemExit("usage: watchdog_sync.py [--stop]")

    _bootstrap_runtime_paths()
    from vector_lake.watchdog_app import request_watchdog_stop, start_watchdog

    if arguments == ["--stop"]:
        print(f"Watchdog stop requested: {request_watchdog_stop()}")
    else:
        start_watchdog()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
