"""One place that answers, for every derived projection: is it healthy, and what repairs it?

Why this exists: on 2026-09-25 five separate incidents landed on the same root cause -- the lake keeps
a *lot* of derived indices (FTS5, the operational-memory gram index, the vector projection, tantivy,
the page projection, the claim index, the timeline index, governance), and each one had its own idea
of what "healthy" means, its own repair entry point, and its own way of degrading.  The symptoms were
all log lines: a search quietly taking the exact projected scan for 13 hours, 501 missing vectors
after a mass edit, an FTS switch that was inert on the path that mattered.

This module does not add a ninth mechanism.  It gives the existing health checks and repair entry
points **one shape**: ``Projection(name, authority, cost, health, repair)``, plus ``status()`` and
``reconcile()`` over them.  A projection's health is a fact with a detail string, not a sentence in a
log; its repair is a callable, not a script name in a comment.

Pilots registered here (2026-09-25): the operational-memory gram index and the vector projection --
the two this session had to repair by hand.  The remaining projections are **not migrated yet**;
until they are, this is a reporting and reconciliation surface for those two, and the honest next step
is one projection per change with its own test.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("vector-lake-projections")

HEALTHY = "healthy"
DEGRADED = "degraded"


@dataclass(frozen=True)
class Projection:
    """A derived index: what it is derived from, how to tell it is behind, how to fix it."""

    name: str
    #: The sovereign thing it is derived from -- the answer to "what must I trust if they disagree".
    authority: str
    #: What a repair costs, in the units the cost was actually measured in.
    cost: str
    #: ``() -> (state, detail)`` with state in {HEALTHY, DEGRADED}.
    health: Callable[[], tuple[str, str]]
    #: ``() -> str``.  Idempotent and safe to call when nothing is due.
    repair: Callable[[], str]
    #: Which reader degrades when this is unhealthy, so the impact is not a guess.
    degrades: str


def _gram_health() -> tuple[str, str]:
    from vector_lake import memory_gram_index

    try:
        if memory_gram_index.gram_index_usable():
            return HEALTHY, "indexed path serving"
        _total, live, _retired = memory_gram_index.dirty_breakdown()
        return DEGRADED, f"{live} live dirty document(s); memory retrieval on the exact scan"
    except Exception as exc:  # noqa: BLE001 - an unreadable projection is not a healthy one
        return DEGRADED, f"unreadable: {type(exc).__name__}: {exc}"


def _gram_repair() -> str:
    from vector_lake import memory_gram_index

    return memory_gram_index.maybe_rebuild_memory_gram_index()


def _vector_health() -> tuple[str, str]:
    from vector_lake import db_store

    conn = db_store.get_connection()
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM page_index_nodes) AS nodes,"
        " (SELECT COUNT(*) FROM vec_embeddings) AS vectors,"
        " (SELECT COUNT(*) FROM vec_embeddings e WHERE NOT EXISTS ("
        "     SELECT 1 FROM vec_embedding_inputs i WHERE i.node_key = e.page_key)) AS unstamped"
    ).fetchone()
    nodes, vectors, unstamped = int(row["nodes"]), int(row["vectors"]), int(row["unstamped"])
    missing = max(0, nodes - vectors)
    if missing or unstamped:
        return DEGRADED, (
            f"{vectors}/{nodes} vectors, {missing} missing, {unstamped} unstamped "
            "(the vector half of hybrid retrieval is short on those pages)"
        )
    return HEALTHY, f"{vectors}/{nodes} vectors, 0 missing, 0 unstamped"


def _vector_repair() -> str:
    from vector_lake.periodic_catch_up import embedding_catch_up

    summary = embedding_catch_up()
    return (
        f"embedded {summary.get('embedded', 0)} of {summary.get('candidates', 0)} candidate(s)"
        + (f" (skipped: {summary['skipped']})" if summary.get("skipped") else "")
    )


PILOTS: tuple[Projection, ...] = (
    Projection(
        name="memory_gram",
        authority="operational_memory_index",
        cost="~77 s for 69 929 documents (measured 2026-09-25: 58.0 stage + 16.5 pack + 2.0 publish)",
        health=_gram_health,
        repair=_gram_repair,
        degrades="search_vector_lake[memory] and assemble_context (1 240 ms instead of 782 ms)",
    ),
    Projection(
        name="vectors",
        authority="page_index_nodes",
        cost="bounded per sweep (the catch-up embedding budget, default 120 s)",
        health=_vector_health,
        repair=_vector_repair,
        degrades="the vector half of hybrid retrieval for the pages that are missing one",
    ),
)


def registry() -> tuple[Projection, ...]:
    return PILOTS


def status() -> dict[str, dict]:
    """Every registered projection's state and detail.  Read-only."""
    report: dict[str, dict] = {}
    for projection in registry():
        try:
            state, detail = projection.health()
        except Exception as exc:  # noqa: BLE001 - a probe must not take the caller down
            state, detail = DEGRADED, f"probe failed: {type(exc).__name__}: {exc}"
        report[projection.name] = {
            "state": state,
            "detail": detail,
            "authority": projection.authority,
            "cost": projection.cost,
            "degrades": projection.degrades,
        }
    return report


def reconcile(only: str | None = None, dry_run: bool = False) -> dict[str, dict]:
    """Repair every unhealthy projection (or one named one), containing each failure.

    Containment is the point: one projection failing to repair must not stop the others, which is
    what the ad-hoc halves in ``periodic_catch_up`` each had to implement separately.
    """
    results: dict[str, dict] = {}
    for projection in registry():
        if only and projection.name != only:
            continue
        state, detail = projection.health()
        if state == HEALTHY:
            results[projection.name] = {"action": "none", "state": state, "detail": detail}
            continue
        if dry_run:
            results[projection.name] = {"action": "would-repair", "state": state, "detail": detail}
            continue
        try:
            results[projection.name] = {
                "action": "repaired", "state": state, "detail": detail,
                "result": str(projection.repair())[:400],
            }
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning("Repair of projection %s failed: %s: %s", projection.name, type(exc).__name__, exc)
            results[projection.name] = {
                "action": "failed", "state": state, "detail": detail,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results


def status_line() -> str:
    """One compact line for a status surface, e.g. ``memory_gram=healthy vectors=degraded(0/7139…)``."""
    parts = []
    for name, entry in status().items():
        if entry["state"] == HEALTHY:
            parts.append(f"{name}=healthy")
        else:
            parts.append(f"{name}=degraded({entry['detail'][:60]})")
    return " ".join(parts)
