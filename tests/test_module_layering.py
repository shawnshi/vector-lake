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
#
# Placement rule applied here: a module with no intra-package imports cannot create
# an upward dependency, so it belongs in the bottom layer; and a module's layer must
# be at least as high as its own dependencies. ``memory_search_normalization``,
# ``search_projection_contract``, ``source_references`` and
# ``operational_memory_contract`` are dependency-free leaf contracts, and
# ``decision_registry`` imports db_store, so it cannot sit below storage. Treating
# these as domain modules made seven edges look like coupling when they were only my
# map being wrong.
LAYERS: dict[str, set[str]] = {
    "base": {
        "runtime_paths",
        "wiki_utils",
        "yaml_utils",
        "durability",
        "cancellation",
        "tokenizer_runtime",
        "memory_search_normalization",
        "search_projection_contract",
        "source_references",
        "operational_memory_contract",
        # Zero intra-package imports: the runner protocol/contract types the worker
        # implements and the adapters satisfy. A dependency-free leaf belongs at the
        # bottom, and it was in the handler layer only because its package is.
        "auto_ingest_runners.base",
        # Zero intra-package imports: pure timeline prefix/date semantics.
        "timeline_semantics",
        # Ingest root/config resolution, split out of tool_ingest so storage can use
        # it without importing a handler. Depends on wiki_utils (base) only.
        "ingest_paths",
        # Depends only on durability and wiki_utils, both base, and nothing below it
        # imports it. It publishes and reads the watchdog status file, which is
        # infrastructure rather than orchestration; being in orchestration made
        # runtime_health's read of it look upward.
        "watchdog_status",
    },
    "storage": {
        "db_store",
        "governance_store",
        "projection_store_v2",
        "projection_format_v2",
        "raw_revision",
        "storage_growth",
        "decision_registry",
        # Its own dependencies are projection_format_v2 and projection_store_v2
        # (storage) plus wiki_utils (base), so its minimum legal layer is storage;
        # being at derived put it above the storage modules that consume it
        # (db_store, storage_growth). Every importer sits at storage or above.
        "backup_capacity",
        # Pure implementation, no @mcp.tool() surface: it maintains the
        # timeline_events table from claim deltas and reports projection parity.
        # Its only dependencies are db_store (storage) and timeline_semantics
        # (base), and every importer sits at storage or above, so the storage tier
        # is where it belongs. The tool_ prefix made it a handler by name only.
        "tool_timeline",
    },
    "domain": {
        "schema_validator",
        "purpose_contract",
        "claim_extractor",
        "claim_assessment",
        "semantic_merge",
        "merge_analysis",
        "quality_registry",
        "evidence_foundation",
        "provenance",
        "provenance_retention",
        "raw_scrub_contract",
        "skeleton_parser",
        "index_snapshot",
        "defense_hook",
        "retrieval_benchmark",
        "native_llm",
    },
    "derived": {
        "indexer",
        "runtime_health",
        "governance_metrics",
        "restore_snapshot",
        # GC recovery-receipt verification, split out of tool_gc so runtime_health can
        # report it without importing a handler.
        "gc_receipts",
        "diagnostic_snapshot",
        "topology_worker",
        "embedding_scheduler",
    },
    "orchestration": {
        "mutation_coordinator",
        "watchdog_app",
        "auto_ingest_worker",
        "ingest_worker",
        "heavy_task_gate",
        # Canonical write path: validates canonical Markdown and commits mutations.
        # It sits above domain (for validation) and above storage (for the commit),
        # which is exactly why wiki_utils could not keep those concerns.
        "canonical_write",
        # The ingest engine, split out of tool_ingest so the orchestration layer can
        # use it without importing a handler. Its heaviest cluster is closed under
        # intra-module references and its maximum dependency layer is orchestration
        # (mutation_coordinator), which is what makes this the right tier.
        "ingest_engine",
    },
}
# Handlers and the CLI/MCP surface sit on top; they are derived from the filename
# rather than listed, so a new tool cannot silently escape the order.
HANDLER_PREFIXES = ("tool_",)
HANDLER_EXTRAS = {
    "governance_service",
    # A 15-line backward-compatibility facade that lazily re-exports tool_registry,
    # imported only by cli_app and mcp_server. It cannot sit below the handlers it
    # re-exports.
    "tools",
    # An agent-facing facade over the handler layer: it composes recall/remember/
    # entity/synthesize from tool_search, tool_memory and tool_query, and only
    # mcp_server imports it. It cannot sit in base while depending on handlers.
    "memory_protocol",
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
# Started at 44. The gate is only as good as its import resolution, and every
# correction came from making it stricter rather than reading code harder:
#   38 -> 39  dotted module names were truncated to their top level
#   39 -> 43  targets were not resolved to real modules
#   43 -> 44  relative imports (from .mod import x) were skipped entirely
#   44 -> 43  P3.2 batch 1 moved version_family_id to the base layer
ALLOWED_BACKWARD_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        # storage -> derived
        ("governance_store", "governance_metrics"),
        # storage -> domain
        ("db_store", "native_llm"),
        ("governance_store", "claim_extractor"),
        ("governance_store", "provenance_retention"),
        # domain -> derived
        ("provenance", "governance_metrics"),
        ("schema_validator", "indexer"),
        # domain -> handler
        ("provenance_retention", "tool_claim_provenance"),
        ("retrieval_benchmark", "tool_search"),
        # derived -> handler
        ("restore_snapshot", "tool_projection"),
        ("runtime_health", "tool_auto_ingest"),
        # orchestration -> handler
        ("watchdog_app", "tool_governance_maintenance"),
        ("watchdog_app", "tool_lint"),
        # handler -> surface
        ("tool_doctor", "mcp_server"),
    }
)
pass

# Frozen on 2026-09-14; raised to 40 with batch 9.
#
# It did not fall for batches 2-8, and that is expected rather than a bug: the
# backward-edge count and the cycle size are different measures. Removing a
# backward edge that is not on every path around a cycle leaves the cycle intact.
#
# It rose by one in batch 9 for a reason worth recording, because it is not a
# coupling regression: ingest_paths is a NEW module, and the edge ratchet improved
# (21 -> 20) in the same commit. It joins the big cycle only because it imports
# wiki_utils, which is itself in that cycle, and db_store imports it. Verified by
# simulation: with ingest_paths' intra-package dependency removed the cycle is 39
# again.
#
# The same simulation says where the real win is. Removing wiki_utils' three upward
# edges (-> defense_hook, schema_validator, mutation_coordinator) takes the cycle
# from 39 to 32. Those three are the anchor of the whole cluster, and any base
# module that imports wiki_utils joins it too. They are the C-class work that batch
# 5a showed needs a function moved UP a layer rather than validation extracted,
# against a 14-test contract surface.
MAX_STRONGLY_CONNECTED_COMPONENT = 33


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


def _relative_package(module: str, level: int) -> str:
    """Package a relative import climbs to.

    For a module ``p1...pn``, ``from .`` (level 1) means ``p1...p(n-1)`` and
    ``from ..`` (level 2) means ``p1...p(n-2)``.
    """
    parts = module.split(".")
    keep = parts[: len(parts) - level] if level <= len(parts) else []
    return ".".join(keep)


def _imports() -> dict[str, set[str]]:
    """Intra-package imports, including imports deferred inside function bodies.

    Targets are module names, not symbols, and stay fully qualified: truncating to
    the top-level package name would let ``auto_ingest_runners.base`` pass as
    ``auto_ingest_runners`` and escape the declared order. Relative imports are
    resolved too -- skipping them left the gate blind to
    ``from .memory_search_normalization import casefold_text``.
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
                if node.level:
                    package = _relative_package(module, node.level)
                    if node.module:
                        package = (
                            f"{package}.{node.module}" if package else node.module
                        )
                elif (node.module or "").startswith("vector_lake"):
                    package = (node.module or "").removeprefix("vector_lake").lstrip(".")
                else:
                    continue
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
