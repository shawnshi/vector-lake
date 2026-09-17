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


def _config_documents() -> list[tuple[str, dict]]:
    """Tracked config documents present in this checkout.

    ``config.json`` is a per-machine file that is no longer tracked, so a fresh
    clone only has the ``config.example.json`` template.  Whatever is present
    (normally both) is what must stay portable.
    """
    import json

    documents: list[tuple[str, dict]] = []
    for name in ("config.json", "config.example.json"):
        path = ROOT / name
        if path.exists():
            documents.append((name, json.loads(path.read_text(encoding="utf-8"))))
    assert documents, "expected at least config.example.json to be present"
    return documents


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
    offenders = sorted(
        f"{name}:{key}"
        for name, config in _config_documents()
        for key, value in config.items()
        if key not in MACHINE_SPECIFIC_KEYS
        and isinstance(value, str)
        and PERSONAL_PATH.search(value)
    )
    assert not offenders, (
        f"config keys {offenders} contain a personal absolute path; "
        f"only {sorted(MACHINE_SPECIFIC_KEYS)} may be machine-specific"
    )


def test_missing_config_keeps_the_shipped_exclusions(tmp_path, monkeypatch):
    """A checkout without config.json must not lose the privacy exclusions."""
    from vector_lake import wiki_utils

    monkeypatch.setattr(wiki_utils, "get_extension_root", lambda: tmp_path)
    config = wiki_utils.load_config()
    assert config["exclude_paths"] == list(wiki_utils.DEFAULT_EXCLUDE_PATHS)
    assert config["supported_extensions"] == list(wiki_utils.DEFAULT_SUPPORTED_EXTENSIONS)


def test_config_target_directories_is_empty_by_default():
    offenders = [name for name, config in _config_documents() if config.get("target_directories") != []]
    assert not offenders, (
        f"{offenders} must ship an empty list so the default resolves to <MEMORY>/raw "
        "instead of one user's path"
    )


HOST_HOME_LITERAL = re.compile(r"\.gemini|\.codex")


def test_host_home_literals_live_in_one_module():
    """Host conventions are data in ``host_env``, not literals across modules.

    Before the extraction, ``~/.gemini`` appeared in three modules and
    ``~/.codex`` in two, with no single place to add a third host.
    """
    offenders = [
        f"{path.name}:{lineno}: {line.strip()}"
        for path in sorted((ROOT / "vector_lake").glob("*.py"))
        if path.name != "host_env.py"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if HOST_HOME_LITERAL.search(line)
    ]
    assert not offenders, f"host home literals escaped host_env.py: {offenders}"


def test_memory_dir_falls_back_to_the_legacy_host_root(monkeypatch):
    """Precedence: VECTOR_LAKE_MEMORY_DIR > config.json > host_env fallback."""
    from vector_lake import host_env, wiki_utils

    monkeypatch.delenv("VECTOR_LAKE_MEMORY_DIR", raising=False)
    # Neutralise the per-machine config.json so the third fallback is reachable.
    monkeypatch.setattr(wiki_utils, "_CONFIG_CACHE", {"memory_dir": None})

    assert wiki_utils.get_memory_dir() == host_env.legacy_memory_root()


def test_memory_dir_env_beats_the_configured_value(monkeypatch, tmp_path):
    from vector_lake import wiki_utils

    monkeypatch.setattr(wiki_utils, "_CONFIG_CACHE", {"memory_dir": tmp_path.parent})
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(tmp_path))

    assert wiki_utils.get_memory_dir() == tmp_path.resolve()
