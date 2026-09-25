#!/usr/bin/env python
"""Install a native-core build additively, so upgrading does not require stopping anything.

Why not just overwrite ``vector_lake_core/vector_lake_core.pyd`` (what was done twice on 2026-09-25):
Windows refuses to replace a loaded DLL, so every upgrade meant disabling the scheduled task, killing
the watchdog, the ingest runner and the MCP servers, installing, then restarting -- measured.  A PyO3
module cannot dodge this by renaming the file either: its init symbol is ``PyInit_<lib name>``, i.e.
fixed at build time, so the file must be called ``vector_lake_core.pyd``.

What works instead: keep one directory per build and let the *package directory* carry the version.

    site-packages/vector_lake_core/__init__.py      <- shim, selects one build (this script writes it)
    site-packages/vector_lake_core_0_2_0/vector_lake_core.pyd
    site-packages/vector_lake_core_0_1_0/vector_lake_core.pyd   (kept for rollback)

The loader matches the *last* path component (``vector_lake_core``) against the init symbol, so the
file inside may live in any versioned directory.  Installing is then a new file plus a pointer, which
touches nothing a running process holds open; consumers pick the new build up when they restart.

Usage:
    python scripts/install_core.py                 # build (MSVC) + install + activate
    python scripts/install_core.py --activate 0.1.0
    python scripts/install_core.py --list
    python scripts/install_core.py --check
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CRATE = REPO / "crates" / "vector_lake_core"
DLL = CRATE / "target" / "release" / "vector_lake_core.dll"
PYPROJECT = CRATE / "pyproject.toml"
POINTER = "_active_version.txt"

try:  # the target this host needs; the default GNU toolchain cannot link here (no -lgcc/-lgcc_eh)
    from vector_lake.wiki_utils import get_meta_dir  # noqa: F401  (import proves the package loads)
except Exception:  # pragma: no cover - the script must also run standalone
    pass

SHIM = '''"""Active native-core build.  Written by ``scripts/install_core.py`` -- do not edit by hand.

One directory per build (``vector_lake_core_<version>/vector_lake_core.pyd``), because a PyO3
extension's init symbol is fixed at build time and a loaded DLL cannot be replaced on Windows.  This
shim re-exports exactly one build, so upgrading is additive and rollback is one pointer change.

Selection order: ``VECTOR_LAKE_CORE_VERSION`` (explicit) -> ``_active_version.txt`` (what the
installer activated).  A named build that fails to import raises instead of silently falling back:
a quiet fallback to a different build is the failure mode this whole mechanism was built to stop.
"""
import importlib as _importlib
import os as _os
from pathlib import Path as _Path

_DEFAULT = "{default}"


def _selected() -> str:
    explicit = _os.environ.get("VECTOR_LAKE_CORE_VERSION", "").strip()
    if explicit:
        return explicit
    pointer = _Path(__file__).parent / "{pointer}"
    if pointer.exists():
        try:
            return pointer.read_text(encoding="utf-8").strip() or _DEFAULT
        except OSError:
            pass
    return _DEFAULT


_VERSION = _selected()
_MODULE = f"vector_lake_core_{{_VERSION.replace('.', '_')}}.vector_lake_core"
try:
    _impl = _importlib.import_module(_MODULE)
except Exception as exc:  # noqa: BLE001 - the message is the value here
    raise ImportError(
        f"vector_lake_core {{_VERSION}} is not importable from {{_MODULE!r}}: {{type(exc).__name__}}: {{exc}}. "
        "Installed builds: " + ", ".join(
            sorted(p.name for p in _Path(__file__).parent.parent.glob("vector_lake_core_*"))
        )
    ) from exc
core_version = _VERSION
globals().update({{k: v for k, v in vars(_impl).items() if not k.startswith("__")}})


def __getattr__(name):
    return getattr(_impl, name)
'''


def source_version() -> str:
    """The version in the crate's pyproject.toml -- the source of truth, not the installed one."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise SystemExit(f"no version in {PYPROJECT}")
    return match.group(1)


def site_packages() -> Path:
    import site

    for candidate in (*site.getsitepackages(), site.getusersitepackages()):
        path = Path(candidate) / "vector_lake_core"
        if path.exists():
            return Path(candidate)
    raise SystemExit("vector_lake_core is not installed in any site-packages; install the wheel first")


def build() -> Path:
    """Build the cdylib with the toolchain that can link on this host."""
    toolchain = "stable-x86_64-pc-windows-msvc" if sys.platform == "win32" else None
    argv = ["cargo"]
    if toolchain:
        argv.append(f"+{toolchain}")
    argv += ["build", "--release"]
    print(f"building: {' '.join(argv)} (in {CRATE})")
    result = subprocess.run(argv, cwd=CRATE, capture_output=True, text=True)
    if result.returncode != 0:
        # The GNU toolchain's failure is not obvious from "linker failed"; say what to use.
        tail = (result.stderr or "").strip().splitlines()[-6:]
        raise SystemExit("build failed:\n  " + "\n  ".join(tail))
    if not DLL.exists():
        raise SystemExit(f"build reported success but {DLL} is missing")
    return DLL


def installed_versions() -> list[str]:
    """Dotted versions found on disk.

    The *directory* label must use underscores (``vector_lake_core_0_2_0``): the import system reads a
    dot in a path component as a separator, so ``vector_lake_core_0.2.0`` would not be importable as
    one package.  Callers think in dotted versions; the conversion lives here.
    """
    parent = site_packages()
    versions = [
        path.name.removeprefix("vector_lake_core_").replace("_", ".")
        for path in sorted(parent.glob("vector_lake_core_*"))
        if (path / "vector_lake_core.pyd").exists()
    ]
    return sorted(versions, key=lambda v: [int(part) for part in re.findall(r"\d+", v)] or [0])


def active_version() -> str | None:
    pointer = site_packages() / "vector_lake_core" / POINTER
    if pointer.exists():
        try:
            return pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def write_shim(default: str) -> Path:
    target = site_packages() / "vector_lake_core" / "__init__.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": the working copy of every repo file is LF, and Python's text mode would write
    # CRLF on Windows (the same mistake was made twice on 2026-09-25 with source files).
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(SHIM.format(default=default, pointer=POINTER))
    return target


def activate(version: str, *, check: bool = True) -> None:
    if version not in installed_versions():
        raise SystemExit(f"{version} is not installed; have: {installed_versions() or 'none'}")
    pointer = site_packages() / "vector_lake_core" / POINTER
    with open(pointer, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(version + "\n")
    if check:
        probe = subprocess.run(
            [sys.executable, "-c",
             "import vector_lake_core as c; print(c.core_version, c.version(),"
             " getattr(c, 'blocks_contract', lambda: 'none')())"],
            capture_output=True, text=True,
        )
        print(f"activated {version}: {probe.stdout.strip() or probe.stderr.strip()[-300:]}")


def install(dll: Path, version: str) -> Path:
    """Copy the build into its own versioned directory -- a new file, never an overwrite."""
    target_dir = site_packages() / f"vector_lake_core_{version.replace('.', '_')}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "vector_lake_core.pyd"
    shutil.copy2(dll, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true", help="install the existing build artifact")
    parser.add_argument("--activate", metavar="VERSION", help="switch the pointer only")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--check", action="store_true", help="report the active build and exit")
    args = parser.parse_args()

    if args.list:
        print(f"installed: {installed_versions() or 'none'}")
        print(f"active   : {active_version() or 'unset'}")
        return 0

    if args.check:
        import vector_lake_core as core

        print(f"core_version   : {getattr(core, 'core_version', 'unset (direct .pyd install)')}")
        print(f"version()      : {core.version()}")
        print(f"blocks_contract: {getattr(core, 'blocks_contract', lambda: 'absent')()}")
        print(f"file           : {core.__file__}")
        return 0

    if args.activate:
        activate(args.activate)
        return 0

    version = source_version()
    dll = DLL if args.no_build else build()
    if not dll.exists():
        raise SystemExit(f"{dll} does not exist; run without --no-build")
    target = install(dll, version)
    write_shim(version)
    print(f"installed {version} -> {target}")
    activate(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
