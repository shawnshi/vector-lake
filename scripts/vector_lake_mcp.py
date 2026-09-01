"""Host-neutral Vector Lake MCP launcher rooted at this plugin checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence


_SURFACES = ("full", "memory", "readonly")


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the Vector Lake MCP server")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--surface", choices=_SURFACES, default="full")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plugin_root = _plugin_root()
    root_text = str(plugin_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    profile_path = plugin_root / "runtime_profiles.json"
    os.environ["VECTOR_LAKE_RUNTIME_PROFILE"] = args.profile
    os.environ["VECTOR_LAKE_RUNTIME_PROFILE_PATH"] = str(profile_path)
    os.environ["VECTOR_LAKE_MCP_SURFACE"] = args.surface

    from vector_lake.runtime_paths import bootstrap_runtime_paths

    bootstrap_runtime_paths(profile_path, caller="MCP launcher", profile=args.profile)

    from vector_lake.mcp_server import main as run_mcp_server

    run_mcp_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
