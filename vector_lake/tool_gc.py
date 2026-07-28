import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import stat as stat_module
import time
import uuid
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


def _apply_runtime_history_retention(
    conn: sqlite3.Connection,
    *,
    days: int,
    now: float,
) -> dict:
    """Apply the bounded GC retention plan in the caller's transaction."""
    from vector_lake import governance_store

    normalized_days = validate_gc_days(days)
    cutoff_epoch = float(now) - (normalized_days * 86400)
    cutoff = datetime.fromtimestamp(
        cutoff_epoch,
        tz=timezone.utc,
    ).isoformat()
    plan = _safe_change_set_retention_plan(conn, cutoff=cutoff)
    change_set_ids = list((plan.get("selected_ids") or {}).get("change_sets") or [])
    idempotency_count = _count_change_set_idempotency(
        conn,
        change_set_ids,
    )
    deleted_counts = governance_store.apply_history_retention_plan(
        conn,
        plan,
    )
    return {
        "dry_run": False,
        "days": normalized_days,
        "cutoff": cutoff,
        "candidate_count": len(change_set_ids),
        "candidate_idempotency_keys": idempotency_count,
        "pruned_change_sets": int(deleted_counts["change_sets"]),
        "pruned_idempotency_keys": idempotency_count,
        "safe_plan": True,
        "deleted_counts": deleted_counts,
    }


def prune_runtime_history(
    days: int = 30,
    dry_run: bool = True,
    now: float | None = None,
) -> dict:
    """Prune only reference-safe terminal change-set history in one bounded batch."""
    from vector_lake import db_store

    normalized_days = validate_gc_days(days)
    cutoff_epoch = (time.time() if now is None else float(now)) - (
        normalized_days * 86400
    )
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
            result["preview_error"] = f"schema_not_ready:{schema_state['status']}"
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
            result["candidate_idempotency_keys"] = _count_change_set_idempotency(
                conn, change_set_ids
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
        return _apply_runtime_history_retention(
            db_store.get_connection(),
            days=normalized_days,
            now=time.time() if now is None else float(now),
        )


def _gc_snapshot_from_connection(
    conn: sqlite3.Connection,
) -> tuple[dict, list]:
    items = {}
    for row in conn.execute("SELECT entity_id, data_json FROM entities").fetchall():
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


def _plain_path_stat(path, *, directory: bool):
    details = path.lstat()
    file_attributes = getattr(details, "st_file_attributes", 0)
    reparse_attribute = getattr(
        stat_module,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0,
    )
    if stat_module.S_ISLNK(details.st_mode) or (
        reparse_attribute and file_attributes & reparse_attribute
    ):
        raise RuntimeError(f"GC path is a symbolic link or reparse point: {path}")
    expected = (
        stat_module.S_ISDIR(details.st_mode)
        if directory
        else stat_module.S_ISREG(details.st_mode)
    )
    if not expected:
        kind = "directory" if directory else "regular file"
        raise RuntimeError(f"GC path is not a plain {kind}: {path}")
    return details


def _stable_stat_identity(details) -> tuple[int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _flush_plain_file(path) -> None:
    before = _plain_path_stat(path, directory=False)
    with path.open("r+b") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"GC file changed before flush: {path}")
        handle.flush()
        os.fsync(handle.fileno())
        after = os.fstat(handle.fileno())
    current = _plain_path_stat(path, directory=False)
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"GC file changed while flushing: {path}")


def _read_stable_utf8_file(path, *, max_bytes: int) -> str:
    before = _plain_path_stat(path, directory=False)
    if int(before.st_size) > max_bytes:
        raise RuntimeError(f"GC file is unexpectedly large: {path}")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"GC file changed before reading: {path}")
        raw = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    current = _plain_path_stat(path, directory=False)
    if len(raw) > max_bytes:
        raise RuntimeError(f"GC file is unexpectedly large: {path}")
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"GC file changed while reading: {path}")
    return raw.decode("utf-8")


def _hash_file(path) -> str:
    before = _plain_path_stat(path, directory=False)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"GC file changed before hashing: {path}")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    current = _plain_path_stat(path, directory=False)
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"GC file changed while hashing: {path}")
    return digest.hexdigest()


def _remove_plain_gc_tree(path) -> None:
    _plain_path_stat(path, directory=True)
    with os.scandir(path) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        child = path / name
        details = child.lstat()
        file_attributes = getattr(details, "st_file_attributes", 0)
        reparse_attribute = getattr(
            stat_module,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0,
        )
        if stat_module.S_ISLNK(details.st_mode) or (
            reparse_attribute and file_attributes & reparse_attribute
        ):
            raise RuntimeError(
                f"Refusing to clean GC staging link or reparse point: {child}"
            )
        if stat_module.S_ISDIR(details.st_mode):
            _remove_plain_gc_tree(child)
        elif stat_module.S_ISREG(details.st_mode):
            child.unlink()
        else:
            raise RuntimeError(f"Refusing to clean special GC staging entry: {child}")
    path.rmdir()


def _gc_backup_file_records(candidates: list[dict]) -> list[dict]:
    records = [
        {
            "filename": candidate["filename"],
            "entity_id": candidate["entity_id"],
            "page_key": candidate["page_key"],
            "size": int(candidate["size"]),
            "content_sha256": candidate["content_sha256"],
        }
        for candidate in candidates
    ]
    records.sort(
        key=lambda item: (
            item["filename"].casefold(),
            item["filename"],
            item["entity_id"],
        )
    )
    return records


def _verify_gc_backup(
    backup_dir,
    *,
    days: int,
    fingerprint: str,
    candidates: list[dict],
) -> dict:
    _plain_path_stat(backup_dir, directory=True)
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(_read_stable_utf8_file(manifest_path, max_bytes=1024 * 1024))
    expected_files = _gc_backup_file_records(candidates)
    if (
        manifest.get("manifest_version") != 1
        or manifest.get("kind") != "vector-lake-orphan-gc"
        or manifest.get("days") != days
        or manifest.get("fingerprint") != fingerprint
        or manifest.get("files") != expected_files
    ):
        raise RuntimeError(f"GC backup manifest mismatch: {backup_dir}")

    expected_names = {"manifest.json"}
    expected_names.update(record["filename"] for record in expected_files)
    actual_names = {entry.name for entry in backup_dir.iterdir()}
    if actual_names != expected_names:
        raise RuntimeError(f"GC backup file set mismatch: {backup_dir}")

    for record in expected_files:
        backup_path = backup_dir / record["filename"]
        backup_stat = _plain_path_stat(backup_path, directory=False)
        if backup_stat.st_size != record["size"]:
            raise RuntimeError(f"GC backup size mismatch: {backup_path}")
        actual_hash = _hash_file(backup_path)
        if not hmac.compare_digest(
            actual_hash,
            record["content_sha256"],
        ):
            raise RuntimeError(f"GC backup hash mismatch: {backup_path}")
    _plain_path_stat(backup_dir, directory=True)
    return manifest


def _publish_gc_backup(
    *,
    days: int,
    fingerprint: str,
    candidates: list[dict],
    now: float,
):
    backup_root = get_wiki_dir().parent / "backup" / "gc"
    backup_root.mkdir(parents=True, exist_ok=True)
    _plain_path_stat(backup_root, directory=True)
    backup_name = fingerprint.removeprefix(_ORPHAN_FINGERPRINT_PREFIX)[:16]
    backup_dir = backup_root / backup_name
    try:
        _plain_path_stat(backup_dir, directory=True)
    except FileNotFoundError:
        pass
    else:
        _verify_gc_backup(
            backup_dir,
            days=days,
            fingerprint=fingerprint,
            candidates=candidates,
        )
        return backup_dir

    staging_dir = backup_root / f".{backup_name}.{uuid.uuid4().hex}.tmp"
    staging_dir.mkdir(mode=0o700)
    _plain_path_stat(staging_dir, directory=True)
    try:
        for candidate in candidates:
            source_path = candidate["path"]
            source_stat = _plain_path_stat(source_path, directory=False)
            if int(source_stat.st_size) != int(candidate["size"]) or int(
                source_stat.st_mtime_ns
            ) != int(candidate["mtime_ns"]):
                raise RuntimeError(f"GC source candidate changed: {source_path}")
            backup_path = staging_dir / candidate["filename"]
            try:
                backup_path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(
                    f"GC staging destination already exists: {backup_path}"
                )
            shutil.copyfile(source_path, backup_path)
            _flush_plain_file(backup_path)
            shutil.copystat(source_path, backup_path)
            backup_stat = _plain_path_stat(backup_path, directory=False)
            if backup_stat.st_size != candidate["size"]:
                raise RuntimeError(f"GC backup size mismatch: {backup_path}")
            actual_hash = _hash_file(backup_path)
            if not hmac.compare_digest(
                actual_hash,
                candidate["content_sha256"],
            ):
                raise RuntimeError(f"GC backup hash mismatch: {backup_path}")

        manifest = {
            "manifest_version": 1,
            "kind": "vector-lake-orphan-gc",
            "created_at": datetime.fromtimestamp(
                float(now),
                tz=timezone.utc,
            ).isoformat(),
            "days": days,
            "fingerprint": fingerprint,
            "files": _gc_backup_file_records(candidates),
        }
        manifest_path = staging_dir / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        _verify_gc_backup(
            staging_dir,
            days=days,
            fingerprint=fingerprint,
            candidates=candidates,
        )
        try:
            staging_dir.replace(backup_dir)
        except OSError:
            _plain_path_stat(backup_dir, directory=True)
            _verify_gc_backup(
                backup_dir,
                days=days,
                fingerprint=fingerprint,
                candidates=candidates,
            )
        else:
            _verify_gc_backup(
                backup_dir,
                days=days,
                fingerprint=fingerprint,
                candidates=candidates,
            )
        return backup_dir
    finally:
        try:
            _remove_plain_gc_tree(staging_dir)
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError) as cleanup_exc:
            log.warning(
                "GC staging cleanup was deferred for %s: %s",
                staging_dir,
                cleanup_exc,
            )


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
    unsafe_markdown_paths: dict[str, list] = {}
    try:
        entries = list(wiki_dir.iterdir())
    except OSError as exc:
        return [], [f"Cannot enumerate Wiki directory: {exc}"]
    for entry in entries:
        if entry.suffix.casefold() != ".md":
            continue
        try:
            entry_stat = entry.lstat()
        except OSError:
            unsafe_markdown_paths.setdefault(
                entry.stem.casefold(),
                [],
            ).append(entry)
            continue
        file_attributes = getattr(entry_stat, "st_file_attributes", 0)
        reparse_attribute = getattr(
            stat_module,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0,
        )
        if entry.is_symlink() or (
            reparse_attribute and file_attributes & reparse_attribute
        ):
            unsafe_markdown_paths.setdefault(
                entry.stem.casefold(),
                [],
            ).append(entry)
            continue
        if stat_module.S_ISREG(entry_stat.st_mode):
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
        identity = page_key.casefold()
        unsafe_matches = unsafe_markdown_paths.get(identity, [])
        if unsafe_matches:
            errors.append(
                f"{page_key}: Wiki Markdown match is a symbolic link, "
                "reparse point, or otherwise cannot be inspected safely"
            )
            continue
        matches = markdown_paths.get(identity, [])
        if len(matches) > 1:
            errors.append(f"{page_key}: multiple case-insensitive Markdown matches")
            continue
        if not matches:
            continue
        path = matches[0]
        try:
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            content_sha256 = _hash_file(path)
        except (OSError, RuntimeError) as exc:
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
            lines = [f"[DRY-RUN] No orphan entities older than {days} days found."]
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
        None if orphan_confirmation is None else str(orphan_confirmation).strip()
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

    if supplied_confirmation is None:
        retention = prune_runtime_history(days=days, dry_run=False, now=now)
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
        retention = prune_runtime_history(days=days, dry_run=False, now=now)
        return (
            f"GC complete. No orphan entities older than {days} days found. "
            f"Pruned {retention['pruned_change_sets']} change set(s) and "
            f"{retention['pruned_idempotency_keys']} idempotency key(s)."
        )

    backup_dir = None
    retention = None
    details = None
    try:
        backup_dir = _publish_gc_backup(
            days=days,
            fingerprint=fingerprint,
            candidates=candidates,
            now=now,
        )

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

        def apply_history_retention_in_transaction(
            _outbox_ids: list[int],
        ) -> None:
            nonlocal retention
            from vector_lake import db_store

            retention = _apply_runtime_history_retention(
                db_store.get_connection(),
                days=days,
                now=now,
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
            transaction_callback=apply_history_retention_in_transaction,
            return_details=True,
        )
        if details.get("committed") is not True:
            raise RuntimeError(
                "Mutation coordinator did not confirm the transaction commit"
            )
        if retention is None:
            raise RuntimeError("History retention callback did not run")
    except Exception as exc:
        log.error("Confirmed orphan GC failed: %s", exc)
        backup_note = (
            f" Verified backup retained at {backup_dir}."
            if backup_dir is not None
            else ""
        )
        if isinstance(details, dict) and details.get("committed") is True:
            return (
                "GC canonical/history transaction committed, but "
                "post-commit reporting raised a warning: "
                f"{type(exc).__name__}: {exc}.{backup_note}"
            )
        return (
            "Confirmed orphan GC was blocked: "
            f"{exc}. No canonical or history deletion committed."
            f"{backup_note}"
        )

    deferred = list(details.get("deferred") or [])
    post_commit_warnings = list(details.get("post_commit_warnings") or [])
    deferred_note = (
        f" {len(deferred)} Markdown projection deletion(s) were deferred: "
        + ", ".join(deferred)
        + "."
        if deferred
        else ""
    )
    warning_note = (
        f" {len(post_commit_warnings)} post-commit warning(s): "
        + " | ".join(post_commit_warnings)
        + "."
        if post_commit_warnings
        else ""
    )

    return (
        "GC complete. Canonical/history transaction committed. "
        f"Deleted {len(candidates)} orphan pages from canonical state "
        f"(backed up to {backup_dir}).{deferred_note}{warning_note} Pruned "
        f"{retention['pruned_change_sets']} change "
        f"set(s) and {retention['pruned_idempotency_keys']} idempotency key(s)."
    )
