"""Guard against shipping machine-specific absolute paths in the package."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The genuinely per-machine setting: an explicit MEMORY root for this install.
# Every other file and key must stay path-free so the project remains portable.
MACHINE_SPECIFIC_KEYS = {"memory_dir"}

SOURCES = sorted(
    [
        *(ROOT / "vector_lake").glob("*.py"),
        *(ROOT / "scripts").glob("*.py"),
        ROOT / "cli.py",
        ROOT / "watchdog_sync.py",
        ROOT / "check_jobs.py",
        ROOT / "reset_jobs.py",
        ROOT / "mcp_config.json",
        ROOT / ".mcp.json",
    ]
)

# A concrete user's home directory baked into shipped source or config.
PERSONAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/](?!<)[^\\/\"']+|/home/[^/\"']+|/Users/[^/\"']+)")


def _config_paths_outside_allowlist() -> list[str]:
    """Absolute personal paths in config.json, excluding the allowed keys."""
    import json

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return sorted(
        key
        for key, value in config.items()
        if key not in MACHINE_SPECIFIC_KEYS
        and isinstance(value, str)
        and PERSONAL_PATH.search(value)
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_machine_specific_absolute_paths(path: Path):
    text = path.read_text(encoding="utf-8")
    offending = [
        line.strip()
        for line in text.splitlines()
        if PERSONAL_PATH.search(line) and "tempfile.mkdtemp" not in line
    ]
    assert not offending, f"{path.relative_to(ROOT)} contains a machine-specific path: {offending}"


def test_only_memory_dir_may_hold_a_machine_specific_path():
    offenders = _config_paths_outside_allowlist()
    assert not offenders, (
        f"config.json keys {offenders} contain a personal absolute path; "
        f"only {sorted(MACHINE_SPECIFIC_KEYS)} may be machine-specific"
    )


def test_config_target_directories_is_empty_by_default():
    import json

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert config.get("target_directories") == [], (
        "ship an empty list so the default resolves to <MEMORY>/raw instead of one user's path"
    )
