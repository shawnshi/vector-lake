"""Static scope check: no function may reference an unbound name.

This is the regression guard for the class of defect found in
``watchdog_app.index_worker_loop``, where a block referenced ``transaction()``
and ``sync_pages_to_canonical()`` without importing them.  The block was only
reachable from a filesystem event, so no test could catch it and every manual
Markdown edit silently failed with ``NameError``.

Per-function analysis is lexical: names bound in an enclosing function,
including nested-closure capture, are treated as bound.
"""
import ast
import builtins
import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGETS = sorted(
    [
        *(ROOT / "vector_lake").glob("*.py"),
        *(ROOT / "scripts").glob("*.py"),
        ROOT / "check_jobs.py",
        ROOT / "reset_jobs.py",
        ROOT / "cli.py",
        ROOT / "watchdog_sync.py",
    ]
)


def _targets(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child.id


def _bound_in_scope(node) -> set[str]:
    """Names bound by executing this node in its own scope."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ImportFrom):
            for alias in child.names:
                names.add(alias.asname or alias.name)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, ast.arg):
            names.add(child.arg)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, ast.Global) or isinstance(child, ast.Nonlocal):
            names.update(child.names)
    return names


def _module_names(tree) -> set[str]:
    names = set(dir(builtins))
    for child in ast.walk(tree):
        if isinstance(child, ast.Import):
            for alias in child.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ImportFrom):
            if any(alias.name == "*" for alias in child.names):
                names.add("*")  # star import: scope unknowable
            for alias in child.names:
                names.add(alias.asname or alias.name)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                names.update(_targets(target))
        elif isinstance(child, ast.AnnAssign):
            names.update(_targets(child.target))
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            names.update(_targets(child.target))
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.Global):
            names.update(child.names)
    return names


def free_names(source: str) -> list[tuple[str, int, list[str]]]:
    tree = ast.parse(source)
    module_names = _module_names(tree)
    if "*" in module_names:
        return []

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    problems: list[tuple[str, int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visible = set(module_names)
        scope: ast.AST | None = node
        while scope is not None:
            visible |= _bound_in_scope(scope)
            scope = parents.get(scope)
            while scope is not None and not isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module, ast.Lambda)
            ):
                scope = parents.get(scope)
        loaded = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
        }
        missing = sorted(loaded - visible)
        if missing:
            problems.append((node.name, node.lineno, missing))
    return problems


def use_before_local_import(source: str) -> list[tuple[str, str, int, int]]:
    """Function-local imports whose name is read earlier in the same function.

    ``free_names`` treats every name bound anywhere in a function as visible for
    its whole body, so it cannot see Python's real binding rule: an ``import``
    inside a function makes the name *local to the entire function*.  A read
    that executes before that import statement raises ``UnboundLocalError``.
    That is exactly how ``prepare_ingest_batch`` lost its scan config and then
    crashed on every non-empty batch.

    Reads lexically inside a nested function/class/lambda are ignored: those
    bodies may legitimately execute after the import line.
    """
    tree = ast.parse(source)
    problems: list[tuple[str, str, int, int]] = []

    def _direct_statements(fn) -> list[ast.AST]:
        """Statements executed directly in this function, excluding nested scopes."""
        collected: list[ast.AST] = []
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            collected.append(node)
            stack.extend(ast.iter_child_nodes(node))
        return sorted(collected, key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0)))

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        statements = _direct_statements(fn)
        imported: dict[str, int] = {}
        for node in statements:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [(alias.asname or alias.name).split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [alias.asname or alias.name for alias in node.names]
            for name in names:
                # The binding runs at the *first* local import of that name in
                # source order; a later re-import cannot make an earlier read legal.
                imported[name] = min(imported.get(name, node.lineno), node.lineno)
        if not imported:
            continue
        for node in statements:
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                continue
            import_line = imported.get(node.id)
            if import_line is not None and node.lineno < import_line:
                problems.append((fn.name, node.id, node.lineno, import_line))
    return problems


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_use_before_function_local_import(path: pathlib.Path):
    problems = use_before_local_import(path.read_text(encoding="utf-8"))
    assert not problems, "; ".join(
        f"{name}() line {line} reads '{symbol}' before its local import at line {import_line}"
        for name, symbol, line, import_line in problems
    )


def test_use_before_local_import_detects_the_prepare_ingest_defect():
    """The guard must fail on the exact shape that broke ingest."""
    broken = '''
def prepare(config_path):
    try:
        with open(config_path) as handle:
            config = json.load(handle)
    except Exception:
        config = {}
    if not config:
        return False
    import json
    return json.dumps(config)
'''
    problems = use_before_local_import(broken)
    assert problems == [("prepare", "json", 5, 10)]


def test_use_before_local_import_allows_import_then_use():
    healthy = '''
def prepare(config_path):
    import json

    def render():
        return json.dumps({"ok": True})

    with open(config_path) as handle:
        return render(json.load(handle))
'''
    assert use_before_local_import(healthy) == []


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_function_references_an_unbound_name(path: pathlib.Path):
    problems = free_names(path.read_text(encoding="utf-8"))
    assert not problems, "; ".join(
        f"{name}() line {lineno} references {missing}" for name, lineno, missing in problems
    )


def test_checker_detects_the_original_defect():
    """The checker must actually fail on the pre-fix watchdog code shape."""
    broken = """
def worker():
    try:
        pass
    except Exception:
        pass
    for name in ('a',):
        with transaction():
            sync_pages_to_canonical([name])
"""
    problems = free_names(broken)
    assert problems and sorted(problems[0][2]) == ["sync_pages_to_canonical", "transaction"]


def test_checker_accepts_nested_closure_capture():
    nested = """
def outer(processed_data):
    def inner():
        return processed_data.get("x")
    return inner
"""
    assert free_names(nested) == []
