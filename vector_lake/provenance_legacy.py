"""Record that a page's provenance is unrecorded, and keep it out of the open debt count.

Stage 1 restored every declaration the two exact ledgers could still answer (352 pages / 4 631
claims).  The remaining 1 700 pages / 12 686 claims have no recoverable provenance: 2 001 of them
only ever carried the placeholder ``Source_Auto_Fixed``, which the 2026-09-20 08:06-08:33 pass
removed without changing one byte of body text, and 45 have no write history at all.  Their claims
are general statements about a subject, so "the raw file that looks most like this page" would be a
fabricated citation -- the one thing a provenance contract must not do.

This is where that decision is recorded.  One ledger under ``wiki/.meta`` names the pages, the
criteria and the evidence, so ``compute_debt_metrics`` can report the open debt and the decided
debt apart.  Without the split the lint reports a constant, and a number nobody can act on stops
being a signal.

The decision is recorded here rather than in 1 700 page frontmatters on purpose: a frontmatter
write is a canonical mutation that re-extracts the page and re-queues its projections, which is a
large blast radius for a label that no reader of the page acts on.  The ledger is a file: it can be
edited, versioned and reverted, and a page can be carved out of it the moment a real source is
found.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import get_meta_dir

log = logging.getLogger(__name__)

LEDGER_NAME = "provenance_legacy_accepted.json"

#: Page families whose raw original is still on disk, so a later re-ingest could still produce a
#: page *with* provenance.  They are inside the accepted set and named apart rather than excluded,
#: because excluding them would leave them in the open count with no route to closure.
REVISIT_PREFIXES = ("Source_", "Event_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger_path() -> Path:
    return get_meta_dir() / LEDGER_NAME


def load_acceptance() -> dict:
    """The recorded decision, or an empty dict.  An unreadable ledger counts as none."""
    path = ledger_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a broken ledger must reopen the debt, never hide it
        log.warning("Could not read %s (%s); treating every claim as open debt", path, exc)
        return {}


def accepted_pages() -> set[str]:
    """Page keys whose unrecorded provenance has been decided on (``.md`` suffix stripped)."""
    ledger = load_acceptance()
    return {str(page).replace(".md", "") for page in (ledger.get("pages") or {})}


def accept_unrecorded_provenance(dry_run: bool = True) -> str:
    """Write the acceptance ledger for every page the exact recovery rules cannot resolve."""
    from vector_lake.provenance_backfill import plan_provenance_backfill

    plan = plan_provenance_backfill()
    pages = {entry["page_key"]: entry["claim_count"] for entry in plan["unmatched"]}
    pages.update({entry["page_key"]: entry["claim_count"] for entry in plan["ambiguous"]})
    claim_count = sum(pages.values())
    ambiguous = {
        entry["page_key"]: entry.get("candidates", []) for entry in plan["ambiguous"]
    }
    revisit = {
        prefix: sum(1 for page in pages if page.startswith(prefix)) for prefix in REVISIT_PREFIXES
    }
    record = {
        "version": 1,
        "decided_at": _now(),
        "decision": "accept-unrecorded-provenance-as-legacy",
        "criteria": (
            "pages carrying claims whose evidence_gap is no_source and whose frontmatter declares "
            "no raw path, after the two exact recovery rules (ingest job ledger; unique "
            "canonical_source_name match) left them unresolved"
        ),
        "evidence": {
            "recovery_rules_tried": (
                "jobs.payload {filepath, canonical_name} -> 142 pages / 1 450 claims restored; "
                "canonical_source_name inverted over raw/ -> 210 pages / 3 181 claims restored"
            ),
            "why_nothing_remains": (
                "2 001 of these pages only ever declared the placeholder Source_Auto_Fixed "
                "(schema_validator.PLACEHOLDER_SOURCES) and 45 have no stored payload; the "
                "2026-09-20 08:06-08:33 pass removed the placeholder with the body text unchanged"
            ),
            "probes": [
                "scratch/probe_source_recovery.py",
                "scratch/probe_provenance_ledger.py",
            ],
        },
        "page_count": len(pages),
        "claim_count": claim_count,
        "pages": pages,
        "ambiguous_candidates": ambiguous,
        "revisit_candidates": revisit,
    }

    lines = [
        "=== Unrecorded Provenance Acceptance ===",
        f"pages to accept: {len(pages)}  |  claims: {claim_count}",
        f"  of which with several candidate raw files (revisit by hand): {len(ambiguous)}",
        f"  revisit candidates whose raw original is still on disk: {revisit}",
        f"ledger: {ledger_path()}",
    ]
    if dry_run:
        lines.append(f"[DRY RUN] {len(pages)} page(s) would be recorded as accepted legacy debt.")
        return "\n".join(lines)

    path = ledger_path()
    if path.exists():
        backup = path.with_suffix(".json.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        lines.append(f"previous ledger backed up to {backup}")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    lines.append(f"recorded {len(pages)} page(s) / {claim_count} claim(s) as accepted legacy debt.")
    return "\n".join(lines)
