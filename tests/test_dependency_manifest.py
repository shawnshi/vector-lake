"""Guard the dependency manifest against drifting from the enforced floors.

Several floors are load-bearing facts discovered by running the packages, not
preferences, so they are asserted here rather than only written in a comment:

* ``mistune>=3.0.1`` - ``create_markdown(renderer='ast')`` raises
  ``TypeError: 'str' object is not callable`` on 3.0.0.
* ``python-louvain`` is gone; clustering is Leiden via leidenalg + igraph.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
LOCK = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
# Requirement lines only: drop comments and blanks.
REQUIRED_LINES = [
    line.strip()
    for line in REQUIREMENTS.splitlines()
    if line.strip() and not line.strip().startswith("#")
]


def _specifier(name: str) -> str | None:
    """The raw requirements.txt line declaring ``name``, if any."""
    for line in REQUIRED_LINES:
        if line.lower().startswith(name.lower()):
            return line
    return None


@pytest.mark.parametrize(
    "name",
    ["filelock", "networkx", "python-dotenv", "igraph", "leidenalg", "PyYAML",
     "watchdog", "google-genai", "mcp", "sqlite-vec", "mistune", "bm25s"],
)
def test_required_dependency_is_declared(name):
    assert _specifier(name) is not None, f"{name} missing from requirements.txt"


def test_python_louvain_is_removed_everywhere():
    """Only requirement/pin lines count; explanatory comments are ignored."""
    assert not [line for line in REQUIRED_LINES if "louvain" in line.lower()]
    pinned = [
        line.strip()
        for line in LOCK.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in pinned if "louvain" in line.lower()]
    # And nothing in the runtime may import it either.
    offenders = []
    for path in list((ROOT / "vector_lake").glob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "community import community_louvain" in text or "community_louvain." in text:
            offenders.append(path.name)
    assert not offenders, f"python-louvain still imported by: {offenders}"


@pytest.mark.parametrize(
    "name, minimum",
    [
        ("filelock", "3.15"),
        ("networkx", "3.2"),
        ("igraph", "0.11.0"),
        ("leidenalg", "0.10.0"),
        ("PyYAML", "6.0.1"),
        ("google-genai", "2.0.0"),
        ("sqlite-vec", "0.1.3"),
        ("bm25s", "0.2.0"),
    ],
)
def test_declared_floor_matches_the_requested_minimum(name, minimum):
    line = _specifier(name)
    assert line is not None
    assert f">={minimum}" in line.replace(" ", ""), f"{name} floor drifted: {line}"


def test_mcp_floor_is_on_the_2x_line():
    """The runtime imports MCPServer, which only exists from mcp 2.x.

    The former floor (``mcp>=1.0.0``) let a clean install resolve to 2.x while
    the code still imported the removed ``mcp.server.fastmcp`` module: that is
    exactly how CI went red on a fresh checkout, so the floor is load-bearing.
    """
    line = _specifier("mcp")
    assert line is not None
    assert line.replace(" ", "") in {"mcp>=2.1.0", "mcp>=2.1"}, (
        f"mcp floor must track the MCPServer API: {line}"
    )
    assert "fastmcp" not in line.lower()


def test_mistune_floor_is_above_the_broken_3_0_0():
    """3.0.0 fails at import time for the renderer this project uses."""
    line = _specifier("mistune")
    assert line is not None
    assert ">=3.0.1" in line.replace(" ", ""), (
        "mistune 3.0.0 breaks create_markdown(renderer='ast'); the floor must be 3.0.1"
    )


def test_every_required_package_is_pinned_in_the_lock():
    for line in REQUIRED_LINES:
        name = re.split(r"[><=~\s]", line, maxsplit=1)[0]
        assert re.search(rf"^{re.escape(name)}==(\S+)", LOCK, re.M), (
            f"{name} is required but not pinned in requirements.lock.txt"
        )
