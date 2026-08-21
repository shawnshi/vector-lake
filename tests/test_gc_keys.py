import hashlib
import json
import os
import re
import shutil
import time
from unittest.mock import patch

import pytest

from vector_lake import db_store, governance_store, tool_gc
from vector_lake.tool_gc import gc_vector_lake


def _insert_change_set_history(
    conn,
    change_set_id: str,
    *,
    status: str = "applied",
    timestamp: str = "2000-01-01T00:00:00+00:00",
):
    data_json = json.dumps({"status": status}, separators=(",", ":"))
    conn.execute(
        "INSERT INTO change_sets "
        "(change_set_id, data_json, updated_at) VALUES (?, ?, ?)",
        (change_set_id, data_json, timestamp),
    )
    conn.execute(
        "INSERT INTO change_set_lifecycle_v6 "
        "(change_set_id, status, created_at, terminal_at, time_source, "
        "payload_guard_sha256) VALUES (?, ?, ?, ?, ?, ?)",
        (
            change_set_id,
            status,
            timestamp,
            None if status == "pending" else timestamp,
            "test_seed",
            hashlib.sha256(data_json.encode("utf-8")).hexdigest(),
        ),
    )


def _seed_vendor(memory_dir, edge_count: int):
    db_store.init_db()
    governance_store.save_entities(
        {
            "items": {
                "entity-internal-id": {
                    "entity_id": "entity-internal-id",
                    "canonical_name": "Acme",
                    "page_key": "Vendor_Acme",
                    "type": "vendor",
                    "status": "Active",
                }
            }
        }
    )
    page = memory_dir / "wiki" / "Vendor_Acme.md"
    page.write_text("legacy vendor", encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(page, (old, old))
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(edge_count):
            conn.execute(
                "INSERT INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Vendor_Acme", f"Concept_{index}", "related", 1.0, "2026-01-01"),
            )


def _seed_old_history(change_set_id: str = "changeset_old"):
    db_store.init_db()
    conn = db_store.get_connection()
    old = "2000-01-01T00:00:00+00:00"
    with db_store.transaction():
        _insert_change_set_history(conn, change_set_id, timestamp=old)
        conn.execute(
            "INSERT INTO change_set_idempotency "
            "(idempotency_key, change_set_id, created_at) "
            "VALUES (?, ?, ?)",
            (f"idem-{change_set_id}", change_set_id, old),
        )
    return conn


def test_gc_uses_page_key_for_file_and_graph_degree(isolated_memory):
    _seed_vendor(isolated_memory, edge_count=1)

    result = gc_vector_lake(days=30, dry_run=True)

    assert "Vendor_Acme.md" in result
    assert "entity-internal-id" in result


def test_gc_hash_race_is_reported_as_blocking_inspection_error(
    isolated_memory,
    monkeypatch,
):
    _seed_vendor(isolated_memory, edge_count=1)

    def unstable_hash(_path):
        raise RuntimeError("injected file change while hashing")

    monkeypatch.setattr(tool_gc, "_hash_file", unstable_hash)

    result = gc_vector_lake(days=30, dry_run=True)

    assert "[BLOCKED]" in result
    assert "cannot inspect candidate" in result
    assert "injected file change while hashing" in result


def test_gc_does_not_delete_high_degree_page_key(isolated_memory):
    _seed_vendor(isolated_memory, edge_count=2)

    result = gc_vector_lake(days=30, dry_run=True)

    assert "No orphan entities" in result


def test_gc_orphan_workflow_never_calls_history_retention(isolated_memory):
    _seed_vendor(isolated_memory, edge_count=2)

    with patch.object(
        tool_gc,
        "prune_runtime_history",
        side_effect=AssertionError("history retention must remain decoupled"),
    ):
        preview = gc_vector_lake(days=30, dry_run=True)
        apply_without_confirmation = gc_vector_lake(days=30, dry_run=False)

    assert "History retention was not scanned" in preview
    assert "History retention was not scanned" in apply_without_confirmation


def test_gc_does_not_scan_history_when_no_orphan_pages(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set_history(conn, "changeset_old")
        conn.execute(
            "INSERT INTO change_set_idempotency (idempotency_key, change_set_id, created_at) "
            "VALUES (?, ?, ?)",
            ("idem_old", "changeset_old", "2000-01-01T00:00:00+00:00"),
        )

    result = gc_vector_lake(days=30, dry_run=False)

    assert "History retention was not scanned" in result
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM change_set_idempotency").fetchone()[0] == 1
    )


def test_gc_dry_run_missing_database_is_zero_write(isolated_memory):
    meta_dir = isolated_memory / "wiki" / ".meta"

    result = gc_vector_lake(days=30, dry_run=True)

    assert "[DRY-RUN] GC unavailable" in result
    assert "No changes made" in result
    assert not meta_dir.exists()


def test_gc_apply_leaves_history_to_dedicated_workflow(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    old = "2000-01-01T00:00:00+00:00"
    change_sets = {
        "terminal-delete": {"status": "applied"},
        "terminal-referenced": {"status": "applied"},
        "pending-protected": {"status": "pending"},
    }
    with db_store.transaction():
        for change_set_id, body in change_sets.items():
            _insert_change_set_history(
                conn,
                change_set_id,
                status=str(body["status"]),
                timestamp=old,
            )
            conn.execute(
                "INSERT INTO change_set_idempotency "
                "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
                (f"idem-{change_set_id}", change_set_id, old),
            )
        conn.execute(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            (
                "queue-active",
                json.dumps(
                    {
                        "item_id": "queue-active",
                        "status": "pending",
                        "change_set_id": "terminal-referenced",
                    }
                ),
                old,
            ),
        )

    result = gc_vector_lake(days=30, dry_run=False)

    remaining = {
        row[0]
        for row in conn.execute("SELECT change_set_id FROM change_sets").fetchall()
    }
    idempotency = {
        row[0]
        for row in conn.execute(
            "SELECT change_set_id FROM change_set_idempotency"
        ).fetchall()
    }
    assert "History retention was not scanned" in result
    assert remaining == set(change_sets)
    assert idempotency == remaining


def _orphan_fingerprint(report: str) -> str:
    match = re.search(
        r"Orphan candidate fingerprint: (sha256:[0-9a-f]{64})",
        report,
    )
    assert match is not None
    return match.group(1)


def test_gc_preview_fingerprint_is_stable_for_unchanged_candidates(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)

    first = gc_vector_lake(days=30, dry_run=True)
    second = gc_vector_lake(days=30, dry_run=True)

    assert _orphan_fingerprint(first) == _orphan_fingerprint(second)
    assert "No changes made" in first


def test_gc_plain_apply_retains_orphans_and_only_runs_safe_phase(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    page = isolated_memory / "wiki" / "Vendor_Acme.md"

    with patch("vector_lake.mutation_coordinator.execute_mutation_batch") as execute:
        result = gc_vector_lake(days=30, dry_run=False)

    assert "Orphan deletion was not confirmed" in result
    assert "retained 1 candidate page(s)" in result
    assert page.exists()
    execute.assert_not_called()


def test_gc_matching_fingerprint_explicitly_enables_orphan_deletion(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)

    def execute_callbacks(_mutations, **kwargs):
        kwargs["precondition_callback"]()
        with db_store.transaction():
            kwargs["transaction_callback"]([])
        return {
            "ok": True,
            "committed": True,
            "deferred": [],
            "post_commit_warnings": [],
        }

    with patch(
        "vector_lake.mutation_coordinator.execute_mutation_batch",
        side_effect=execute_callbacks,
    ) as execute:
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Deleted 1 orphan pages" in result
    mutations = execute.call_args.args[0]
    assert mutations[0]["filename"] == "Vendor_Acme.md"
    assert mutations[0]["is_delete"] is True
    assert len(mutations[0]["expected_projection_hash"]) == 64
    assert execute.call_args.kwargs["return_details"] is True
    assert callable(execute.call_args.kwargs["precondition_callback"])
    assert callable(execute.call_args.kwargs["transaction_callback"])


def test_gc_symlink_candidate_is_blocked_before_retention_or_backup(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    conn = _seed_old_history("symlink-candidate-history")
    page = isolated_memory / "wiki" / "Vendor_Acme.md"
    external_target = isolated_memory / "raw" / "external-vendor.md"
    external_target.write_text("external vendor", encoding="utf-8")
    page.unlink()
    try:
        page.symlink_to(external_target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"File symlink creation is unavailable: {exc}")

    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)
    result = gc_vector_lake(
        days=30,
        dry_run=False,
        orphan_confirmation=fingerprint,
    )

    assert "[BLOCKED]" in preview
    assert "symbolic link" in preview
    assert "[BLOCKED]" in result
    assert "no changes made" in result.casefold()
    assert page.is_symlink()
    assert external_target.read_text(encoding="utf-8") == "external vendor"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
            ("symlink-candidate-history",),
        ).fetchone()[0]
        == 1
    )
    assert not (isolated_memory / "backup" / "gc").exists()


def _publish_test_orphan_backup():
    entities, edges, schema_state = tool_gc._read_gc_snapshot()
    assert schema_state["ready"] is True
    assert entities is not None and edges is not None
    now = time.time()
    candidates, errors = tool_gc._orphan_candidates(
        entities=entities,
        edges=edges,
        days=30,
        now=now,
    )
    assert errors == []
    assert len(candidates) == 1
    fingerprint = tool_gc._orphan_candidate_fingerprint(30, candidates)
    backup_dir = tool_gc._publish_gc_backup(
        days=30,
        fingerprint=fingerprint,
        candidates=candidates,
        now=now,
    )
    return fingerprint, backup_dir


def test_gc_snapshot_uses_compact_page_key_index_scan(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    governance_store.upsert_entity(
        "entity_compact_snapshot",
        {
            "entity_id": "entity_compact_snapshot",
            "page_key": "Vendor_Compact-Snapshot",
            "type": "vendor",
            "large_payload": "x" * 100_000,
        },
    )
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        entities, _edges = tool_gc._gc_snapshot_from_connection(conn)
    finally:
        conn.set_trace_callback(None)

    assert entities["items"]["entity_compact_snapshot"] == {
        "page_key": "Vendor_Compact-Snapshot",
        "type": "vendor",
    }
    assert any(
        "INDEXED BY idx_entities_page_key" in statement for statement in statements
    )


def test_gc_plain_path_stat_rejects_windows_reparse_attribute(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "reparse-candidate"
    target.mkdir()
    actual = target.lstat()
    path_type = type(target)
    real_lstat = path_type.lstat

    class ReparseStat:
        st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(actual, name)

    def fake_lstat(self):
        if self == target:
            return ReparseStat()
        return real_lstat(self)

    monkeypatch.setattr(tool_gc.stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    monkeypatch.setattr(path_type, "lstat", fake_lstat)

    with pytest.raises(RuntimeError, match="reparse point"):
        tool_gc._plain_path_stat(target, directory=True)


def test_gc_existing_backup_directory_symlink_blocks_canonical_delete(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))
    backup_root = isolated_memory / "backup" / "gc"
    backup_root.mkdir(parents=True)
    external = isolated_memory / "external-backup-dir"
    external.mkdir()
    backup_dir = backup_root / fingerprint.removeprefix("sha256:")[:16]
    try:
        backup_dir.symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlink creation is unavailable: {exc}")

    result = gc_vector_lake(
        days=30,
        dry_run=False,
        orphan_confirmation=fingerprint,
    )

    assert "symbolic link or reparse point" in result
    assert "No canonical deletion committed" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert external.is_dir()
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM entities WHERE entity_id = 'entity-internal-id'")
        .fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("entry_name", ["manifest.json", "Vendor_Acme.md"])
def test_gc_existing_backup_leaf_symlink_blocks_canonical_delete(
    isolated_memory,
    entry_name,
):
    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint, backup_dir = _publish_test_orphan_backup()
    target = backup_dir / entry_name
    external = isolated_memory / f"external-{entry_name}"
    external.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(external)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"File symlink creation is unavailable: {exc}")

    result = gc_vector_lake(
        days=30,
        dry_run=False,
        orphan_confirmation=fingerprint,
    )

    assert "symbolic link or reparse point" in result
    assert "No canonical deletion committed" in result
    assert external.exists()
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM entities WHERE entity_id = 'entity-internal-id'")
        .fetchone()[0]
        == 1
    )


def test_gc_staging_cleanup_failure_preserves_primary_error(
    isolated_memory,
    caplog,
):
    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))

    with (
        patch(
            "vector_lake.tool_gc.shutil.copyfile",
            side_effect=OSError("primary copy failure"),
        ),
        patch(
            "vector_lake.tool_gc._remove_plain_gc_tree",
            side_effect=RuntimeError("cleanup failure"),
        ),
        caplog.at_level("WARNING"),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "primary copy failure" in result
    assert "cleanup failure" in caplog.text
    assert "No canonical deletion committed" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()


def test_gc_backup_fsync_failure_blocks_canonical_delete(isolated_memory):
    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))

    with patch.object(
        tool_gc.os,
        "fsync",
        side_effect=OSError("injected fsync failure"),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "injected fsync failure" in result
    assert "No canonical deletion committed" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    backup_root = isolated_memory / "backup" / "gc"
    assert not backup_root.exists() or list(backup_root.iterdir()) == []


def test_gc_backup_copy_failure_leaves_no_formal_backup_or_db_mutation(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    conn = _seed_old_history("copy-failure-history")
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))

    with patch(
        "vector_lake.tool_gc.shutil.copyfile",
        side_effect=OSError("injected copy failure"),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "injected copy failure" in result
    assert "No canonical deletion committed" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE json_extract(data_json, '$.page_key') = ?",
            ("Vendor_Acme",),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
            ("copy-failure-history",),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_set_idempotency WHERE change_set_id = ?",
            ("copy-failure-history",),
        ).fetchone()[0]
        == 1
    )
    backup_root = isolated_memory / "backup" / "gc"
    assert not backup_root.exists() or list(backup_root.iterdir()) == []


def test_gc_canonical_delete_failure_rolls_back_and_preserves_history(
    isolated_memory,
):
    from vector_lake import runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    conn = _seed_old_history("canonical-failure-history")
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))
    real_delete = db_store.delete_node_cascade

    def delete_then_fail(node_key):
        real_delete(node_key)
        raise RuntimeError("injected canonical delete failure")

    with (
        patch.object(runtime_health, "enforce_runtime_write_health"),
        patch.object(
            db_store,
            "delete_node_cascade",
            side_effect=delete_then_fail,
        ),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "injected canonical delete failure" in result
    assert "No canonical deletion committed" in result
    assert "transaction committed" not in result
    assert "Verified backup retained" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE json_extract(data_json, '$.page_key') = ?",
            ("Vendor_Acme",),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
            ("canonical-failure-history",),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_set_idempotency WHERE change_set_id = ?",
            ("canonical-failure-history",),
        ).fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 0
    backup_root = isolated_memory / "backup" / "gc"
    backups = [
        entry
        for entry in backup_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    ]
    assert len(backups) == 1
    assert not any(entry.name.startswith(".") for entry in backup_root.iterdir())
    manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fingerprint"] == fingerprint
    assert manifest["files"][0]["filename"] == "Vendor_Acme.md"


def test_gc_notification_failure_reports_committed_canonical_and_outbox(
    isolated_memory,
):
    from vector_lake import mutation_coordinator, runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    conn = _seed_old_history("notification-failure-history")
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))

    with (
        patch.object(runtime_health, "enforce_runtime_write_health"),
        patch.object(
            mutation_coordinator,
            "_signal_outbox_consumer",
            side_effect=RuntimeError("injected notification failure"),
        ),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Canonical transaction committed" in result
    assert "post-commit warning(s)" in result
    assert "injected notification failure" in result
    assert "No canonical deletion committed" not in result
    assert (
        conn.execute(
            "SELECT 1 FROM entities WHERE json_extract(data_json, '$.page_key') = ?",
            ("Vendor_Acme",),
        ).fetchone()
        is None
    )
    outbox = conn.execute(
        "SELECT mutation_type, status FROM mutation_outbox WHERE filename = ?",
        ("Vendor_Acme.md",),
    ).fetchone()
    assert outbox is not None
    assert tuple(outbox) == ("delete", "pending")
    assert (
        conn.execute(
            "SELECT 1 FROM change_sets WHERE change_set_id = ?",
            ("notification-failure-history",),
        ).fetchone()
        is not None
    )


def test_gc_projection_failure_counts_canonical_delete_as_committed(
    isolated_memory,
):
    from vector_lake import mutation_coordinator, runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))

    with (
        patch.object(runtime_health, "enforce_runtime_write_health"),
        patch.object(
            mutation_coordinator,
            "materialize_markdown_projection",
            side_effect=RuntimeError("injected projection failure"),
        ),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Canonical transaction committed" in result
    assert "Deleted 1 orphan pages from canonical state" in result
    assert "1 Markdown projection deletion(s) were deferred" in result
    assert "post-commit warning(s)" in result
    assert "injected projection failure" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    conn = db_store.get_connection()
    assert (
        conn.execute(
            "SELECT 1 FROM entities WHERE json_extract(data_json, '$.page_key') = ?",
            ("Vendor_Acme",),
        ).fetchone()
        is None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM mutation_outbox WHERE filename = ?",
            ("Vendor_Acme.md",),
        ).fetchone()
        is not None
    )


def test_gc_confirmed_success_commits_canonical_and_receipt_without_history_scan(
    isolated_memory,
):
    from vector_lake import runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    conn = _seed_old_history("successful-gc-history")
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))

    with patch.object(runtime_health, "enforce_runtime_write_health"):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Canonical transaction committed" in result
    assert "Deleted 1 orphan pages from canonical state" in result
    assert "History retention was not scanned" in result
    assert not (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities "
            "WHERE json_extract(data_json, '$.page_key') = ?",
            ("Vendor_Acme",),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
            ("successful-gc-history",),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_set_idempotency WHERE change_set_id = ?",
            ("successful-gc-history",),
        ).fetchone()[0]
        == 1
    )
    backup_root = isolated_memory / "backup" / "gc"
    backups = [
        entry
        for entry in backup_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    ]
    assert len(backups) == 1
    manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
    record = manifest["files"][0]
    backup_file = backups[0] / record["filename"]
    assert record["size"] == backup_file.stat().st_size
    assert (
        record["content_sha256"] == hashlib.sha256(backup_file.read_bytes()).hexdigest()
    )
    receipt_path = (
        isolated_memory
        / "wiki"
        / ".meta"
        / "gc-runs"
        / f"{fingerprint.removeprefix('sha256:')}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "committed"
    assert receipt["fingerprint"] == fingerprint
    assert receipt["backup_name"] == backups[0].name
    assert len(receipt["outbox_ids"]) == 1


def test_gc_receipt_does_not_self_block_the_runtime_health_gate(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))
    observed_receipt_roots = []
    real_gate = runtime_health.enforce_runtime_write_health

    def observed_gate(*args, **kwargs):
        receipt_root = isolated_memory / "wiki" / ".meta" / "gc-runs"
        observed_receipt_roots.append(receipt_root.exists())
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(runtime_health, "enforce_runtime_write_health", observed_gate)
    result = gc_vector_lake(
        days=30,
        dry_run=False,
        orphan_confirmation=fingerprint,
    )

    assert "Canonical transaction committed" in result
    assert observed_receipt_roots == [False]


def test_gc_stale_fingerprint_blocks_orphan_and_history_mutation(
    isolated_memory,
):
    _seed_vendor(isolated_memory, edge_count=1)
    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)
    page = isolated_memory / "wiki" / "Vendor_Acme.md"
    page.write_text("changed vendor", encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(page, (old, old))
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set_history(
            conn,
            "stale-token-history",
            timestamp="2000-01-01T00:00:00+00:00",
        )

    result = gc_vector_lake(
        days=30,
        dry_run=False,
        orphan_confirmation=fingerprint,
    )

    assert "[BLOCKED]" in result
    assert "No changes made" in result
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE change_set_id = ?",
            ("stale-token-history",),
        ).fetchone()[0]
        == 1
    )


def test_gc_confirmed_delete_accepts_existing_legacy_filename(isolated_memory):
    from vector_lake import runtime_health

    db_store.init_db()
    page_key = "Vendor_Acme_Legacy_20260516"
    filename = f"{page_key}.md"
    governance_store.save_entities(
        {
            "items": {
                "entity-legacy-filename": {
                    "entity_id": "entity-legacy-filename",
                    "canonical_name": "Acme Legacy",
                    "page_key": page_key,
                    "type": "vendor",
                    "status": "Active",
                }
            }
        }
    )
    page = isolated_memory / "wiki" / filename
    page.write_text("legacy vendor", encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(page, (old, old))
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claim_graph_edges "
            "(source_id, target_id, relation, weight, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (page_key, "Concept_0", "related", 1.0, "2026-01-01"),
        )

    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)
    assert filename in preview

    with patch.object(runtime_health, "enforce_runtime_write_health"):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Canonical transaction committed" in result
    assert not page.exists()
    outbox = conn.execute(
        "SELECT mutation_type, validation_mode FROM mutation_outbox "
        "WHERE filename = ?",
        (filename,),
    ).fetchone()
    assert tuple(outbox) == ("delete", "schema")


def test_gc_missing_backup_is_reported_by_receipt_and_write_health(isolated_memory):
    from vector_lake import runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    fingerprint = _orphan_fingerprint(gc_vector_lake(days=30, dry_run=True))
    with patch.object(runtime_health, "enforce_runtime_write_health"):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )
    assert "Canonical transaction committed" in result

    backup_name = fingerprint.removeprefix("sha256:")[:16]
    shutil.rmtree(isolated_memory / "backup" / "gc" / backup_name)

    verification = tool_gc.verify_gc_recovery_receipts(deep=True)
    assert any(
        issue.startswith("gc_receipt_invalid:")
        for issue in verification["issues"]
    )
    health = runtime_health.assess_runtime_health()
    assert health["ok"] is False
    assert any(
        issue.startswith("gc_receipt_invalid:") for issue in health["issues"]
    )


def test_gc_rechecks_canonical_edges_inside_mutation_precondition(
    isolated_memory,
):
    from vector_lake import mutation_coordinator, runtime_health

    _seed_vendor(isolated_memory, edge_count=1)
    preview = gc_vector_lake(days=30, dry_run=True)
    fingerprint = _orphan_fingerprint(preview)
    real_execute = mutation_coordinator.execute_mutation_batch

    def inject_relation_before_transaction(mutations, **kwargs):
        conn = db_store.get_connection()
        with db_store.transaction():
            conn.execute(
                "INSERT INTO claim_graph_edges "
                "(source_id, target_id, relation, weight, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "Vendor_Acme",
                    "Concept_concurrent",
                    "related",
                    1.0,
                    "2026-07-27T00:00:00+00:00",
                ),
            )
        return real_execute(mutations, **kwargs)

    with (
        patch.object(
            mutation_coordinator,
            "execute_mutation_batch",
            side_effect=inject_relation_before_transaction,
        ),
        patch.object(runtime_health, "enforce_runtime_write_health"),
    ):
        result = gc_vector_lake(
            days=30,
            dry_run=False,
            orphan_confirmation=fingerprint,
        )

    assert "Confirmed orphan GC was blocked" in result
    assert "candidate set changed after confirmation" in result
    assert (isolated_memory / "wiki" / "Vendor_Acme.md").exists()
    assert (
        db_store.get_connection()
        .execute(
            "SELECT 1 FROM entities WHERE json_extract(data_json, '$.page_key') = ?",
            ("Vendor_Acme",),
        )
        .fetchone()
        is not None
    )
    assert (
        db_store.get_connection().execute("SELECT 1 FROM mutation_outbox").fetchone()
        is None
    )
