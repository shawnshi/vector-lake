"""Module layering contract for vector_lake.

The package is one 37-module strongly connected component: every module can reach
every other, so no module can be extracted, tested or replaced on its own, and the
blast radius of any change is the whole package. This test states the target
layering and freezes the violations that exist today so each refactor batch can
shrink the list and never grow it.

It is deliberately a test rather than an import-linter configuration: this
repository hash-locks its dependencies, so a new build-time dependency would mean
regenerating three lock files and the CI install, which costs more than the check
is worth.

Two ratchets:

* ``ALLOWED_BACKWARD_EDGES`` -- dependencies that point upward, against the
  declared layer order. It must shrink, never grow.
* ``MAX_STRONGLY_CONNECTED_COMPONENT`` -- the size of the largest cycle cluster.
  It starts at 37 and must fall as batches land.

Removing an entry here is the definition of done for a P3 batch.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "vector_lake"

# Bottom-up. A module may only import from its own layer or from a layer below it.
LAYERS: dict[str, set[str]] = {
    "base": {
        "runtime_paths",
        "wiki_utils",
        "yaml_utils",
        "durability",
        "cancellation",
        "memory_protocol",
        "tokenizer_runtime",
    },
    "storage": {
        "db_store",
        "governance_store",
        "projection_store_v2",
        "projection_format_v2",
        "raw_revision",
        "storage_growth",
    },
    "domain": {
        "schema_validator",
        "purpose_contract",
        "claim_extractor",
        "claim_assessment",
        "semantic_merge",
        "merge_analysis",
        "timeline_semantics",
        "source_references",
        "quality_registry",
        "decision_registry",
        "evidence_foundation",
        "provenance",
        "provenance_retention",
        "search_projection_contract",
        "operational_memory_contract",
        "raw_scrub_contract",
        "skeleton_parser",
        "memory_search_normalization",
        "index_snapshot",
        "defense_hook",
        "retrieval_benchmark",
        "native_llm",
    },
    "derived": {
        "indexer",
        "runtime_health",
        "governance_metrics",
        "backup_capacity",
        "restore_snapshot",
        "diagnostic_snapshot",
        "topology_worker",
        "embedding_scheduler",
    },
    "orchestration": {
        "mutation_coordinator",
        "watchdog_app",
        "watchdog_status",
        "auto_ingest_worker",
        "ingest_worker",
        "heavy_task_gate",
        "tools",
    },
}
# Handlers and the CLI/MCP surface sit on top; they are derived from the filename
# rather than listed, so a new tool cannot silently escape the order.
HANDLER_PREFIXES = ("tool_",)
HANDLER_EXTRAS = {
    "governance_service",
    "auto_ingest_runners.base",
    "auto_ingest_runners.codex_exec",
    "auto_ingest_runners.host_relay",
}
SURFACE = {"mcp_server", "cli_app"}

ORDER = ["base", "storage", "domain", "derived", "orchestration", "handler", "surface"]


def _layer_of(module: str) -> str | None:
    for layer in ORDER:
        if module in LAYERS.get(layer, ()):
            return layer
    if module.startswith(HANDLER_PREFIXES) or module in HANDLER_EXTRAS:
        return "handler"
    if module in SURFACE:
        return "surface"
    return None


# Frozen on 2026-09-14: 43 intra-package edges violate the order above. Each entry
# is a P3 work item; delete the line when the edge is gone.
#
# This number is the third one measured. The audit said 38, then 39, and both were
# wrong for the same reason: module names were truncated to their top level, which
# hid auto_ingest_worker -> auto_ingest_runners.base and, once dotted names were
# kept, four more edges (governance_store/provenance -> governance_metrics,
# provenance_retention -> tool_claim_provenance, restore_snapshot ->
# tool_projection). The lesson is in the gate itself: resolve imports to real
# modules, never to symbol names or top-level prefixes.
ALLOWED_BACKWARD_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        # storage -> domain (10): mostly pure helpers living one layer too high.
        ("db_store", "native_llm"),
        ("db_store", "search_projection_contract"),
        ("governance_store", "claim_extractor"),
        ("governance_store", "decision_registry"),
        ("governance_store", "evidence_foundation"),
        ("governance_store", "memory_search_normalization"),
        ("governance_store", "operational_memory_contract"),
        ("governance_store", "provenance_retention"),
        ("governance_store", "source_references"),
        ("projection_format_v2", "search_projection_contract"),
        # orchestration -> handler (6)
        ("auto_ingest_worker", "auto_ingest_runners.base"),
        ("auto_ingest_worker", "tool_ingest"),
        ("ingest_worker", "tool_ingest"),
        ("tools", "tool_registry"),
        ("watchdog_app", "tool_governance_maintenance"),
        ("watchdog_app", "tool_ingest"),
        ("watchdog_app", "tool_lint"),
        # storage -> handler (3)
        ("db_store", "tool_ingest"),
        ("db_store", "tool_timeline"),
        ("governance_store", "tool_timeline"),
        # domain -> derived (3)
        ("claim_assessment", "governance_metrics"),
        ("provenance_retention", "governance_metrics"),
        ("schema_validator", "indexer"),
        # derived -> handler (3)
        ("runtime_health", "tool_auto_ingest"),
        ("runtime_health", "tool_gc"),
        ("runtime_health", "tool_timeline"),
        # base -> handler (3)
        ("memory_protocol", "tool_memory"),
        ("memory_protocol", "tool_query"),
        ("memory_protocol", "tool_search"),
        # storage -> derived (2)
        ("db_store", "backup_capacity"),
        ("storage_growth", "backup_capacity"),
        # base -> derived (2)
        ("memory_protocol", "indexer"),
        ("memory_protocol", "runtime_health"),
        # base -> domain (2)
        ("wiki_utils", "defense_hook"),
        ("wiki_utils", "schema_validator"),
        # found only after imports were resolved to real modules (storage -> derived)
        ("governance_store", "governance_metrics"),
        ("provenance", "governance_metrics"),
        ("provenance_retention", "tool_claim_provenance"),
        ("restore_snapshot", "tool_projection"),
        # singletons
        ("retrieval_benchmark", "tool_search"),
        ("runtime_health", "watchdog_status"),
        ("tool_doctor", "mcp_server"),
        ("wiki_utils", "mutation_coordinator"),
    }
)
assert len(ALLOWED_BACKWARD_EDGES) == 43, len(ALLOWED_BACKWARD_EDGES)

# Frozen on 2026-09-14, re-measured once imports resolved to real modules.
# Must fall as P3 batches land.
MAX_STRONGLY_CONNECTED_COMPONENT = 39


def _module_name(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix().removesuffix(".py").replace("/", ".")


def _is_package_init(module: str) -> bool:
    return module == "__init__" or module.endswith(".__init__")


def _known_modules() -> set[str]:
    """Every importable module in the package, excluding package inits."""
    return {
        _module_name(path)
        for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
        and not _is_package_init(_module_name(path))
    }


def _resolve_target(candidate: str, package: str, known: set[str]) -> str:
    """Pick the module an import actually names.

    ``from vector_lake.pkg import base`` may name the submodule ``pkg.base`` or the
    attribute ``pkg.base``; ``from vector_lake.mod import SYMBOL`` names the module
    ``mod`` plus a symbol. Only names that resolve to a real module are kept,
    otherwise a symbol would be recorded as a module and could hide a violation or
    invent one.
    """
    if candidate in known:
        return candidate
    if package and package in known:
        return package
    return candidate


def _imports() -> dict[str, set[str]]:
    """Intra-package imports, including imports deferred inside function bodies.

    Targets are module names, not symbols, and stay fully qualified: truncating to
    the top-level package name would let ``auto_ingest_runners.base`` pass as
    ``auto_ingest_runners`` and escape the declared order.
    """
    known = _known_modules()
    graph: dict[str, set[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        if _is_package_init(module):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # Do not shadow ``module``: it still holds this file's own name,
                # which keys the graph.
                imported_from = node.module or ""
                if not imported_from.startswith("vector_lake"):
                    continue
                package = imported_from.removeprefix("vector_lake").lstrip(".")
                if package:
                    targets.add(_resolve_target(package, "", known))
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    candidate = f"{package}.{alias.name}" if package else alias.name
                    targets.add(_resolve_target(candidate, package, known))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("vector_lake"):
                        targets.add(alias.name.removeprefix("vector_lake."))
        # The bare package name targets the package itself, which has no layer.
        graph[module] = {t for t in targets if t and t != module and t != "vector_lake"}
    return graph


def _largest_cycle(graph: dict[str, set[str]]) -> int:
    """Size of the largest strongly connected component (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = [0]
    sizes: list[int] = []

    def strong(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in graph.get(node, ()):
            if nxt not in index:
                strong(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            size = 0
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                size += 1
                if popped == node:
                    break
            sizes.append(size)

    for node in graph:
        if node not in index:
            strong(node)
    return max(sizes) if sizes else 0


def test_every_module_has_a_declared_layer():
    """An unassigned module escapes the order silently, so it is an error."""
    unassigned = sorted(
        module for module in _imports() if _layer_of(module) is None
    )
    assert unassigned == [], f"modules missing a layer: {unassigned}"


def test_no_new_backward_dependency_edges():
    """Dependencies must point down the declared order, except the frozen list."""
    graph = _imports()
    backward: set[tuple[str, str]] = set()
    for module, targets in graph.items():
        layer = _layer_of(module)
        if layer is None:
            continue
        for target in targets:
            target_layer = _layer_of(target)
            if target_layer is None:
                continue
            if ORDER.index(target_layer) > ORDER.index(layer):
                backward.add((module, target))
    introduced = sorted(backward - ALLOWED_BACKWARD_EDGES)
    assert introduced == [], (
        "new backward dependency edges were introduced: " + repr(introduced)
    )
    # Ratchet in the other direction: a batch must delete its entries here.
    resolved = sorted(ALLOWED_BACKWARD_EDGES - backward)
    assert resolved == [], (
        "these edges no longer violate the order; remove them from "
        "ALLOWED_BACKWARD_EDGES so the ratchet tightens: " + repr(resolved)
    )


def test_cycle_cluster_has_not_grown():
    """The whole point of P3: the giant cycle must shrink, never grow."""
    graph = _imports()
    largest = _largest_cycle(graph)
    assert largest <= MAX_STRONGLY_CONNECTED_COMPONENT, (
        f"largest strongly connected component grew to {largest} "
        f"(frozen cap {MAX_STRONGLY_CONNECTED_COMPONENT})"
    )
    if largest < MAX_STRONGLY_CONNECTED_COMPONENT:
        raise AssertionError(
            f"largest cycle cluster fell to {largest}; lower "
            f"MAX_STRONGLY_CONNECTED_COMPONENT from "
            f"{MAX_STRONGLY_CONNECTED_COMPONENT} to tighten the ratchet"
        )
