"""Load repository-owned text templates; never interpret inserted data as a template."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from vector_lake import get_extension_root


_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def read_template(name: str, *, root: Path | None = None) -> str:
    """Read a UTF-8 template beneath the extension's templates directory."""
    relative = PurePosixPath(name.replace("\\", "/"))
    if not name or relative.is_absolute() or PureWindowsPath(name).drive or ".." in relative.parts:
        raise ValueError(f"template path must be relative and contained: {name!r}")
    directory = ((root if root is not None else get_extension_root()) / "templates").resolve()
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError(f"template path escapes templates: {name!r}")
    return path.read_text(encoding="utf-8")


def render_template_text(template: str, /, **values: object) -> str:
    """Validate source placeholders, then substitute once (not recursively)."""
    tokens = set(_VARIABLE.findall(template))
    remainder = _VARIABLE.sub("", template)
    # Adjacent closing braces can be ordinary nested JSON, not a placeholder.
    if "{{" in remainder:
        raise ValueError("invalid template placeholder; expected {{identifier}}")
    missing = tokens - values.keys()
    unused = values.keys() - tokens
    if missing or unused:
        raise ValueError(f"template variables mismatch: missing={sorted(missing)}, unused={sorted(unused)}")
    return _VARIABLE.sub(lambda match: str(values[match.group(1)]), template)


def render_template(name: str, *, root: Path | None = None, **values: object) -> str:
    """Load and render a repository template, failing closed on missing inputs."""
    return render_template_text(read_template(name, root=root), **values)
