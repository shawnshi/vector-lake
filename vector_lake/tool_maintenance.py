"""Operator-facing wrappers for the two bounded repair paths.

``backup_retention.prune_backups`` and ``db_store.repair_idempotency_keys`` both
answer "what would change" before they change anything, and neither acts until it
is told to.  These wrappers keep that contract at the surface: the CLI's
``--apply`` and the MCP tool's ``dry_run`` flag are the only route to the mutating
branch, and the returned text always states whether anything actually changed.

They live here rather than inside ``backup_retention`` / ``db_store`` so the repair
logic stays free of presentation, and rather than inside ``cli_app`` /
``mcp_server`` so both surfaces report identically.
"""

from __future__ import annotations

from pathlib import Path

from vector_lake import db_store
from vector_lake.backup_retention import (
    prune_backups,
)
from vector_lake.db_store import IDEMPOTENCY_TABLES


def _gib(value: int) -> str:
    return f"{value / 1024 ** 3:.2f} GiB"


def _backup_root() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "backups"


def _bound(value: int) -> int | None:
    """``0`` (or less) means "use the configured default" for either bound."""
    return int(value) if value and int(value) > 0 else None


def backup_retention_report(keep: int = 0, max_bytes: int = 0, dry_run: bool = True) -> str:
    """Report, and only if asked enforce, the bound on ``.meta/backups``.

    Args:
        keep: Number of newest copies to retain. 0 uses the configured default.
        max_bytes: Byte budget for the older copies. 0 uses the configured default.
        dry_run: When True (the default) nothing is removed.
    """
    result = prune_backups(
        _backup_root(),
        keep_count=_bound(keep),
        max_bytes=_bound(max_bytes),
        dry_run=dry_run,
    )

    entries = result["keep"] + result["remove"]
    width = max((len(entry["name"]) for entry in entries), default=0)
    lines = [
        "=== Backup Retention ===",
        f"Root: {result['root']}",
        (
            f"Entries: {result['entry_count']}  total {_gib(result['total_bytes'])}  "
            f"retained {_gib(result['retained_bytes'])}  "
            f"removable {_gib(result['removable_bytes'])}"
        ),
        (
            f"Bound: keep <= {result['keep_count']} copy/copies "
            f"and <= {_gib(result['max_bytes'])}"
        ),
    ]
    for entry in result["keep"]:
        lines.append(
            f"  KEEP    {entry['name']:<{width}}  {entry['kind']:<9} {_gib(entry['bytes'])}"
        )
    for entry in result["remove"]:
        lines.append(
            f"  REMOVE  {entry['name']:<{width}}  {entry['kind']:<9} {_gib(entry['bytes'])}"
        )
    if result["unrecognized"]:
        lines.append(
            "  unrecognised, never pruned: " + ", ".join(result["unrecognized"])
        )

    if not result["remove"]:
        lines.append("Nothing to remove: the backup tree is inside the bound.")
    elif result["dry_run"]:
        lines.append(
            f"[DRY RUN] {len(result['remove'])} entr(ies) would be removed, freeing "
            f"{_gib(result['removable_bytes'])}. Re-run with apply to remove them."
        )
    else:
        lines.append(
            f"[APPLIED] Removed {len(result['deleted'])} entr(ies), freeing "
            f"{_gib(result['removable_bytes'])}. The newest copy is always kept."
        )
        for name in result["deleted"]:
            lines.append(f"  deleted: {name}")
    for failure in result["failures"]:
        lines.append(f"  refused: {failure}")
    return "\n".join(lines)


def idempotency_index_report() -> str:
    """Report the uniqueness guarantee each idempotency table actually has."""
    state = db_store.idempotency_index_state()
    lines = ["=== Idempotency Index ==="]
    for table, info in sorted(state.items()):
        lines.append(
            f"  {table}: uniqueness={info['uniqueness']} "
            f"duplicate_groups={info['duplicate_groups']}"
        )

    degraded = {
        table: info for table, info in state.items() if info["uniqueness"] != "full"
    }
    if not degraded:
        lines.append(
            "Every key is unique forever on both tables; a key can never be reused "
            "after its row reaches a terminal status."
        )
        return "\n".join(lines)

    lines.append("")
    lines.append("full   = a key may never repeat.")
    lines.append(
        "active = the table already held duplicate history, so uniqueness holds over "
        "non-terminal rows only -- exactly the population a concurrent enqueue collides in."
    )
    lines.append(
        "absent = no unique index could be created; only the BEGIN IMMEDIATE write lock "
        "protects enqueue."
    )
    lines.append("")
    for table, info in sorted(degraded.items()):
        lines.append(
            f"{table}: {info['duplicate_groups']} duplicated idempotency_key group(s) "
            f"written by an earlier release."
        )
        lines.append(
            f"  inspect: cli.py repair-idempotency --table {table}   "
            f"(then add --apply to reclaim the full index)"
        )
    return "\n".join(lines)


def repair_idempotency_keys(table: str = "mutation_outbox", dry_run: bool = True) -> str:
    """Clear the redundant idempotency keys so the full unique index can be created.

    Only the duplicate *key* is cleared.  The row, its status, timestamps, error text
    and ``superseded_by`` link are all kept, so no audit history is lost.  The
    canonical row for a key is the one ``enqueue_mutation`` / ``enqueue_job`` already
    returns for it: the lowest ``id``.

    Args:
        table: ``mutation_outbox`` or ``jobs``.
        dry_run: When True (the default) no row is changed.
    """
    if table not in IDEMPOTENCY_TABLES:
        return (
            f"Error: unknown idempotency table '{table}'. "
            f"Expected one of: {', '.join(IDEMPOTENCY_TABLES)}."
        )
    result = db_store.repair_idempotency_keys(table, dry_run=dry_run)

    lines = [f"=== Idempotency Repair: {result['table']} ==="]
    lines.append(
        f"Duplicate groups: {result['duplicate_groups_before']}  "
        f"redundant rows: {result['redundant_rows']}  "
        f"uniqueness: {result['uniqueness_before']}"
    )
    if result["redundant_keys"]:
        lines.append(
            "Each group keeps the key on its canonical (lowest) row; the other "
            f"{result['redundant_rows']} row(s) lose only the key and are not deleted."
        )
        shown = ", ".join(str(key) for key in result["redundant_keys"][:50])
        lines.append(f"  redundant rows: {shown}")
        if len(result["redundant_keys"]) > 50:
            lines.append(f"  ... and {len(result['redundant_keys']) - 50} more.")

    if result["dry_run"]:
        if result["redundant_rows"]:
            lines.append(
                "[DRY RUN] No row was changed. Re-run with apply to clear the redundant "
                "keys and reclaim the full unique index."
            )
        else:
            lines.append("Nothing to repair: no duplicated idempotency_key groups.")
        return "\n".join(lines)

    lines.append("[APPLIED] No rows deleted.")
    lines.append(
        f"Duplicate groups: {result['duplicate_groups_before']} -> "
        f"{result.get('duplicate_groups_after', 0)}  "
        f"uniqueness: {result['uniqueness_before']} -> "
        f"{result.get('uniqueness_after', result['uniqueness_before'])}"
    )
    if result.get("uniqueness_after") != "full":
        lines.append(
            "The full unique index is still unavailable: a duplicate is in a non-terminal "
            "status. Resolve that row, then re-run cli.py idempotency-status."
        )
    return "\n".join(lines)
