"""Converge stored ``claim_type`` with the current classifier, ledger claims first.

When the heading rule that decides whether a block is a ledger entry changes, the rows it
already typed keep the old answer: canonical state is derived, but nothing re-derives it.  On
the live corpus that left 2322 claims typed ``timeline-event`` that the current extractor would
not type that way -- 1595 from evidence-boundary sections (``证据边界`` and friends, 99% of them
undated) and 727 that are page-template markup rather than knowledge.

This re-derives those two sets from what is stored and applies the difference:

* a claim whose heading no longer qualifies is re-typed the way
  ``claim_extractor.claim_type_for_heading`` would type it now (``assertion`` for a paragraph,
  ``bullet-claim`` for a list item), keeping every other field of its JSON untouched -- the
  governance-enriched fields (``assessment_status``, ``calibrated_probability``,
  ``authority_score``, ``reinforcement_count``, ...) are *not* re-derived and must not be lost,
  which is why this does not re-run page extraction;
* a claim the extractor would no longer emit at all (``_is_page_boilerplate``: the template
  caption and the reader directives) is removed together with the evidence rows that support
  only it, and the operational memory built from it, exactly as a removal through the normal
  delta path does;
* the timeline projection follows through
  ``tool_timeline.sync_timeline_events_for_claim_delta`` in the same transaction, so the ledger
  loses those rows with the claims.

Dry-run by default.  ``--apply`` writes a complete restore payload first (every claim row, its
evidence rows, its operational-memory rows and its ledger rows) to
``<MEMORY>/wiki/.meta/backups/reclassify_timeline_claims_<ts>.json``; restoring it means
re-inserting those rows and running ``timeline-rebuild --apply``.

Usage::

    python scripts/reclassify_timeline_claims.py                    # dry run, all pages
    python scripts/reclassify_timeline_claims.py --limit 400         # dry run, first 400 claims
    python scripts/reclassify_timeline_claims.py --apply --batch 500
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vector_lake import governance_store  # noqa: E402
from vector_lake.claim_extractor import (  # noqa: E402
    _claim_type_for_block,
    _is_page_boilerplate,
    _iter_blocks,
    claim_type_for_heading,
)
from vector_lake.db_store import get_connection, init_db, transaction  # noqa: E402
from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta  # noqa: E402
from vector_lake.wiki_utils import get_meta_dir, get_wiki_dir  # noqa: E402

CANDIDATE_TYPE = "timeline-event"
_ROW_COLUMNS = "claim_id, claim_text, status, data_json, updated_at"


def _block_kinds_from_evidence(conn) -> dict[str, str]:
    """``evidence_id -> block kind`` for every evidence row that records one.

    A claim's own JSON does not say whether it came from a paragraph or a list item; the
    evidence row it points at does (``evidence_type`` is ``block-<kind>``).
    """
    kinds: dict[str, str] = {}
    for row in conn.execute("SELECT evidence_id, data_json FROM evidence"):
        data = json.loads(row["data_json"])
        evidence_type = str(data.get("evidence_type") or "")
        if evidence_type.startswith("block-"):
            kinds[str(row["evidence_id"])] = evidence_type[len("block-"):]
    return kinds


def _block_kinds_from_page(page_key: str, block_kinds: dict[str, str]) -> dict[str, str]:
    """``claim text -> block kind`` read back off the page that produced the claim.

    Only used for claims whose evidence link is missing, and only ever called for a page that
    has such a claim.
    """
    path = get_wiki_dir() / f"{page_key}.md"
    if not path.exists():
        return {}
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for block in _iter_blocks(body):
        block_kinds.setdefault(block["text"], block["kind"])
    return block_kinds


def _classify(conn) -> dict[str, list]:
    """Decide what the current classifier would do to every stored ledger claim."""
    evidence_kinds = _block_kinds_from_evidence(conn)
    page_block_kinds: dict[str, dict[str, str]] = {}
    keep: list[dict] = []
    retype: list[dict] = []
    remove: list[dict] = []

    for row in conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM claims WHERE f_claim_type = ? ORDER BY claim_id",
        (CANDIDATE_TYPE,),
    ).fetchall():
        data = json.loads(row["data_json"])
        locator = data.get("locator") or {}
        heading = str(locator.get("heading") or "")
        page_key = str(locator.get("page_key") or "")
        text = str(row["claim_text"] or "")

        if _is_page_boilerplate(text):
            remove.append({"row": row, "data": data})
            continue

        kind = ""
        for evidence_id in data.get("evidence_ids") or []:
            if evidence_id in evidence_kinds:
                kind = evidence_kinds[evidence_id]
                break
        kind_source = "evidence" if kind else ""
        if not kind and page_key:
            kinds = page_block_kinds.setdefault(
                page_key, _block_kinds_from_page(page_key, {})
            )
            kind = kinds.get(text, "")
            kind_source = "page" if kind else ""
        if not kind:
            kind = "paragraph"
            kind_source = "fallback"

        new_type = claim_type_for_heading(kind, heading, text)
        if new_type == CANDIDATE_TYPE:
            keep.append({"row": row, "data": data})
        else:
            retype.append(
                {"row": row, "data": data, "new_type": new_type, "kind_source": kind_source}
            )
    return {"keep": keep, "retype": retype, "remove": remove}


def _retyped_json(data: dict, new_type: str) -> str:
    """The claim's own JSON with only ``claim_type`` moved.

    Re-serialised with the convention ``governance_store`` writes (``ensure_ascii=False``) so
    the row keeps the shape every other claim row has; ``claims.f_claim_type`` is a generated
    column over this document, which is why the type is written here rather than into a
    column.
    """
    updated = dict(data)
    updated["claim_type"] = new_type
    return json.dumps(updated, ensure_ascii=False)


def _restore_payload(conn, work: dict[str, list]) -> dict:
    """Everything an undo needs, read before anything is written."""
    removed_ids = [entry["row"]["claim_id"] for entry in work["remove"]]
    touched_ids = removed_ids + [entry["row"]["claim_id"] for entry in work["retype"]]
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/reclassify_timeline_claims.py",
        "candidate_claim_type": CANDIDATE_TYPE,
        "restore": "re-insert claims/evidence/operational_memory as written, then run "
                   "`python cli.py timeline-rebuild --apply`",
        "claims": [
            dict(entry["row"]) for entry in work["retype"] + work["remove"]
        ],
        "retyped_to": {
            entry["row"]["claim_id"]: entry["new_type"] for entry in work["retype"]
        },
        "removed_claim_ids": removed_ids,
        "evidence": [],
        "operational_memory": [],
        "timeline_events": [],
    }
    if touched_ids:
        claim_list = json.dumps(touched_ids)
        payload["evidence"] = [
            dict(row)
            for row in conn.execute(
                "SELECT evidence_id, data_json, updated_at FROM evidence WHERE EXISTS ("
                "SELECT 1 FROM json_each(json_extract(data_json, '$.supports_claim_ids')) "
                "WHERE value IN (SELECT value FROM json_each(?)))",
                (claim_list,),
            ).fetchall()
        ]
        payload["operational_memory"] = [
            dict(row)
            for row in conn.execute(
                "SELECT memory_id, data_json, updated_at FROM operational_memory "
                "WHERE f_source_claim_id IN (SELECT value FROM json_each(?))",
                (claim_list,),
            ).fetchall()
        ]
        payload["timeline_events"] = [
            dict(row) for row in conn.execute("SELECT * FROM timeline_events")
        ]
    return payload


def _apply(batch: list[dict], work: dict[str, list], conn) -> dict:
    """Write one batch: re-type, remove, and let the derived projections follow."""
    batch_ids = {entry["row"]["claim_id"] for entry in batch}
    retype = [entry for entry in work["retype"] if entry["row"]["claim_id"] in batch_ids]
    remove = [entry for entry in work["remove"] if entry["row"]["claim_id"] in batch_ids]
    removed_ids = [entry["row"]["claim_id"] for entry in remove]
    old_rows = [entry["row"] for entry in retype + remove]

    with transaction():
        for entry in retype:
            conn.execute(
                "UPDATE claims SET data_json = ? WHERE claim_id = ?",
                (_retyped_json(entry["data"], entry["new_type"]), entry["row"]["claim_id"]),
            )
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            conn.execute(f"DELETE FROM claims WHERE claim_id IN ({placeholders})", removed_ids)
            conn.execute(
                "DELETE FROM evidence WHERE EXISTS (SELECT 1 FROM json_each("
                "json_extract(data_json, '$.supports_claim_ids')) WHERE value IN ("
                f"SELECT value FROM json_each(?))) AND NOT EXISTS (SELECT 1 FROM json_each("
                "json_extract(data_json, '$.supports_claim_ids')) "
                f"WHERE value NOT IN (SELECT value FROM json_each(?)))",
                (json.dumps(removed_ids), json.dumps(removed_ids)),
            )
            governance_store._refresh_operational_memory_delta(set(removed_ids), [])
        sync_timeline_events_for_claim_delta(old_rows, [])

    return {"retyped": len(retype), "removed": len(remove)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Persist. Defaults to dry-run.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum claims to process.")
    parser.add_argument("--batch", type=int, default=500, help="Claims per transaction.")
    args = parser.parse_args()

    init_db()
    conn = get_connection()
    work = _classify(conn)
    # Counted before ``--limit`` truncates, so the header always describes the whole corpus.
    stored = {key: len(value) for key, value in work.items()}
    total_stored = sum(stored.values())
    if args.limit is not None:
        keep_limit = max(1, int(args.limit))
        work["retype"] = work["retype"][:keep_limit]
        work["remove"] = work["remove"][:keep_limit]

    retyped_by_type: dict[str, int] = {}
    for entry in work["retype"]:
        retyped_by_type[entry["new_type"]] = retyped_by_type.get(entry["new_type"], 0) + 1
    kind_sources: dict[str, int] = {}
    for entry in work["retype"]:
        kind_sources[entry["kind_source"]] = kind_sources.get(entry["kind_source"], 0) + 1

    print(f"stored {CANDIDATE_TYPE} claims: {total_stored}")
    print(f"  unchanged                     : {stored['keep']}")
    print(f"  re-type (heading no longer qualifies): {stored['retype']}  {retyped_by_type}")
    print(f"    block kind recovered from     : {kind_sources}")
    print(f"  remove (page-template markup)  : {stored['remove']}")
    if args.limit is not None:
        print(f"  this run processes            : {len(work['retype'])} re-type + {len(work['remove'])} remove")

    if not args.apply:
        print("\n[DRY RUN] Nothing written. Re-run with --apply to converge.")
        return 0

    backup_dir = get_meta_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"reclassify_timeline_claims_{stamp}.json"
    backup_path.write_text(
        json.dumps(_restore_payload(conn, work), ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nrestore payload: {backup_path} ({backup_path.stat().st_size / 1024:.0f} KiB)")

    batch_size = max(1, int(args.batch))
    pending = [{"row": e["row"]} for e in work["retype"]] + [{"row": e["row"]} for e in work["remove"]]
    totals = {"retyped": 0, "removed": 0}
    for offset in range(0, len(pending), batch_size):
        result = _apply(pending[offset:offset + batch_size], work, conn)
        totals["retyped"] += result["retyped"]
        totals["removed"] += result["removed"]
        print(f"  batch {offset // batch_size + 1}: {result}")

    remaining = _classify(conn)
    print(
        f"\nre-typed {totals['retyped']} claim(s), removed {totals['removed']} claim(s); "
        f"{len(remaining['retype']) + len(remaining['remove'])} row(s) still disagree"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
