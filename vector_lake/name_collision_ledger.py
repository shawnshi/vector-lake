"""Record that two near-identical page names are distinct entities, so the lint stops re-reporting them.

``tool_lint``'s Name-Collisions check reports *name shape* and deliberately stops short of calling
it duplication -- ``governance_metrics.find_merge_candidates`` owns that judgement, weighs declared
names and aliases, and carries the ambiguity guard.  But the report counted every near-name pair
identically, so a pair an operator had already read and rejected came back on every run.  A number
that cannot move stops being a signal.

The decision is recorded in a ledger rather than in page frontmatter for the reason
``provenance_legacy`` gives: a frontmatter write is a canonical mutation that re-extracts the page
and re-queues its projections, which is a large blast radius for a label that no reader of the page
acts on.  A ledger is a file -- editable, versionable, revertible -- and a pair can be carved out of
it the moment new evidence makes the two names one entity.

Measured 2026-09-28: 21 pairs reported, 20 of them distinct entities (a national plan beside its
provincial counterparts, ``地坛医院`` beside ``天坛医院``, ``LLM-as-a-Judge`` beside
``VLM-as-a-judge``, three different ``s41746-…`` papers).  The 21st -- ``Concept_UI-Agent`` beside
``Concept_GUI-Agent`` -- was one concept and was merged instead of accepted here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import get_meta_dir

log = logging.getLogger(__name__)

LEDGER_NAME = "name_collision_accepted.json"

#: Bumped when the ledger's shape changes, so a future reader can tell an old file from a corrupt
#: one instead of guessing.
LEDGER_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pair_key(name_a: str, name_b: str) -> str:
    """Order-independent key for a pair of page keys, with any ``.md`` suffix stripped.

    Ordered by ``sorted`` so a pair is one row whichever way the caller happened to enumerate it.
    """
    left = str(name_a).replace(".md", "")
    right = str(name_b).replace(".md", "")
    return "::".join(sorted((left, right)))


def ledger_path() -> Path:
    return get_meta_dir() / LEDGER_NAME


def load_ledger() -> dict:
    """The recorded decisions, or an empty ledger.  An unreadable ledger counts as none."""
    path = ledger_path()
    if not path.exists():
        return {"version": LEDGER_VERSION, "pairs": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a broken ledger must reopen every pair, never hide them
        log.warning("Could not read %s (%s); treating every pair as open", path, exc)
        return {"version": LEDGER_VERSION, "pairs": {}}
    if not isinstance(loaded, dict):
        return {"version": LEDGER_VERSION, "pairs": {}}
    loaded.setdefault("version", LEDGER_VERSION)
    loaded.setdefault("pairs", {})
    return loaded


def accepted_pair_keys() -> set[str]:
    """Pair keys whose near-name similarity has been read and decided on."""
    return {str(key) for key in (load_ledger().get("pairs") or {})}


def accepted_pair_count() -> int:
    return len(accepted_pair_keys())


def accept_pairs(pairs, *, reason: str, decided_by: str = "operator", dry_run: bool = False) -> dict:
    """Record ``pairs`` as distinct entities.  Idempotent: an existing pair keeps its first reason.

    Returns a small report -- ``{"added": n, "skipped": n, "total": n}`` -- rather than the whole
    ledger, because the caller wants to know what changed.
    """
    ledger = load_ledger()
    recorded = ledger.setdefault("pairs", {})
    added = skipped = 0
    for pair in pairs:
        left, right = (list(pair) + [""])[:2]
        key = pair_key(left, right)
        if not left or not right:
            continue
        if key in recorded:
            skipped += 1
            continue
        recorded[key] = {
            "left": str(left).replace(".md", ""),
            "right": str(right).replace(".md", ""),
            "reason": reason,
            "decided_by": decided_by,
            "decided_at": _now(),
        }
        added += 1
    if not dry_run and added:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"added": added, "skipped": skipped, "total": len(recorded), "path": str(ledger_path())}
