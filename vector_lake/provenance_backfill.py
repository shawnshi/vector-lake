"""Restore a page's ``sources:`` declaration from the ledgers its own ingest left behind.

Measured on the live corpus: of the 2 052 pages carrying ``no_source`` claims (17 317 claims), the
provenance was **never recorded** for all but 6 -- 2 001 pages only ever carried the placeholder
``Source_Auto_Fixed`` (``schema_validator.PLACEHOLDER_SOURCES``), which the 2026-09-20 pass removed
without touching a byte of body text, and 45 have no record at all.  So this is not a repair of a
lost field; it is recovery from the two ledgers that *do* still know the answer:

* ``jobs.payload`` -- every ingest records ``{filepath, hash, canonical_name}``, which maps a raw
  file to the Source page it produced.  Resolves 142 pages / 1 450 claims, and all 142 raw files are
  still on disk.
* ``canonical_source_name`` inverted over the raw tree -- a Source page is *named* after its raw
  stem, so a unique match resolves 210 pages / 3 181 claims.  68 further pages match more than one
  raw file and 28 match none; those are left for a human, not guessed.

Both rules are exact.  A similarity match against "the raw file that looks most like this page"
would fabricate provenance for pages whose claims are general statements, which is the one thing a
knowledge base with a provenance contract must not do.

Writes go through the single mutation path (``execute_mutation_batch``), so each page still gets
schema validation, ``verify_asset`` and the canonical change set; every write is preceded by the
page's full prior text landing in a rollback file, and ``revert`` replays that file.
"""

from __future__ import annotations

import collections
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import db_store, governance_store, mutation_coordinator
from vector_lake.wiki_utils import canonical_source_name, get_meta_dir, get_wiki_dir

log = logging.getLogger(__name__)

#: The frontmatter ``sources:`` line plus any block-list items under it.
_SOURCES_BLOCK = re.compile(r"^sources:.*(?:\n(?:[ \t]+.*|[ \t]*-.*))*", re.M)
_FRONTMATTER = re.compile(r"\A---\n(?P<frontmatter>.*?)\n---\n", re.S)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memory_root() -> Path:
    return get_wiki_dir().parent


def _no_source_pages() -> dict[str, int]:
    """``page_key -> claim count`` for every page carrying an evidence gap of ``no_source``."""
    db_store.init_db()
    rows = db_store.get_connection().execute(
        """SELECT json_extract(data_json, '$.locator.page_key') AS page_key, COUNT(*) AS claims
           FROM claims
           WHERE json_extract(data_json, '$.evidence_gap') = 'no_source'
             AND json_extract(data_json, '$.locator.page_key') IS NOT NULL
           GROUP BY 1"""
    ).fetchall()
    return {str(row["page_key"]): int(row["claims"]) for row in rows}


def declared_raw_paths(page_key: str) -> list[str]:
    """The ``raw/`` entries the page declares today, or an empty list."""
    path = get_wiki_dir() / f"{page_key}.md"
    if not path.exists():
        return []
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8", errors="ignore"))
    if not match:
        return []
    found = _SOURCES_BLOCK.search(match.group("frontmatter"))
    if not found:
        return []
    return [ref for ref in re.findall(r"[\"'](raw/[^\"']+)[\"']", found.group(0))]


def _ledger_paths() -> dict[str, str]:
    """``page_key -> raw path`` from the ingest jobs' ``{filepath, canonical_name}`` payloads."""
    resolved: dict[str, str] = {}
    rows = db_store.get_connection().execute(
        "SELECT payload FROM jobs WHERE payload LIKE '%\"filepath\"%' AND payload LIKE '%canonical_name%'"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        name = str(payload.get("canonical_name") or "").strip()
        filepath = str(payload.get("filepath") or "").strip().replace("\\", "/")
        if not name or not filepath:
            continue
        if "/raw/" in filepath:
            filepath = "raw/" + filepath.split("/raw/", 1)[1]
        resolved.setdefault(name.replace(".md", ""), filepath)
    return resolved


def _canonical_name_paths() -> dict[str, list[str]]:
    """``canonical source page name -> raw paths``, inverted over the whole raw tree."""
    inverted: dict[str, list[str]] = collections.defaultdict(list)
    root = _memory_root()
    for path in (root / "raw").rglob("*"):
        if path.is_file():
            relative = str(path.relative_to(root)).replace("\\", "/")
            inverted[canonical_source_name(relative).replace(".md", "")].append(relative)
    return inverted


def plan_provenance_backfill() -> dict:
    """Read-only plan: what can be restored by an exact rule, and what cannot."""
    root = _memory_root()
    pages = _no_source_pages()
    ledger = _ledger_paths()
    canonical = _canonical_name_paths()

    restorable: list[dict] = []
    ambiguous: list[dict] = []
    unmatched: list[dict] = []
    already_declared: list[dict] = []
    for page_key, claims in sorted(pages.items()):
        if declared_raw_paths(page_key):
            # A page that already declares a raw path but still shows no_source is a different
            # defect from an unrecorded provenance, so it is reported apart and not touched.
            already_declared.append({"page_key": page_key, "claim_count": claims})
            continue
        raw_path = ledger.get(page_key)
        rule = "job-ledger"
        if not raw_path or not (root / raw_path).exists():
            candidates = canonical.get(page_key, [])
            rule = "canonical-name"
            raw_path = candidates[0] if len(candidates) == 1 else None
            if len(candidates) > 1:
                ambiguous.append(
                    {"page_key": page_key, "claim_count": claims, "candidates": candidates[:5]}
                )
                continue
        if raw_path and (root / raw_path).exists():
            restorable.append(
                {"page_key": page_key, "raw_path": raw_path, "rule": rule, "claim_count": claims}
            )
        else:
            unmatched.append({"page_key": page_key, "claim_count": claims})

    return {
        "no_source_pages": len(pages),
        "no_source_claims": sum(pages.values()),
        "restorable": restorable,
        "restorable_claims": sum(entry["claim_count"] for entry in restorable),
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "already_declared": already_declared,
    }


def _rollback_path() -> Path:
    directory = get_meta_dir() / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-provenance-backfill.rollback.jsonl"


def _render_with_sources(text: str, raw_path: str) -> str:
    """The page text with its frontmatter ``sources:`` block replaced by one raw path."""
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError("page has no frontmatter block to edit")
    replacement = "sources: " + json.dumps([raw_path], ensure_ascii=False)
    rendered, count = _SOURCES_BLOCK.subn(replacement, match.group("frontmatter"), count=1)
    if not count:
        raise ValueError("page declares no `sources:` line to replace")
    return text[: match.start("frontmatter")] + rendered + text[match.end("frontmatter") :]


def backfill_provenance(dry_run: bool = True, batch: int = 50, limit: int | None = None) -> str:
    """Restore the restorable declarations, one mutation batch at a time.

    Each page's prior text is written to the rollback file *before* its batch is submitted, and a
    batch the write gate refuses is retried page by page so one refusing page cannot hold back the
    rest -- the failures are named instead of retried.
    """
    plan = plan_provenance_backfill()
    entries = plan["restorable"][: limit if limit else None]
    lines = [
        "=== Provenance Backfill ===",
        f"pages with a no_source gap: {plan['no_source_pages']} ({plan['no_source_claims']} claims)",
        f"restorable by an exact rule: {len(plan['restorable'])} pages "
        f"({plan['restorable_claims']} claims)",
        f"  job ledger: {sum(1 for e in plan['restorable'] if e['rule'] == 'job-ledger')}"
        f"  |  canonical name (unique): {sum(1 for e in plan['restorable'] if e['rule'] == 'canonical-name')}",
        f"ambiguous (several raw files match): {len(plan['ambiguous'])} pages"
        f"  |  no match: {len(plan['unmatched'])} pages"
        f"  |  already declaring a path: {len(plan['already_declared'])} pages",
    ]
    for entry in entries[:8]:
        lines.append(f"  {entry['page_key']} -> {entry['raw_path']}  [{entry['rule']}]")
    if len(entries) > 8:
        lines.append(f"  … and {len(entries) - 8} more")
    if dry_run:
        lines.append(f"[DRY RUN] {len(entries)} page(s) would be rewritten; rollback file: {_rollback_path()}")
        return "\n".join(lines)

    rollback = _rollback_path()
    written = 0
    refused: list[str] = []
    with rollback.open("a", encoding="utf-8") as sink:
        for start in range(0, len(entries), batch):
            chunk = entries[start : start + batch]
            mutations = []
            for entry in chunk:
                path = get_wiki_dir() / f"{entry['page_key']}.md"
                before = path.read_text(encoding="utf-8", errors="ignore")
                after = _render_with_sources(before, entry["raw_path"])
                sink.write(
                    json.dumps(
                        {
                            "page_key": entry["page_key"],
                            "raw_path": entry["raw_path"],
                            "rule": entry["rule"],
                            "claim_count": entry["claim_count"],
                            "written_at": _now(),
                            "before": before,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                mutations.append(
                    {
                        "filename": f"{entry['page_key']}.md",
                        "content": after,
                        "expected_version": governance_store.canonical_page_version_from_content(
                            f"{entry['page_key']}.md", before
                        ),
                    }
                )
            sink.flush()  # the recovery information must be on disk before the write it guards
            try:
                mutation_coordinator.execute_mutation_batch(mutations)
                written += len(mutations)
            except Exception as exc:  # noqa: BLE001 - a refusing page must not hold back the batch
                log.warning("Batch %d refused (%s); retrying page by page", start // batch + 1, exc)
                for mutation in mutations:
                    try:
                        mutation_coordinator.execute_mutation_batch([mutation])
                        written += 1
                    except Exception as inner:  # noqa: BLE001
                        refused.append(f"{mutation['filename']}: {inner}")
        lines.append(f"restored {written} page(s); rollback file: {rollback}")
        if refused:
            lines.append(f"refused by the write gate: {len(refused)}")
            lines.extend(f"  {item}" for item in refused[:10])
    return "\n".join(lines)


def revert_provenance_backfill(rollback_file: str, batch: int = 50, dry_run: bool = False) -> str:
    """Put every page in ``rollback_file`` back to the text it had before the backfill."""
    path = Path(rollback_file)
    if not path.exists():
        return f"No rollback file at {rollback_file}."
    before: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        # First record wins: a page written twice must go back to what it held *before* this pass.
        before.setdefault(record["page_key"], record["before"])
    if dry_run:
        return f"[DRY RUN] {len(before)} page(s) would be reverted from {path}."

    entries = sorted(before.items())
    reverted = 0
    refused: list[str] = []
    for start in range(0, len(entries), batch):
        chunk = entries[start : start + batch]
        mutations = []
        for page_key, text in chunk:
            mutations.append(
                {
                    "filename": f"{page_key}.md",
                    "content": text,
                    "expected_version": governance_store.canonical_page_version_from_content(
                        f"{page_key}.md",
                        (get_wiki_dir() / f"{page_key}.md").read_text(encoding="utf-8", errors="ignore"),
                    ),
                }
            )
        try:
            mutation_coordinator.execute_mutation_batch(mutations)
            reverted += len(mutations)
        except Exception as exc:  # noqa: BLE001
            log.warning("Revert batch refused (%s); retrying page by page", exc)
            for mutation in mutations:
                try:
                    mutation_coordinator.execute_mutation_batch([mutation])
                    reverted += 1
                except Exception as inner:  # noqa: BLE001
                    refused.append(f"{mutation['filename']}: {inner}")
    report = [f"reverted {reverted} page(s) from {path}"]
    if refused:
        report.append(f"refused by the write gate: {len(refused)}")
        report.extend(f"  {item}" for item in refused[:10])
    return "\n".join(report)
