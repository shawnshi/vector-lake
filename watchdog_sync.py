import sys
from pathlib import Path

from vector_lake.runtime_paths import bootstrap_runtime_paths

def _bootstrap_runtime_paths(config_path: Path | None = None) -> dict[str, str]:
    """Fill missing standalone runtime paths from the packaged MCP manifest."""
    return bootstrap_runtime_paths(config_path, caller="Watchdog")


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
