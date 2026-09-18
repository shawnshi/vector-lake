"""The command and configuration registries are documentation, so they drift silently.

Four lists describe overlapping surfaces -- argparse subcommands, MCP tools, ``tools.__all__``
and the README -- and nothing checked that they agree.  A command added to the CLI and left out
of the README is invisible to the operator who reads the README; a switch read in code and left
out of the config list is a behaviour nobody can find.

This guards the two pairs where drift is both likely and checkable:

* every ``python cli.py <name>`` in the README is a real subcommand, and every subcommand is
  named in the README;
* every ``VECTOR_LAKE_*`` literal in ``vector_lake/`` or ``scripts/`` is named in the README's
  configuration section (the MCP tool list is *not* guarded here: its entries are function names
  and there is no command-name mapping to compare against, so an assertion would be invented
  rather than measured).

Both checks are written as small pure functions over source text so the tests can feed them
synthetic input and prove they fire -- a registry guard that passes because it found nothing to
compare is worse than no guard.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

_SWITCH_LITERAL = re.compile(r'["\'](VECTOR_LAKE_[A-Z0-9_]+)["\']')
_README_COMMAND = re.compile(r"python cli\.py ([a-z0-9-]+)")
_README_SWITCH = re.compile(r"VECTOR_LAKE_[A-Z0-9_]+")


def cli_subcommands(cli_source: str) -> set[str]:
    """Every name passed to ``add_parser`` in the CLI's argparse build."""
    names = set()
    for node in ast.walk(ast.parse(cli_source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def documented_commands(readme: str) -> set[str]:
    return set(_README_COMMAND.findall(readme))


def switches_in(source: str) -> set[str]:
    """Switch names the source mentions as literals -- covers ``os.environ.get`` and helpers."""
    return set(_SWITCH_LITERAL.findall(source))


def documented_switches(readme: str) -> set[str]:
    return set(_README_SWITCH.findall(readme))


def _package_switches() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for base in ("vector_lake", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            for name in switches_in(path.read_text(encoding="utf-8", errors="ignore")):
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


# --- the registries agree today -------------------------------------------------


def test_every_cli_subcommand_is_documented():
    readme = _readme()
    commands = cli_subcommands((ROOT / "vector_lake" / "cli_app.py").read_text(encoding="utf-8"))

    assert len(commands) > 20, f"the parser should have found the subcommands, got {commands}"
    assert commands - documented_commands(readme) == set(), (
        "these subcommands exist but are not in the README: "
        f"{sorted(commands - documented_commands(readme))}"
    )


def test_every_documented_command_exists():
    readme = _readme()
    commands = cli_subcommands((ROOT / "vector_lake" / "cli_app.py").read_text(encoding="utf-8"))

    assert documented_commands(readme) - commands == set(), (
        "the README documents commands that do not exist: "
        f"{sorted(documented_commands(readme) - commands)}"
    )


def test_every_environment_switch_is_registered():
    documented = documented_switches(_readme())
    switches = _package_switches()

    assert len(switches) > 20, f"the scan should have found the switches, got {len(switches)}"
    missing = {name: sorted(where) for name, where in switches.items() if name not in documented}
    assert missing == {}, (
        "these switches are read but absent from the README's configuration list "
        f"(add them there, next to the other VECTOR_LAKE_* entries): {missing}"
    )


# --- and the guards can fail ----------------------------------------------------


def test_the_command_guard_fires_on_a_command_that_only_exists_in_code():
    readme = "```\npython cli.py sync\n```"
    assert cli_subcommands('subparsers.add_parser("brand-new", help="x")') == {"brand-new"}
    assert cli_subcommands('subparsers.add_parser("brand-new")') - documented_commands(readme)


def test_the_command_guard_fires_on_a_command_that_only_exists_in_docs():
    readme = "```\npython cli.py vanished\n```"
    assert documented_commands(readme) - cli_subcommands('subparsers.add_parser("sync")')


def test_the_switch_guard_fires_on_an_unregistered_switch():
    source = 'x = os.environ.get("VECTOR_LAKE_NOT_REGISTERED", "1")'
    source_via_helper = '_env_int("VECTOR_LAKE_ALSO_NOT_REGISTERED", 5)'
    readme = "`VECTOR_LAKE_MEMORY_DIR` is documented."

    assert switches_in(source) == {"VECTOR_LAKE_NOT_REGISTERED"}
    assert switches_in(source_via_helper) == {"VECTOR_LAKE_ALSO_NOT_REGISTERED"}
    assert switches_in(source) - documented_switches(readme)
    # And a registered one does not fire, so the guard is not simply always true.
    assert not (switches_in('os.environ.get("VECTOR_LAKE_MEMORY_DIR")') - documented_switches(readme))
