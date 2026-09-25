"""Dispatch the unsupported-claim debt into the governance queue as cohort batches.

The queue already carries this debt at *claim* granularity: 684 ``evidence-gap`` items under
``source='unsupported-claim-governance'`` hold one ``claim_id`` each (419 resolved, 265
acknowledged).  The debt is 18 243 claims across 2 220 pages, so that shape cannot cover it -- a
reviewer needs a unit that shares one owner and one fix, and 18 243 rows would crowd out every
other pending item in the queue.

This produces the coarser unit.  A cohort is ``(state, group)`` and one item is one batch of pages:

* ``state`` is ``claims.evidence_gap``, which the extractor records instead of flattening the two
  gaps into one number (see ``claim_extractor.extract_page_objects``): a page that declares **no**
  source at all is an ingest-contract problem, while a page that declares several and whose block
  does not say which one is the block's own missing anchor.  Measured on the live corpus: no page
  with exactly one declared source carries a gap at all -- with one source every block has an
  anchor, so the state and the fix are the same question.
* ``group`` is ``prefix`` (the page prefix: the content owner) or ``month`` (the claim's
  ``created_at`` month: the ingestion wave).  A "source family" does not exist for these pages by
  definition -- having no recorded source *is* the debt -- so the producer axes available are the
  page name and the ingestion wave.

The era split is reported inside each cohort rather than used as an axis.  A claim with no
``extractor_name`` predates the field, and 98% of this debt is that: it says whether a fix is
possible at all, because a legacy claim on a page that declares no source needs the declaration
before anything can be attached to it.

``search_queries`` is deliberately empty on these items.  ``tool_research`` builds its directive
from the first five pending items' ``search_queries``, and external research cannot supply a page's
provenance -- filling it in would push the items that *can* be researched out of that window.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from vector_lake import db_store, governance_store

log = logging.getLogger(__name__)

#: Who wrote the items, so an operator can list them back with one filter.
SOURCE = "unsupported-claim-cohort-dispatch"

#: The two debt shapes and the fix each one needs.  The labels are used verbatim in the item
#: title, so they say what is wrong rather than naming an internal constant.
STATES = {
    "no_source": {
        "label": "page declares no source at all",
        "reason": "page_declares_no_source",
        "fix": (
            "declare the page's provenance in frontmatter (`sources:`) and re-extract, or accept "
            "the claim as legacy debt"
        ),
    },
    "ambiguous_source": {
        "label": "block does not name which of the page's sources it used",
        "reason": "block_does_not_name_its_source",
        "fix": "add the block's inline anchor (`(Source: [[...]])`) so evidence can attach to it",
    },
}

_GROUPINGS = ("prefix", "month")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _page_facts() -> list[dict]:
    """One row per ``(page, state, era, month)``: how many claims carry that gap.

    Read from the canonical ``claims`` table rather than from ``claim_index`` because the index
    projects ``evidence_gap`` but not ``extractor_name``, and the era decides whether a fix is
    possible at all.  Both come out of one pass of ``json_extract`` -- no payload decode, which is
    what keeps this affordable next to an 18 243-claim debt (~0.9 s on the live corpus).
    """
    db_store.init_db()
    conn = db_store.get_connection()
    rows = conn.execute(
        """
        SELECT json_extract(data_json, '$.locator.page_key')         AS page_key,
               json_extract(data_json, '$.evidence_gap')             AS gap,
               json_extract(data_json, '$.extractor_name') IS NULL   AS legacy,
               substr(json_extract(data_json, '$.created_at'), 1, 7) AS month,
               COUNT(*)                                              AS claims
        FROM claims
        WHERE json_extract(data_json, '$.evidence_gap') <> ''
        GROUP BY 1, 2, 3, 4
        """
    ).fetchall()
    return [
        {
            "page_key": str(row["page_key"]),
            "state": str(row["gap"]),
            "legacy": bool(row["legacy"]),
            "month": str(row["month"] or "unknown"),
            "claims": int(row["claims"]),
        }
        for row in rows
        if row["page_key"]
    ]


def _cohort_key(entry: dict, group: str) -> str:
    if group == "month":
        return entry["month"]
    return entry["page_key"].split("_", 1)[0]


def _item(
    *,
    state: str,
    group: str,
    cohort: str,
    entries: list[dict],
    batch_index: int,
    batch_count: int,
    page_limit: int,
) -> dict:
    """One governance item covering ``entries`` (a batch of one cohort's pages)."""
    shape = STATES[state]
    pages = sorted(entry["page_key"] for entry in entries)
    claims = sum(entry["claims"] for entry in entries)
    legacy = sum(entry["claims"] for entry in entries if entry["legacy"])
    batch_key = f"{state}:{group}:{cohort}:{batch_index}"
    #: The covered page set, so a rerun can tell "the same batch" from "the same batch, changed
    #: pages" without re-deriving what the reviewer is looking at.
    cohort_version = _short_hash("\n".join(pages))
    return {
        "item_id": f"gov_evidence_gap_cohort_{_short_hash(batch_key)}",
        "type": "evidence-gap",
        "title": f"Evidence-gap cohort: {shape['label']} -- {cohort} (batch {batch_index}/{batch_count})",
        "description": (
            f"{len(pages)} page(s) carry {claims} claim(s) whose evidence gap is "
            f"{shape['reason']}; {legacy} of those claim(s) predate extractor attribution. "
            f"Fix: {shape['fix']}."
        ),
        "created_at": _now(),
        "status": "pending",
        "source": SOURCE,
        "owner": "vector-lake-governance",
        "reason": shape["reason"],
        "fix": shape["fix"],
        "cohort": {
            "state": state,
            "group": group,
            "key": cohort,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "batch_key": batch_key,
            "cohort_version": cohort_version,
            "page_count": len(pages),
            "claim_count": claims,
            "legacy_claim_count": legacy,
            "current_claim_count": claims - legacy,
        },
        "affected_pages": pages[:page_limit],
        "affected_page_count": len(pages),
        # Empty on purpose: ``tool_research`` builds its directive from the first five pending
        # items' queries, and no amount of external research supplies a page's provenance.
        "search_queries": [],
    }


def plan_evidence_gap_batches(group: str = "prefix", batch_pages: int = 100, page_limit: int = 25) -> dict:
    """Read-only plan: the cohorts, their batches, and the item each batch would become."""
    if group not in _GROUPINGS:
        raise ValueError(f"group must be one of {_GROUPINGS}, not {group!r}")
    if batch_pages < 1:
        raise ValueError("batch_pages must be at least 1")
    if page_limit < 0:
        raise ValueError("page_limit must not be negative")

    facts = _page_facts()
    cohorts: dict[tuple[str, str], list[dict]] = {}
    for entry in facts:
        cohorts.setdefault((entry["state"], _cohort_key(entry, group)), []).append(entry)

    batches = []
    for (state, cohort), entries in sorted(cohorts.items()):
        entries.sort(key=lambda entry: entry["page_key"])
        chunks = [entries[index : index + batch_pages] for index in range(0, len(entries), batch_pages)]
        for index, chunk in enumerate(chunks, start=1):
            batches.append(
                {
                    "state": state,
                    "cohort": cohort,
                    "batch_index": index,
                    "batch_count": len(chunks),
                    "entry": chunk[0],
                    "item": _item(
                        state=state,
                        group=group,
                        cohort=cohort,
                        entries=chunk,
                        batch_index=index,
                        batch_count=len(chunks),
                        page_limit=page_limit,
                    ),
                }
            )
    return {
        "group": group,
        "batch_pages": batch_pages,
        "page_limit": page_limit,
        "total_claims": sum(entry["claims"] for entry in facts),
        "total_pages": len({entry["page_key"] for entry in facts}),
        "state_totals": {
            state: {
                "claims": sum(entry["claims"] for entry in facts if entry["state"] == state),
                "legacy_claims": sum(
                    entry["claims"] for entry in facts if entry["state"] == state and entry["legacy"]
                ),
                "pages": len({entry["page_key"] for entry in facts if entry["state"] == state}),
                "label": STATES.get(state, {}).get("label", state),
            }
            for state in sorted({entry["state"] for entry in facts})
        },
        "batches": batches,
    }


def claim_evidence_queue(
    dry_run: bool = True,
    group: str = "prefix",
    batch_pages: int = 100,
    page_limit: int = 25,
) -> str:
    """Report the cohort plan, and with ``dry_run=False`` enqueue one item per batch.

    Re-running is safe: an item id is derived from ``(state, group, cohort, batch)``, so a batch
    already in the queue is skipped instead of duplicated, and a batch whose page set has changed
    since is reported as stale rather than enqueued a second time under a new id.
    """
    plan = plan_evidence_gap_batches(group=group, batch_pages=batch_pages, page_limit=page_limit)
    lines = ["=== Unsupported Claim Cohorts ==="]
    if not plan["batches"]:
        lines.append("No claim carries an evidence gap. Nothing to dispatch.")
        return "\n".join(lines)

    lines.append(
        f"claims with an evidence gap: {plan['total_claims']} over {plan['total_pages']} page(s)"
    )
    for state, totals in plan["state_totals"].items():
        lines.append(
            f"  {totals['label']}: {totals['claims']} claims / {totals['pages']} pages "
            f"(legacy without extractor attribution: {totals['legacy_claims']})"
        )

    existing = {
        str(item.get("item_id")): item
        for item in governance_store.load_governance_queue()["items"]
    }
    fresh, skipped, stale = [], [], []
    for batch in plan["batches"]:
        item = batch["item"]
        previous = existing.get(item["item_id"])
        if previous is None:
            fresh.append(item)
        else:
            skipped.append(item["item_id"])
            if previous.get("cohort", {}).get("cohort_version") != item["cohort"]["cohort_version"]:
                stale.append(item["item_id"])

    lines.append(
        f"group={plan['group']} batch_pages={plan['batch_pages']} -> {len(plan['batches'])} batch(es)"
    )
    for batch in plan["batches"]:
        cohort = batch["item"]["cohort"]
        preview = ", ".join(batch["item"]["affected_pages"][:5])
        if cohort["page_count"] > 5:
            preview += f", … (+{cohort['page_count'] - 5} more)"
        lines.append(
            f"  {batch['cohort']}  |  {STATES.get(batch['state'], {}).get('label', batch['state'])}"
            f"  |  batch {batch['batch_index']}/{batch['batch_count']}"
            f"  |  {cohort['page_count']} page(s)  |  {cohort['claim_count']} claim(s) "
            f"(legacy {cohort['legacy_claim_count']})"
        )
        lines.append(f"      {preview}")

    if skipped:
        lines.append(f"already in the queue (skipped): {len(skipped)} batch(es)")
    if stale:
        lines.append(
            f"of those, {len(stale)} cover a changed page set: resolve them and rerun to refresh"
        )
    if dry_run:
        lines.append(f"[DRY RUN] {len(fresh)} item(s) would be enqueued under source={SOURCE!r}.")
        return "\n".join(lines)

    enqueued = governance_store.enqueue_governance_items(fresh)
    lines.append(f"enqueued {enqueued} item(s) under source={SOURCE!r}; list them with `review`.")
    return "\n".join(lines)
