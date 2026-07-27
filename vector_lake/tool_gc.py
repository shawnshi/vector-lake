import hashlib
import hmac
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from vector_lake.wiki_utils import get_wiki_dir

log = logging.getLogger("vector-lake-gc")

_ORPHAN_FINGERPRINT_PREFIX = "sha256:"


def validate_gc_days(days: int) -> int:
    """Return a safe retention interval or reject an ambiguous/destructive value."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    return days


def _count_change_set_idempotency(
    conn: sqlite3.Connection,
    change_set_ids: list[str],
) -> int:
    count = 0
    for offset in range(0, len(change_set_ids), 400):
        chunk = change_set_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            "SELECT COUNT(*) FROM change_set_idempotency "
            f"WHERE change_set_id IN ({placeholders})",
            tuple(chunk),
        ).fetchone()
        count += int(row[0] or 0)
    return count


def _safe_change_set_retention_plan(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
) -> dict:
    from vector_lake import governance_store

    keep_all_other_history = 2_147_483_647
    return governance_store.plan_history_retention(
        conn,
        cutoff=cutoff,
        batch_size=500,
        keep_change_sets=0,
        keep_terminal_jobs=keep_all_other_history,
        keep_terminal_outbox=keep_all_other_history,
        keep_versions_per_family=keep_all_other_history,
    )


def prune_runtime_history(
    days: int = 30,
    dry_run: bool = True,
    now: float | None = None,
) -> dict:
    """Prune only reference-safe terminal change-set history in one bounded batch."""
    from vector_lake import db_store, governance_store

    normalized_days = validate_gc_days(days)
    cutoff_epoch = (
        time.time() if now is None else float(now)
    ) - (normalized_days * 86400)
    cutoff = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()
    result = {
        "dry_run": bool(dry_run),
        "days": normalized_days,
        "cutoff": cutoff,
        "candidate_count": 0,
        "candidate_idempotency_keys": 0,
        "pruned_change_sets": 0,
        "pruned_idempotency_keys": 0,
        "safe_plan": True,
    }

    if dry_run:
        path = db_store.peek_db_path()
        schema_state = db_store.inspect_schema_migration_state(path)
        result["schema_state"] = schema_state
        if not schema_state["ready"]:
            result["preview_error"] = (
                f"schema_not_ready:{schema_state['status']}"
            )
            return result
        conn = None
        try:
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            plan = _safe_change_set_retention_plan(conn, cutoff=cutoff)
            change_set_ids = list(
                (plan.get("selected_ids") or {}).get("change_sets") or []
            )
            result["candidate_count"] = len(change_set_ids)
            result["candidate_idempotency_keys"] = (
                _count_change_set_idempotency(conn, change_set_ids)
            )
            result["selected_counts"] = plan.get("selected_counts") or {}
            return result
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            result["preview_error"] = str(exc)
            return result
        finally:
            if conn is not None:
                conn.close()

    db_store.init_db()
    with db_store.transaction():
        conn = db_store.get_connection()
        plan = _safe_change_set_retention_plan(conn, cutoff=cutoff)
        change_set_ids = list(
            (plan.get("selected_ids") or {}).get("change_sets") or []
        )
        idempotency_count = _count_change_set_idempotency(
            conn,
            change_set_ids,
        )
        deleted_counts = governance_store.apply_history_retention_plan(
            conn,
            plan,
        )
    result["candidate_count"] = len(change_set_ids)
    result["candidate_idempotency_keys"] = idempotency_count
    result["pruned_change_sets"] = int(deleted_counts["change_sets"])
    result["pruned_idempotency_keys"] = idempotency_count
    result["deleted_counts"] = deleted_counts
    return result


def _gc_snapshot_from_connection(
    conn: sqlite3.Connection,
) -> tuple[dict, list]:
    items = {}
    for row in conn.execute(
        "SELECT entity_id, data_json FROM entities"
    ).fetchall():
        items[str(row["entity_id"])] = json.loads(row["data_json"])
    edges = conn.execute(
        "SELECT source_id, target_id FROM claim_graph_edges "
        "UNION SELECT source_id, target_id FROM page_graph_edges"
    ).fetchall()
    return {"items": items}, edges


def _read_gc_snapshot() -> tuple[dict | None, list | None, dict]:
    from vector_lake import db_store

    path = db_store.peek_db_path()
    schema_state = db_store.inspect_schema_migration_state(path)
    if not schema_state["ready"]:
        return None, None, schema_state
    conn = None
    try:
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        entities, edges = _gc_snapshot_from_connection(conn)
        return entities, edges, schema_state
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        invalid_state = {
            **schema_state,
            "ready": False,
            "status": "invalid",
            "error": str(exc),
        }
        return None, None, invalid_state
    finally:
        if conn is not None:
            conn.close()


def _hash_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _orphan_candidates(
    *,
    entities: dict,
    edges: list,
    days: int,
    now: float,
) -> tuple[list[dict], list[str]]:
    nodes = {
        str(node.get("page_key") or entity_id): (str(entity_id), node)
        for entity_id, node in entities.get("items", {}).items()
    }
    degrees = {key: 0 for key in nodes}
    for row in edges:
        source_id, target_id = str(row[0]), str(row[1])
        if source_id in degrees:
            degrees[source_id] += 1
        if target_id in degrees:
            degrees[target_id] += 1

    wiki_dir = get_wiki_dir()
    markdown_paths: dict[str, list] = {}
    try:
        entries = list(wiki_dir.iterdir())
    except OSError as exc:
        return [], [f"Cannot enumerate Wiki directory: {exc}"]
    for entry in entries:
        if entry.is_file() and entry.suffix.casefold() == ".md":
            markdown_paths.setdefault(entry.stem.casefold(), []).append(entry)

    cutoff = now - (days * 86400)
    candidates = []
    errors = []
    for page_key, (entity_id, node) in nodes.items():
        if str(node.get("type") or "").casefold() not in {
            "vendor",
            "product",
            "person",
            "event",
        }:
            continue
        degree = degrees[page_key]
        if degree > 1:
            continue
        matches = markdown_paths.get(page_key.casefold(), [])
        if len(matches) > 1:
            errors.append(
                f"{page_key}: multiple case-insensitive Markdown matches"
            )
            continue
        if not matches:
            continue
        path = matches[0]
        try:
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            content_sha256 = _hash_file(path)
        except OSError as exc:
            errors.append(f"{path.name}: cannot inspect candidate: {exc}")
            continue
        candidates.append(
            {
                "path": path,
                "filename": path.name,
                "entity_id": entity_id,
                "page_key": page_key,
                "degree": degree,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "content_sha256": content_sha256,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["filename"].casefold(),
            item["filename"],
            item["entity_id"],
        )
    )
    return candidates, errors


def _orphan_candidate_fingerprint(days: int, candidates: list[dict]) -> str:
    payload = {
        "days": days,
        "candidates": [
            {
                key: candidate[key]
                for key in (
                    "filename",
                    "entity_id",
                    "page_key",
                    "degree",
                    "size",
                    "mtime_ns",
                    "content_sha256",
                )
            }
            for candidate in candidates
        ],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _ORPHAN_FINGERPRINT_PREFIX + hashlib.sha256(serialized).hexdigest()


def _append_orphan_candidates(lines: list[str], candidates: list[dict]) -> None:
    for candidate in candidates[:20]:
        lines.append(
            "  - "
            f"{candidate['filename']} (ID: {candidate['entity_id']}, "
            f"Degree: {candidate['degree']})"
        )
    if len(candidates) > 20:
        lines.append(f"  ... and {len(candidates) - 20} more.")


def gc_vector_lake(
    days: int = 30,
    dry_run: bool = True,
    orphan_confirmation: str | None = None,
) -> str:
    """Run safe history retention and separately confirmed orphan deletion."""
    days = validate_gc_days(days)
    entities, edges, schema_state = _read_gc_snapshot()
    if entities is None or edges is None:
        mode = "[DRY-RUN] " if dry_run else ""
        return (
            f"{mode}GC unavailable: "
            f"schema_not_ready:{schema_state['status']}. No changes made."
        )

    now = time.time()
    candidates, inspection_errors = _orphan_candidates(
        entities=entities,
        edges=edges,
        days=days,
        now=now,
    )
    fingerprint = _orphan_candidate_fingerprint(days, candidates)

    if dry_run:
        retention_preview = prune_runtime_history(days=days, dry_run=True, now=now)
        if candidates:
            lines = [
                f"[DRY-RUN] Found {len(candidates)} orphan entities older "
                f"than {days} days (Degree <= 1):"
            ]
            _append_orphan_candidates(lines, candidates)
        else:
            lines = [
                f"[DRY-RUN] No orphan entities older than {days} days found."
            ]
        if inspection_errors:
            lines.append(
                f"  [BLOCKED] {len(inspection_errors)} candidate inspection "
                "error(s) must be resolved before orphan deletion:"
            )
            lines.extend(f"    - {error}" for error in inspection_errors[:20])
        lines.extend(
            [
                f"Orphan candidate fingerprint: {fingerprint}",
                "Orphan pages are never deleted by a plain apply. To delete "
                "this exact candidate set, use CLI --apply --confirm-orphans "
                "<fingerprint>, or MCP orphan_confirmation equal to the "
                "fingerprint.",
                "Retention would prune "
                f"{retention_preview['candidate_count']} change set(s).",
                "No changes made.",
            ]
        )
        return "\n".join(lines)

    supplied_confirmation = (
        None
        if orphan_confirmation is None
        else str(orphan_confirmation).strip()
    )
    if supplied_confirmation is not None and not hmac.compare_digest(
        supplied_confirmation,
        fingerprint,
    ):
        return (
            "[BLOCKED] Orphan candidate confirmation does not match the "
            f"current fingerprint {fingerprint}. No changes made; run a new "
            "dry-run preview."
        )
    if supplied_confirmation is not None and inspection_errors:
        return (
            "[BLOCKED] Orphan candidate inspection is incomplete; no changes "
            "made. Resolve: " + "; ".join(inspection_errors[:20])
        )

    retention = prune_runtime_history(days=days, dry_run=False, now=now)
    if supplied_confirmation is None:
        lines = [
            "GC safe phase complete. Orphan deletion was not confirmed; "
            f"retained {len(candidates)} candidate page(s).",
            f"Orphan candidate fingerprint: {fingerprint}",
        ]
        if candidates:
            lines.append(
                "Preview again, then use CLI --confirm-orphans or MCP "
                "orphan_confirmation with the returned fingerprint to "
                "delete the exact candidate set."
            )
        if inspection_errors:
            lines.append(
                f"Orphan scan reported {len(inspection_errors)} inspection "
                "error(s); no orphan pages were deleted."
            )
        lines.append(
            f"Pruned {retention['pruned_change_sets']} change set(s) and "
            f"{retention['pruned_idempotency_keys']} idempotency key(s)."
        )
        return "\n".join(lines)

    if not candidates:
        return (
            f"GC complete. No orphan entities older than {days} days found. "
            f"Pruned {retention['pruned_change_sets']} change set(s) and "
            f"{retention['pruned_idempotency_keys']} idempotency key(s)."
        )

    import shutil

    backup_dir = (
        get_wiki_dir().parent
        / "backup"
        / "gc"
        / fingerprint.removeprefix(_ORPHAN_FINGERPRINT_PREFIX)[:16]
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        for candidate in candidates:
            path = candidate["path"]
            shutil.copy2(path, backup_dir / path.name)

        def assert_candidates_unchanged() -> None:
            from vector_lake import db_store

            current_entities, current_edges = _gc_snapshot_from_connection(
                db_store.get_connection()
            )
            current_candidates, current_errors = _orphan_candidates(
                entities=current_entities,
                edges=current_edges,
                days=days,
                now=now,
            )
            if current_errors:
                raise RuntimeError(
                    "Orphan candidate inspection changed after confirmation: "
                    + "; ".join(current_errors[:20])
                )
            current_fingerprint = _orphan_candidate_fingerprint(
                days,
                current_candidates,
            )
            if not hmac.compare_digest(
                current_fingerprint,
                supplied_confirmation,
            ):
                raise RuntimeError(
                    "Canonical orphan candidate set changed after confirmation"
                )

        from vector_lake.mutation_coordinator import execute_mutation_batch

        details = execute_mutation_batch(
            [
                {
                    "filename": candidate["filename"],
                    "is_delete": True,
                    "expected_projection_hash": candidate["content_sha256"],
                }
                for candidate in candidates
            ],
            precondition_callback=assert_candidates_unchanged,
            return_details=True,
        )
    except Exception as exc:
        log.error("Confirmed orphan GC failed: %s", exc)
        return (
            "GC safe phase complete, but confirmed orphan deletion was "
            f"blocked: {exc}. Pruned {retention['pruned_change_sets']} change "
            f"set(s) and {retention['pruned_idempotency_keys']} idempotency "
            "key(s)."
        )

    deferred = list(details.get("deferred") or [])
    deleted = len(candidates) - len(deferred)
    deferred_note = (
        f" {len(deferred)} projection deletion(s) were deferred: "
        + ", ".join(deferred)
        + "."
        if deferred
        else ""
    )

    return (
        f"GC complete. Deleted {deleted} orphan pages (backed up to "
        f"{backup_dir}).{deferred_note} Pruned "
        f"{retention['pruned_change_sets']} change "
        f"set(s) and {retention['pruned_idempotency_keys']} idempotency key(s)."
    )
