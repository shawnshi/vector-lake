from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path

import psutil
import pytest

from vector_lake import db_store, governance_store, indexer, tool_projection


def _physical_tree_identity(root: Path) -> list[tuple]:
    result = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        details = path.stat()
        result.append(
            (
                str(path.relative_to(root)),
                path.is_dir(),
                int(details.st_size),
                int(details.st_mtime_ns),
                int(details.st_ctime_ns),
            )
        )
    return result


def _sample_index() -> dict:
    return {
        "nodes": {
            "Concept_A": {
                "id": "urn:a",
                "title": "A",
                "summary": "alpha",
                "raw_text": "alpha body",
                "type": "concept",
                "categories": ["Cat"],
                "aliases": ["Alpha"],
                "links": ["Concept_B"],
                "sources": ["source-1"],
                "triples": [],
            },
            "Concept_B": {
                "id": "urn:b",
                "title": "B",
                "summary": "beta",
                "raw_text": "beta body",
                "type": "concept",
                "categories": ["Cat"],
                "aliases": [],
                "links": [],
                "sources": ["source-1"],
                "triples": [],
            },
        },
        "aliases": {"urn:a": "Concept_A", "Alpha": "Concept_A"},
        "categories": ["Cat"],
        "weighted_edges": [
            {"source": "Concept_A", "target": "Concept_B", "weight": 4.0}
        ],
        "error_log": [{"file": "bad.md", "error": "invalid"}],
        "communities": {},
        "community_labels": {},
        "graph_insights": [],
        "graph_state": {"dirty": True, "reason": "test", "updated_at": None},
        "governance_metrics": {"debt": 1},
        "schema_version": "9.0",
    }


def _sample_claim_graph() -> dict:
    return {
        "nodes": [{"id": "claim:a", "text": "A is true"}],
        "edges": [
            {"source": "claim:a", "target": "claim:b", "weight": 1.0}
        ],
        "schema_version": "1.0",
    }


def _migrated_v8_database_with_legacy_v1_projection() -> Path:
    """Build a physical v8/v1 fixture, then run the real v8-to-v9 migration."""
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_projection_v1_migration",
        {
            "entity_id": "entity_projection_v1_migration",
            "page_key": "Concept_Projection-V1-Migration",
            "canonical_name": "Projection V1 Migration",
            "type": "concept",
            "summary": "Legacy projection migration fixture.",
            "raw_text": "Legacy projection migration fixture body.",
        },
    )
    indexer.generate_index()
    canonical_generation = indexer.canonical_runtime_generation_snapshot()
    canonical_binding = indexer._verified_canonical_generation(
        canonical_generation
    )
    index_data = indexer.read_committed_index_snapshot(_mutable=True)
    claim_graph_data = indexer._read_claim_graph_snapshot(
        str(indexer.get_claim_graph_path())
    )
    output_path = str(indexer.get_index_path())
    tmp_output, tmp_claim, _manifest = indexer._stage_projection_pair(
        output_path,
        index_data,
        claim_graph_data,
        canonical_binding,
    )
    indexer._publish_staged_projection_pair_guarded(
        output_path,
        tmp_output,
        tmp_claim,
        canonical_generation,
    )

    path = db_store.get_db_path().resolve()
    db_store.close_all_connections()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE projection_runtime_v9")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("PRAGMA user_version = 8")
        connection.commit()
    finally:
        connection.close()
    db_store._INITIALIZED_DB_PATHS.discard(str(path))

    preview = db_store.preview_schema_migration(path)
    result = db_store.schema_migration_maintenance(
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )
    assert result["applied"] is True
    assert result["projection_rebuild_required"] is True
    assert db_store.get_projection_runtime_v9()["status"] == "rebuild_required"
    return path


def _inflate_legacy_v1_index(target_bytes: int = 35_774_105) -> None:
    index_path = indexer.get_index_path()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["legacy_padding"] = ""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["legacy_padding"] = "x" * (target_bytes - len(encoded))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) == target_bytes
    index_path.write_bytes(encoded)

    sidecar_path = indexer.get_projection_manifest_path()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["artifacts"][index_path.name] = {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")


def _schema_rollback_receipt_directory() -> tuple[Path, Path]:
    database_path = db_store.peek_db_path()
    receipt_dir = database_path.parent / "schema-migration-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    return database_path, receipt_dir


def _projection_diff_fixture() -> dict[str, set[str]]:
    return {
        "wiki": {"Concept_A"},
        "canonical": {"Concept_A"},
        "index": set(),
        "missing_index": {"Concept_A"},
        "extra_index": set(),
        "missing_canonical": set(),
        "extra_canonical": set(),
    }


def _write_self_bound_pending_receipt(database_path: Path) -> Path:
    migration_fingerprint = "sha256:" + "a" * 64
    migration_binding = {
        "path": str(database_path.with_name("migration.json").resolve()),
        "file_identity": {"exists": True},
        "file_sha256": "sha256:" + "b" * 64,
        "receipt_fingerprint": migration_fingerprint,
        "plan_fingerprint": "sha256:" + "c" * 64,
    }
    current_source = {
        "physical_identity": [],
        "database_sha256": "sha256:" + "d" * 64,
        "logical_database_sha256": "sha256:" + "e" * 64,
        "schema_state": {"user_version": 9},
        "runtime_generations": {},
        "projection": {},
    }
    restore = {
        "database": {},
        "schema_state": {"user_version": 8},
        "runtime_generations": {},
        "pre_projection": {},
        "projection_backup": {},
    }
    completed_path, pending_path = db_store._schema_rollback_receipt_paths(
        database_path,
        migration_binding,
    )
    plan = {
        "contract": "vector-lake-schema-rollback-plan/v1",
        "database_path": str(database_path.resolve()),
        "source_schema_version": 9,
        "target_schema_version": 8,
        "migration_receipt": migration_binding,
        "current_source": current_source,
        "restore": restore,
        "forward_recovery": {
            "database_path": str(database_path),
            "projection_directory": str(database_path.with_suffix(".projection")),
            "completed_receipt_path": str(completed_path),
            "pending_receipt_path": str(pending_path),
            "reuse_existing": False,
            "reuse_database": False,
            "reuse_projection": False,
            "database": None,
            "projection": None,
        },
        "data_loss_since_migration": False,
        "changed_runtime_generations": [],
        "confirm_data_rewind_required": False,
        "projection_action": "restore_pre_migration_pair",
        "old_runtime_projection_rebuild_required": False,
        "recovery_action": None,
        "pending_rollback_receipt": None,
        "completed_rollback_receipt": None,
        "issues": [],
        "can_apply": True,
        "no_op": False,
    }
    plan["fingerprint"] = db_store._schema_migration_fingerprint(
        db_store._schema_rollback_plan_core(plan)
    )
    pending = db_store._schema_rollback_pending_payload(
        plan,
        forward_database={},
        forward_projection={},
    )
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    return pending_path


def _complete_real_schema_rollback() -> tuple[Path, Path, dict, dict]:
    database_path = _migrated_v8_database_with_legacy_v1_projection()
    migration_receipt, _migration_pending = (
        db_store._schema_migration_receipt_paths(database_path)
    )
    original_migration = json.loads(
        migration_receipt.read_text(encoding="utf-8")
    )
    db_store.close_all_connections()
    rollback_preview = db_store.preview_schema_rollback(
        migration_receipt,
        database_path,
    )
    assert rollback_preview["can_apply"] is True, rollback_preview["issues"]
    rollback = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=rollback_preview["fingerprint"],
        confirm_no_writers=True,
        db_path=database_path,
    )
    assert rollback["applied"] is True
    return database_path, migration_receipt, original_migration, rollback


@pytest.mark.parametrize(
    "payload",
    (
        '{"contract":"vector-lake-schema-rollback-receipt/v1",'
        '"status":"pending"}',
        "{malformed",
    ),
    ids=("valid-json", "malformed"),
)
def test_schema_rollback_pending_guard_blocks_same_database_receipt(payload):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    pending_path = receipt_dir / (
        f"{database_path.name}.rollback-v9-to-v8."
        "000000000000000000000000.pending.json"
    )
    pending_path.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="schema_rollback_pending"):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_blocks_non_regular_and_symlink_entries(
    tmp_path,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    pending_directory = receipt_dir / (
        f"{database_path.name}.rollback-v9-to-v8."
        "000000000000000000000000.pending.json"
    )
    pending_directory.mkdir()
    with pytest.raises(RuntimeError, match="schema_rollback_pending"):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)
    pending_directory.rmdir()

    target = tmp_path / "rollback-receipt-target.json"
    target.write_text("{}", encoding="utf-8")
    pending_symlink = receipt_dir / (
        f"{database_path.name}.rollback-v9-to-v8."
        "111111111111111111111111.pending.json"
    )
    try:
        pending_symlink.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable in this Windows context")
    with pytest.raises(RuntimeError, match="schema_rollback_pending"):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_ignores_real_other_database_receipt(
    monkeypatch,
):
    database_path, _receipt_dir = _schema_rollback_receipt_directory()
    other_database = database_path.with_name("other-vector-lake.db").resolve()
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(other_database))
    db_store.close_all_connections()
    _other_path, _migration, _original, rollback = (
        _complete_real_schema_rollback()
    )
    Path(rollback["receipt_path"]).unlink()
    assert Path(rollback["pending_receipt_path"]).is_file()

    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(database_path.resolve()))
    db_store.close_all_connections()

    def forbidden_external_validator(*_args, **_kwargs):
        raise AssertionError("rollback guard invoked an external validator")

    with monkeypatch.context() as receipt_only:
        for name in (
            "_schema_rollback_validate_receipt",
            "_schema_migration_validate_receipt",
            "_schema_migration_sha256",
            "_schema_rollback_logical_database_sha256",
            "_schema_migration_validate_projection_backup",
        ):
            receipt_only.setattr(
                db_store,
                name,
                forbidden_external_validator,
            )
        assert (
            db_store.assert_no_schema_rollback_pending_receipt(database_path)
            is None
        )


def test_schema_rollback_pending_guard_blocks_renamed_current_database_receipt():
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    pending_path = _write_self_bound_pending_receipt(database_path)
    renamed = receipt_dir / f"other-{pending_path.name}"
    pending_path.rename(renamed)

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_filename_database_mismatch",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_blocks_current_name_for_other_database():
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    other_database = database_path.with_name("other-vector-lake.db")
    other_pending = _write_self_bound_pending_receipt(other_database)
    mismatched = receipt_dir / other_pending.name.replace(
        other_database.name,
        database_path.name,
        1,
    )
    other_pending.rename(mismatched)

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_filename_database_mismatch",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_blocks_malformed_other_database_candidate():
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    malformed = receipt_dir / (
        "other-vector-lake.db.rollback-v9-to-v8."
        "000000000000000000000000.pending.json"
    )
    malformed.write_text("{}", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_candidate_invalid",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_blocks_invalid_other_migration_fingerprint():
    database_path, _receipt_dir = _schema_rollback_receipt_directory()
    other_database = database_path.with_name("other-vector-lake.db")
    pending_path = _write_self_bound_pending_receipt(other_database)
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["migration_receipt"]["receipt_fingerprint"] = "malformed"
    pending["plan"]["migration_receipt"] = pending["migration_receipt"]
    pending["plan_fingerprint"] = db_store._schema_migration_fingerprint(
        pending["plan"]
    )
    pending["receipt_fingerprint"] = db_store._schema_migration_fingerprint(
        {key: value for key, value in pending.items() if key != "receipt_fingerprint"}
    )
    pending_path.write_text(json.dumps(pending), encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_candidate_invalid",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_rejects_inconsistent_terminal_pair():
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    basename = (
        f"{database_path.name}.rollback-v9-to-v8."
        "000000000000000000000000"
    )
    (receipt_dir / f"{basename}.pending.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (receipt_dir / f"{basename}.json").write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="schema_rollback_pending"):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_terminal_remains_terminal_after_generation_progress(
    monkeypatch,
):
    database_path, _migration_receipt, original_migration, rollback = (
        _complete_real_schema_rollback()
    )
    pending_path = Path(rollback["pending_receipt_path"])
    completed_path = Path(rollback["receipt_path"])
    assert pending_path.is_file()
    assert completed_path.is_file()

    remigration_preview = db_store.preview_schema_migration(database_path)
    assert remigration_preview["can_apply"] is True, remigration_preview["issues"]
    remigration = db_store.schema_migration_maintenance(
        apply=True,
        confirmation=remigration_preview["fingerprint"],
        confirm_no_writers=True,
        db_path=database_path,
    )
    assert remigration["applied"] is True
    remigration_payload = json.loads(
        Path(remigration["receipt_path"]).read_text(encoding="utf-8")
    )
    assert (
        remigration_payload["receipt_fingerprint"]
        != original_migration["receipt_fingerprint"]
    )
    generation_before = indexer.canonical_runtime_generation_snapshot()
    governance_store.upsert_entity(
        "entity_after_remigration",
        {
            "entity_id": "entity_after_remigration",
            "page_key": "Concept_After-Remigration",
            "canonical_name": "After Remigration",
            "type": "concept",
            "summary": "Generation progress after a completed rollback cycle.",
            "raw_text": "The terminal rollback receipt must remain terminal.",
        },
    )
    generation_after = indexer.canonical_runtime_generation_snapshot()
    assert generation_after["entities"] > generation_before["entities"]

    def forbidden_external_validator(*_args, **_kwargs):
        raise AssertionError("rollback guard invoked an external validator")

    with monkeypatch.context() as receipt_only:
        for name in (
            "_schema_rollback_validate_receipt",
            "_schema_migration_validate_receipt",
            "_schema_migration_sha256",
            "_schema_rollback_logical_database_sha256",
            "_schema_migration_validate_projection_backup",
        ):
            receipt_only.setattr(
                db_store,
                name,
                forbidden_external_validator,
            )
        assert (
            db_store.assert_no_schema_rollback_pending_receipt(database_path)
            is None
        )


def test_schema_rollback_pending_guard_rejects_swapped_terminal_payloads():
    database_path, _migration_receipt, _original, rollback = (
        _complete_real_schema_rollback()
    )
    pending_path = Path(rollback["pending_receipt_path"])
    completed_path = Path(rollback["receipt_path"])
    pending_bytes = pending_path.read_bytes()
    completed_bytes = completed_path.read_bytes()
    pending_path.write_bytes(completed_bytes)
    completed_path.write_bytes(pending_bytes)

    with pytest.raises(RuntimeError, match="schema_rollback_pending"):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_rejects_reparse_completed_terminal(
    tmp_path,
):
    database_path, _migration_receipt, _original, rollback = (
        _complete_real_schema_rollback()
    )
    completed_path = Path(rollback["receipt_path"])
    target = tmp_path / "completed-rollback-target.json"
    target.write_bytes(completed_path.read_bytes())
    completed_path.unlink()
    try:
        completed_path.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable in this Windows context")

    with pytest.raises(RuntimeError, match="schema_rollback_pending"):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


@pytest.mark.parametrize(
    "rollback_core",
    (
        "v10-to-v9.000000000000000000000000",
        "v9-to-v7.000000000000000000000000",
        "v9-tov8.000000000000000000000000",
        "v9-to-v8.short",
    ),
)
def test_schema_rollback_pending_guard_blocks_unknown_rollback_contract(
    rollback_core,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    candidate = receipt_dir / (
        f"{database_path.name}.rollback-{rollback_core}.pending.json"
    )
    candidate.write_text("{}", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_unknown_contract",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_rejects_reparse_receipt_directory(
    tmp_path,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    target = tmp_path / "rollback-receipt-directory-target"
    target.mkdir()
    receipt_dir.rmdir()
    try:
        receipt_dir.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_scan_unsafe_directory",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_rejects_directory_identity_drift(
    monkeypatch,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    (receipt_dir / "unrelated.json").write_text("{}", encoding="utf-8")
    real_identity = db_store._schema_rollback_directory_identity
    calls = 0

    def drifting_identity(path):
        nonlocal calls
        calls += 1
        identity = real_identity(path)
        if calls == 2:
            return (*identity[:-1], identity[-1] + 1)
        return identity

    monkeypatch.setattr(
        db_store,
        "_schema_rollback_directory_identity",
        drifting_identity,
    )
    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_scan_directory_changed",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_fails_closed_at_scan_limit(monkeypatch):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    monkeypatch.setattr(db_store, "_SCHEMA_ROLLBACK_PENDING_SCAN_LIMIT", 2)
    for index in range(3):
        (receipt_dir / f"unrelated-{index}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_scan_limit_exceeded",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_schema_rollback_pending_guard_fails_closed_at_receipt_byte_budget(
    monkeypatch,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    basename = (
        f"{database_path.name}.rollback-v9-to-v8."
        "000000000000000000000000"
    )
    (receipt_dir / f"{basename}.pending.json").write_text("{}", encoding="utf-8")
    (receipt_dir / f"{basename}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(db_store, "_SCHEMA_ROLLBACK_RECEIPT_SCAN_MAX_BYTES", 1)

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending_receipt_bytes_exceeded",
    ):
        db_store.assert_no_schema_rollback_pending_receipt(database_path)


def test_projection_rebuild_pending_rollback_dry_run_is_read_only_and_apply_stops(
    isolated_memory,
    monkeypatch,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    _write_self_bound_pending_receipt(database_path)
    monkeypatch.setattr(
        tool_projection,
        "_diff_sets",
        lambda **_kwargs: _projection_diff_fixture(),
    )

    before_dry_run = _physical_tree_identity(isolated_memory)
    preview = tool_projection.rebuild_index_projection(dry_run=True)
    assert "[DRY RUN]" in preview
    assert _physical_tree_identity(isolated_memory) == before_dry_run

    calls = []

    def forbidden_call(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"unexpected write path: {name}")

        return fail

    monkeypatch.setattr(
        tool_projection,
        "create_maintenance_backup",
        forbidden_call("backup"),
    )
    monkeypatch.setattr(tool_projection, "init_db", forbidden_call("init_db"))
    monkeypatch.setattr(db_store, "init_db", forbidden_call("db_store.init_db"))
    monkeypatch.setattr(indexer, "generate_index", forbidden_call("generate"))
    monkeypatch.setattr(
        indexer,
        "refresh_graph_topology_if_dirty",
        forbidden_call("topology"),
    )

    with pytest.raises(
        RuntimeError,
        match="schema_rollback_pending",
    ):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert calls == []


def test_projection_rebuild_schema_lock_closes_post_guard_pending_race(
    monkeypatch,
):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    pending_path = receipt_dir / (
        f"{database_path.name}.rollback-v9-to-v8.race.pending.json"
    )
    monkeypatch.setattr(
        tool_projection,
        "_diff_sets",
        lambda **_kwargs: _projection_diff_fixture(),
    )

    guard_observed = threading.Event()
    contender_done = threading.Event()
    contender_errors = []
    real_guard = db_store.assert_no_schema_rollback_pending_receipt

    def contend_for_schema_lock():
        if not guard_observed.wait(timeout=5):
            contender_errors.append("guard_timeout")
            contender_done.set()
            return
        try:
            with db_store.schema_maintenance_lock(database_path):
                pending_path.write_text("{}", encoding="utf-8")
        except RuntimeError as exc:
            contender_errors.append(str(exc))
        finally:
            contender_done.set()

    contender = threading.Thread(target=contend_for_schema_lock)
    contender.start()

    def observed_guard(path):
        real_guard(path)
        guard_observed.set()
        assert contender_done.wait(timeout=5)

    calls = []
    monkeypatch.setattr(
        db_store,
        "assert_no_schema_rollback_pending_receipt",
        observed_guard,
    )
    monkeypatch.setattr(
        tool_projection,
        "create_maintenance_backup",
        lambda _label: calls.append("backup") or receipt_dir,
    )

    def generate_without_race():
        assert not pending_path.exists()
        calls.append("generate")
        return receipt_dir / "index.json"

    monkeypatch.setattr(indexer, "generate_index", generate_without_race)
    monkeypatch.setattr(
        indexer,
        "refresh_graph_topology_if_dirty",
        lambda: calls.append("topology") or True,
    )
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )

    result = tool_projection.rebuild_index_projection(dry_run=False)
    contender.join(timeout=5)

    assert "Rebuilt index projection" in result
    assert not contender.is_alive()
    assert contender_errors == ["schema_maintenance_lock_busy"]
    assert calls == ["backup", "generate", "topology"]
    assert not pending_path.exists()

    with db_store.schema_maintenance_lock(database_path):
        pending_path.write_text("{}", encoding="utf-8")
    assert pending_path.is_file()


def test_projection_rebuild_apply_preflight_runs_under_schema_lock(monkeypatch):
    database_path, receipt_dir = _schema_rollback_receipt_directory()
    preflight_calls = []

    def locked_diff(**_kwargs):
        held = getattr(db_store._LOCAL, "schema_maintenance_lock", None)
        assert held is not None
        assert held[1].is_locked
        assert db_store.peek_db_path().resolve() == database_path.resolve()
        preflight_calls.append("diff")
        return _projection_diff_fixture()

    monkeypatch.setattr(tool_projection, "_diff_sets", locked_diff)
    monkeypatch.setattr(
        tool_projection,
        "create_maintenance_backup",
        lambda _label: receipt_dir,
    )
    monkeypatch.setattr(
        indexer,
        "generate_index",
        lambda: receipt_dir / "index.json",
    )
    monkeypatch.setattr(
        indexer,
        "refresh_graph_topology_if_dirty",
        lambda: True,
    )
    monkeypatch.setattr(
        indexer,
        "projection_pair_matches_current_generation",
        lambda: True,
    )

    result = tool_projection.rebuild_index_projection(dry_run=False)

    assert "Rebuilt index projection" in result
    assert preflight_calls == ["diff", "diff"]


def test_projection_rebuild_database_path_drift_stops_before_backup(monkeypatch):
    database_path, _receipt_dir = _schema_rollback_receipt_directory()
    drifted_path = database_path.with_name("drifted-vector-lake.db")
    observed_paths = iter((database_path, drifted_path))
    calls = []
    monkeypatch.setattr(
        tool_projection,
        "peek_db_path",
        lambda: next(observed_paths),
    )
    monkeypatch.setattr(
        tool_projection,
        "create_maintenance_backup",
        lambda _label: calls.append("backup"),
    )

    with pytest.raises(
        RuntimeError,
        match="projection_rebuild_database_path_changed",
    ):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert calls == []


@pytest.mark.parametrize(
    "root_name",
    (
        "meta",
        "wiki",
        "index",
        "claim_graph",
        "manifest",
        "projection_store",
    ),
)
def test_projection_rebuild_root_drift_stops_before_first_business_write(
    monkeypatch,
    root_name,
):
    _database_path, _receipt_dir = _schema_rollback_receipt_directory()
    initial = tool_projection._projection_rebuild_root_snapshot()
    drifted = dict(initial)
    drifted[root_name] = initial[root_name].with_name(
        initial[root_name].name + "-drifted"
    )
    snapshots = iter((initial, initial, drifted))
    calls = []

    monkeypatch.setattr(
        tool_projection,
        "_projection_rebuild_root_snapshot",
        lambda: next(snapshots),
    )
    monkeypatch.setattr(
        tool_projection,
        "_diff_sets",
        lambda **_kwargs: _projection_diff_fixture(),
    )
    monkeypatch.setattr(
        tool_projection,
        "create_maintenance_backup",
        lambda _label: calls.append("backup"),
    )
    monkeypatch.setattr(
        indexer,
        "generate_index",
        lambda: calls.append("generate"),
    )

    with pytest.raises(
        RuntimeError,
        match="projection_rebuild_root_snapshot_changed",
    ):
        tool_projection.rebuild_index_projection(dry_run=False)
    assert calls == []


def test_projection_rebuild_migrates_v8_legacy_v1_to_v2(
    isolated_memory,
    monkeypatch,
):
    _migrated_v8_database_with_legacy_v1_projection()
    _inflate_legacy_v1_index()
    index_path = indexer.get_index_path()

    with pytest.raises(
        Exception,
        match="legacy_index_requires_explicit_migration_reader",
    ):
        from vector_lake.index_snapshot import load_index_snapshot

        load_index_snapshot(index_path)

    before_dry_run = _physical_tree_identity(isolated_memory)
    dry_run = tool_projection.rebuild_index_projection(dry_run=True)
    assert "[DRY RUN]" in dry_run
    assert "from 1 canonical entity row(s)" in dry_run
    assert _physical_tree_identity(isolated_memory) == before_dry_run

    events = []
    backup_paths = []
    real_backup = tool_projection.create_maintenance_backup
    real_generate = indexer.generate_index

    def tracking_backup(label):
        events.append("backup")
        result = real_backup(label)
        backup_paths.append(Path(result))
        return result

    def tracking_generate(*args, **kwargs):
        events.append("generate")
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(tool_projection, "create_maintenance_backup", tracking_backup)
    monkeypatch.setattr(indexer, "generate_index", tracking_generate)
    applied = tool_projection.rebuild_index_projection(dry_run=False)
    assert "Rebuilt index projection" in applied
    assert events[:2] == ["backup", "generate"]
    assert len(backup_paths) == 1
    backup_manifest = json.loads(
        (backup_paths[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert backup_manifest["projection_format"] == 1
    assert backup_manifest["artifact_bytes"]["index.json"] == 35_774_105
    assert backup_manifest["artifact_sha256"]["index.json"] == hashlib.sha256(
        (backup_paths[0] / "index.json").read_bytes()
    ).hexdigest()
    assert indexer.projection_pair_matches_current_generation() is True
    assert db_store.get_projection_runtime_v9()["status"] == "ready"
    from vector_lake.projection_format_v2 import locator_payload

    assert json.loads(index_path.read_text(encoding="utf-8")) == locator_payload(
        "index"
    )


def test_projection_rebuild_legacy_reader_rejects_corruption_and_generation_drift(
    isolated_memory,
):
    _migrated_v8_database_with_legacy_v1_projection()
    index_path = indexer.get_index_path()
    original = index_path.read_bytes()
    index_path.write_bytes(b"{")

    with pytest.raises((ValueError, json.JSONDecodeError)):
        tool_projection.rebuild_index_projection(dry_run=True)

    index_path.write_bytes(original)
    claim_graph_path = indexer.get_claim_graph_path()
    original_claim_graph = claim_graph_path.read_bytes()
    claim_graph = json.loads(original_claim_graph)
    claim_graph[indexer.PROJECTION_MANIFEST_KEY]["generation"] = "drifted"
    claim_graph_path.write_text(json.dumps(claim_graph), encoding="utf-8")
    with pytest.raises(
        indexer.ProjectionPairContractError,
        match="generations do not match",
    ):
        tool_projection.rebuild_index_projection(dry_run=True)

    claim_graph_path.write_bytes(original_claim_graph)
    sidecar_path = indexer.get_projection_manifest_path()
    original_sidecar = sidecar_path.read_bytes()
    sidecar = json.loads(original_sidecar)
    sidecar["artifacts"][index_path.name]["sha256"] = "0" * 64
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="legacy_projection_sidecar_artifact_mismatch:index.json",
    ):
        tool_projection.rebuild_index_projection(dry_run=True)

    sidecar_path.write_bytes(original_sidecar)
    governance_store.upsert_entity(
        "entity_projection_generation_drift",
        {
            "entity_id": "entity_projection_generation_drift",
            "page_key": "Concept_Projection-Generation-Drift",
            "canonical_name": "Projection Generation Drift",
            "type": "concept",
        },
    )
    with pytest.raises(
        RuntimeError,
        match="legacy_projection_canonical_generation_mismatch",
    ):
        tool_projection.rebuild_index_projection(dry_run=True)

    with pytest.raises(
        Exception,
        match="legacy_index_requires_explicit_migration_reader",
    ):
        tool_projection.projection_diff_report()


def test_v2_full_roots_materialize_with_legacy_shape(tmp_path):
    from vector_lake.projection_format_v2 import (
        build_projection_roots,
        materialize_claim_graph,
        materialize_index,
    )

    prepared = build_projection_roots(
        tmp_path,
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation={
            "entities": 1,
            "claims": 2,
            "sources": 3,
            "page_graph_edges": 4,
            "claim_graph_edges": 5,
        },
    )
    observed_index = materialize_index(tmp_path, prepared.sidecar)
    observed_graph = materialize_claim_graph(tmp_path, prepared.sidecar)

    for field in (
        "nodes",
        "aliases",
        "categories",
        "weighted_edges",
        "error_log",
        "communities",
        "community_labels",
        "graph_insights",
        "graph_state",
        "governance_metrics",
    ):
        assert observed_index[field] == _sample_index()[field]
    assert observed_graph["nodes"] == _sample_claim_graph()["nodes"]
    assert observed_graph["edges"] == _sample_claim_graph()["edges"]
    assert len(prepared.sidecar_json.encode("utf-8")) <= 64 * 1024
    assert prepared.projection_generation == hashlib.sha256(
        json.dumps(
            {
                "canonical_generation": prepared.sidecar["canonical_generation"],
                "claim_graph_root_sha256": prepared.claim_graph_root_sha256,
                "index_root_sha256": prepared.index_root_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_locators_are_static_and_wrong_projection_fails_closed(tmp_path):
    from vector_lake.index_snapshot import (
        load_index_snapshot,
        load_legacy_index_snapshot_for_migration,
    )
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        ensure_static_locators,
        locator_payload,
        validate_locator,
    )

    ensure_static_locators(tmp_path)
    first_index = (tmp_path / "index.json").read_bytes()
    first_graph = (tmp_path / "claim_graph.json").read_bytes()
    ensure_static_locators(tmp_path)
    assert (tmp_path / "index.json").read_bytes() == first_index
    assert (tmp_path / "claim_graph.json").read_bytes() == first_graph
    assert json.loads(first_index) == locator_payload("index")
    with pytest.raises(ProjectionV2ContractError, match="locator_projection"):
        validate_locator(tmp_path / "index.json", "claim_graph")

    legacy_path = tmp_path / "legacy" / "index.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text('{"nodes":{}}', encoding="utf-8")
    with pytest.raises(
        ProjectionV2ContractError,
        match="legacy_index_requires_explicit_migration_reader",
    ):
        load_index_snapshot(legacy_path)
    assert load_legacy_index_snapshot_for_migration(legacy_path)["nodes"] == {}


def test_frontier_cap_has_stable_heavy_rebuild_error():
    from vector_lake.projection_format_v2 import (
        ProjectionHeavyRebuildRequired,
        require_bounded_frontier,
    )

    assert require_bounded_frontier(range(512)) == tuple(range(512))
    with pytest.raises(
        ProjectionHeavyRebuildRequired,
        match="^projection_heavy_rebuild_required$",
    ):
        require_bounded_frontier(range(513))


def test_claim_graph_node_and_edge_caps_are_independent(tmp_path, monkeypatch):
    from vector_lake import projection_format_v2

    monkeypatch.setattr(projection_format_v2, "MAX_CLAIM_GRAPH_NODES", 2)
    monkeypatch.setattr(projection_format_v2, "MAX_CLAIM_GRAPH_EDGES", 3)
    nodes = [{"id": f"claim-{index}"} for index in range(2)]
    edges = [
        {"source": "claim-0", "target": "claim-1", "ordinal": index}
        for index in range(3)
    ]

    root, _stats = projection_format_v2.build_claim_graph_root(
        tmp_path / "accepted",
        {"nodes": nodes, "edges": edges},
    )
    assert root

    with pytest.raises(
        projection_format_v2.ProjectionHeavyRebuildRequired,
        match="^projection_heavy_rebuild_required$",
    ):
        projection_format_v2.build_claim_graph_root(
            tmp_path / "too-many-nodes",
            {"nodes": nodes + [{"id": "claim-2"}], "edges": []},
        )

    with pytest.raises(
        projection_format_v2.ProjectionHeavyRebuildRequired,
        match="^projection_heavy_rebuild_required$",
    ):
        projection_format_v2.build_claim_graph_root(
            tmp_path / "too-many-edges",
            {"nodes": nodes, "edges": edges + [dict(edges[0], ordinal=3)]},
        )


def test_root_descriptor_rejects_missing_and_surplus_fields(tmp_path):
    from vector_lake import projection_format_v2
    from vector_lake.projection_store_v2 import ProjectionStoreV2

    prepared = projection_format_v2.build_projection_roots(
        tmp_path,
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation={
            "entities": 0,
            "claims": 0,
            "sources": 0,
            "page_graph_edges": 0,
            "claim_graph_edges": 0,
        },
    )
    store = ProjectionStoreV2(tmp_path)
    surplus = store.apply(
        prepared.index_root_sha256,
        sets={
            "uncontracted": store.empty_root_digest,
            "uncontracted-2": store.empty_root_digest,
        },
    )
    missing_index = store.apply(
        prepared.index_root_sha256,
        deletes={"nodes"},
    )
    missing = store.apply(
        prepared.claim_graph_root_sha256,
        deletes={"meta"},
    )

    with pytest.raises(
        projection_format_v2.ProjectionV2ContractError,
        match="^index_root_fields$",
    ):
        projection_format_v2._root_descriptor(store, surplus.root_digest, "index")
    with pytest.raises(
        projection_format_v2.ProjectionV2ContractError,
        match="^claim_graph_root_fields$",
    ):
        projection_format_v2._root_descriptor(
            store,
            missing.root_digest,
            "claim_graph",
        )
    with pytest.raises(
        projection_format_v2.ProjectionV2ContractError,
        match="^index_root_fields$",
    ):
        projection_format_v2._root_descriptor(
            store,
            missing_index.root_digest,
            "index",
        )
    with pytest.raises(
        projection_format_v2.ProjectionV2ContractError,
        match="^projection_root_kind$",
    ):
        projection_format_v2._root_descriptor(
            store,
            prepared.index_root_sha256,
            "unknown",
        )


def test_exact_legacy_index_descriptor_remains_closed_and_materializable(tmp_path):
    from vector_lake import projection_format_v2
    from vector_lake.projection_store_v2 import ProjectionStoreV2

    sample_index = _sample_index()
    prepared = projection_format_v2.build_projection_roots(
        tmp_path,
        sample_index,
        _sample_claim_graph(),
        canonical_generation={
            "entities": 0,
            "claims": 0,
            "sources": 0,
            "page_graph_edges": 0,
            "claim_graph_edges": 0,
        },
    )
    store = ProjectionStoreV2(tmp_path)
    legacy = store.apply(
        prepared.index_root_sha256,
        deletes={"errors_by_file"},
    )
    legacy_prepared = projection_format_v2.prepare_projection_from_roots(
        tmp_path,
        index_root_sha256=legacy.root_digest,
        claim_graph_root_sha256=prepared.claim_graph_root_sha256,
        canonical_generation=prepared.canonical_generation,
    )

    closure = projection_format_v2.validate_root_closure(
        tmp_path,
        legacy_prepared.sidecar,
    )
    materialized = projection_format_v2.materialize_index(
        tmp_path,
        legacy_prepared.sidecar,
    )
    assert closure
    assert materialized["error_log"] == sample_index["error_log"]

    legacy_with_extra = store.apply(
        legacy.root_digest,
        sets={"uncontracted": store.empty_root_digest},
    )
    with pytest.raises(
        projection_format_v2.ProjectionV2ContractError,
        match="^index_root_fields$",
    ):
        projection_format_v2._root_descriptor(
            store,
            legacy_with_extra.root_digest,
            "index",
        )


def _entity(page_key: str, *, title: str, aliases=(), links=(), sources=()):
    return {
        "entity_id": "entity_" + page_key.casefold(),
        "id": "urn:" + page_key.casefold(),
        "page_key": page_key,
        "canonical_name": title,
        "title": title,
        "type": "concept",
        "status": "Active",
        "domain": "General",
        "categories": ["Concept"],
        "aliases": list(aliases),
        "links": list(links),
        "sources": list(sources),
        "summary": title + " summary",
        "raw_text": title + " body",
        "updated": "2026-08-28T00:00:00+00:00",
    }


def test_indexer_v2_incremental_update_alias_edge_and_delete_without_full_loader(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    first = _entity(
        "Concept_A",
        title="Alpha",
        aliases=["A-old"],
        links=["Concept_B"],
        sources=["shared"],
    )
    second = _entity(
        "Concept_B",
        title="Beta",
        sources=["shared"],
    )
    governance_store.upsert_entity(first["entity_id"], first)
    governance_store.upsert_entity(second["entity_id"], second)
    indexer.generate_index()

    updated = dict(first)
    updated.update({"title": "Alpha 2", "aliases": ["A-new"]})
    governance_store.upsert_entity(updated["entity_id"], updated)
    original_loader = indexer.load_committed_index
    monkeypatch.setattr(
        indexer,
        "load_committed_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full index materialization invoked")
        ),
    )
    indexer.update_index_item("Concept_A.md")

    monkeypatch.setattr(indexer, "load_committed_index", original_loader)
    observed = indexer.read_committed_index_snapshot()
    assert observed["nodes"]["Concept_A"]["title"] == "Alpha 2"
    assert observed["aliases"]["A-new"] == "Concept_A"
    assert "A-old" not in observed["aliases"]
    assert any(
        {edge["source"], edge["target"]} == {"Concept_A", "Concept_B"}
        for edge in observed["weighted_edges"]
    )

    governance_store.delete_entity(updated["entity_id"])
    indexer.update_index_item("Concept_A.md")
    observed = indexer.read_committed_index_snapshot()
    assert "Concept_A" not in observed["nodes"]
    assert "A-new" not in observed["aliases"]
    assert all(
        "Concept_A" not in {edge["source"], edge["target"]}
        for edge in observed["weighted_edges"]
    )


def test_same_roots_and_generation_publish_is_byte_and_mtime_idempotent(
    isolated_memory,
):
    from vector_lake.projection_format_v2 import (
        build_projection_roots,
        publish_prepared_projection,
    )

    db_store.init_db()
    generations = indexer.canonical_runtime_generation_snapshot()
    first = build_projection_roots(
        isolated_memory / "wiki",
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation=generations,
        published_at_utc="2026-08-28T00:00:00+00:00",
    )
    publish_prepared_projection(isolated_memory / "wiki", first)
    marker = isolated_memory / "wiki" / "projection_pair_manifest.json"
    before = (marker.read_bytes(), marker.stat().st_mtime_ns)
    runtime_before = db_store.get_projection_runtime_v9()

    second = build_projection_roots(
        isolated_memory / "wiki",
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation=generations,
        published_at_utc="2026-08-28T01:00:00+00:00",
    )
    assert second.object_new_count == 0
    publish_prepared_projection(isolated_memory / "wiki", second)
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before
    assert db_store.get_projection_runtime_v9() == runtime_before


def test_second_full_generate_is_noop_for_current_generation(isolated_memory):
    db_store.init_db()
    entity = _entity("Concept_A", title="Alpha")
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    marker = isolated_memory / "wiki" / "projection_pair_manifest.json"
    before = (marker.read_bytes(), marker.stat().st_mtime_ns)
    runtime_before = db_store.get_projection_runtime_v9()
    object_count = len(
        list(
            (isolated_memory / "wiki" / ".projection-store" / "objects").rglob(
                "*.json"
            )
        )
    )

    indexer.generate_index()

    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before
    assert db_store.get_projection_runtime_v9() == runtime_before
    assert len(
        list(
            (isolated_memory / "wiki" / ".projection-store" / "objects").rglob(
                "*.json"
            )
        )
    ) == object_count


def test_materializer_supports_more_than_live_126884_nodes(isolated_memory):
    from vector_lake.index_snapshot import (
        clear_index_snapshot_cache_for_tests,
        load_index_snapshot,
    )
    from vector_lake.projection_format_v2 import (
        publish_prepared_projection,
        prepare_projection_from_roots,
    )
    from vector_lake.projection_store_v2 import ProjectionStoreV2

    db_store.init_db()
    base = isolated_memory / "wiki"
    store = ProjectionStoreV2(base)
    node_count = 126_885
    build_started = time.perf_counter()
    nodes = store.apply(
        None,
        sets={f"Concept_{index:06d}": index for index in range(node_count)},
    ).root_digest
    empty = store.empty_root_digest
    descriptor = store.apply(
        None,
        sets={
            "contract": "vector-lake-projection-root-v2",
            "format_version": 2,
            "projection": "index",
            "nodes": nodes,
            "aliases": empty,
            "aliases_by_node": empty,
            "edges": empty,
            "categories": empty,
            "error_log": empty,
            "search_rows": empty,
            "reverse_links": empty,
            "reverse_sources": empty,
            "category_counts": empty,
            "edge_candidates": empty,
            "edge_incidence": empty,
            "edge_candidate_incidence": empty,
            "meta": empty,
        },
    ).root_digest
    claim = store.apply(
        None,
        sets={
            "contract": "vector-lake-projection-root-v2",
            "format_version": 2,
            "projection": "claim_graph",
            "nodes": empty,
            "edges": empty,
            "meta": empty,
        },
    ).root_digest
    prepared = prepare_projection_from_roots(
        base,
        index_root_sha256=descriptor,
        claim_graph_root_sha256=claim,
        canonical_generation={
            "entities": 0,
            "claims": 0,
            "sources": 0,
            "page_graph_edges": 0,
            "claim_graph_edges": 0,
        },
        counts={"index_nodes": node_count},
    )
    publish_prepared_projection(base, prepared)
    full_build_seconds = time.perf_counter() - build_started
    object_count = len(list(store.objects_dir.rglob("*.json")))

    clear_index_snapshot_cache_for_tests()
    process = psutil.Process()
    rss_before = process.memory_info().rss
    cold_started = time.perf_counter()
    observed = load_index_snapshot(base / "index.json")
    cold_seconds = time.perf_counter() - cold_started
    rss_after = process.memory_info().rss
    warm_started = time.perf_counter()
    warm = load_index_snapshot(base / "index.json")
    warm_seconds = time.perf_counter() - warm_started
    metrics = {
        "nodes": node_count,
        "objects": object_count,
        "full_build_seconds": full_build_seconds,
        "cold_committed_read_seconds": cold_seconds,
        "warm_cache_hit_seconds": warm_seconds,
        "cold_rss_delta_bytes": max(0, rss_after - rss_before),
        "sidecar_bytes": len(prepared.sidecar_json.encode("utf-8")),
    }
    print("projection_v2_cold_read_benchmark=" + json.dumps(metrics))
    assert len(observed["nodes"]) == node_count
    assert observed["nodes"]["Concept_126884"] == 126_884
    assert warm is observed
    assert cold_seconds <= 2.5
    assert warm_seconds <= 0.05


def test_single_item_write_amplification_is_flat_through_126885_nodes(
    isolated_memory,
):
    from vector_lake.projection_store_v2 import ProjectionStoreV2

    def prebuild(label: str, count: int) -> dict[str, object]:
        store = ProjectionStoreV2(isolated_memory / label)
        padding = "p" * 160
        initial = store.apply(
            None,
            sets={
                f"key-{index:06d}": {"payload": padding, "revision": 0}
                for index in range(count)
            },
        )
        return {
            "store": store,
            "root": initial.root_digest,
            "count": count,
            "padding": padding,
            "initial_bytes": float(initial.new_bytes),
            "byte_samples": [],
            "latency_samples": [],
        }

    labels = ("n10k", "n100k", "n126885")
    states = {
        "n10k": prebuild("n10k", 10_000),
        "n100k": prebuild("n100k", 100_000),
        "n126885": prebuild("n126885", 126_885),
    }
    # Interleave sizes and rotate their order so Windows flush/antivirus jitter
    # cannot systematically privilege the first (10k) baseline.  The gate is
    # still the p95 of fully durable mutations, not a best-effort shortcut.
    for revision in range(1, 41):
        offset = revision % len(labels)
        for label in labels[offset:] + labels[:offset]:
            state = states[label]
            store = state["store"]
            count = int(state["count"])
            key = f"key-{((revision * 7919) % count):06d}"
            started = time.perf_counter()
            changed = store.apply(
                state["root"],
                sets={
                    key: {
                        "payload": state["padding"],
                        "revision": revision,
                    }
                },
            )
            state["latency_samples"].append(time.perf_counter() - started)
            state["byte_samples"].append(changed.new_bytes)
            state["root"] = changed.root_digest

    def summarize(state: dict[str, object]) -> dict[str, float]:
        byte_samples = state["byte_samples"]
        latency_samples = state["latency_samples"]
        return {
            "initial_bytes": float(state["initial_bytes"]),
            "p95_new_bytes": float(
                statistics.quantiles(byte_samples, n=20, method="inclusive")[18]
            ),
            "p95_latency_seconds": statistics.quantiles(
                latency_samples,
                n=20,
                method="inclusive",
            )[18],
        }

    observed = {label: summarize(states[label]) for label in labels}
    print("projection_v2_incremental_benchmark=" + json.dumps(observed))
    baseline = observed["n10k"]
    for label in ("n100k", "n126885"):
        assert observed[label]["p95_new_bytes"] / baseline["p95_new_bytes"] <= 1.10
        assert (
            observed[label]["p95_latency_seconds"]
            / baseline["p95_latency_seconds"]
            <= 1.25
        )


def test_publish_crash_boundaries_recover_exact_pending_sidecar(isolated_memory):
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        build_projection_roots,
        load_committed_index,
        publish_prepared_projection,
        recover_pending_publish,
    )

    db_store.init_db()
    base = isolated_memory / "wiki"
    generations = indexer.canonical_runtime_generation_snapshot()
    baseline = build_projection_roots(
        base,
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation=generations,
    )
    publish_prepared_projection(base, baseline)
    assert load_committed_index(base)["nodes"]["Concept_A"]["title"] == "A"

    changed_index = _sample_index()
    changed_index["nodes"]["Concept_A"]["title"] = "A2"
    candidate = build_projection_roots(
        base,
        changed_index,
        _sample_claim_graph(),
        canonical_generation=generations,
    )
    # Crash after immutable objects: the old sidecar remains committed.
    assert load_committed_index(base)["nodes"]["Concept_A"]["title"] == "A"

    state = db_store.get_projection_runtime_v9()
    with db_store.transaction() as connection:
        db_store.cas_projection_runtime_publish_pending(
            connection,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=candidate.projection_generation,
            canonical_generation=candidate.canonical_generation,
            sidecar_json=candidate.sidecar_json,
        )
    # Crash after DB pending: readers fail closed instead of accepting old FTS
    # with new roots; recovery publishes only the exact persisted intent.
    with pytest.raises(ProjectionV2ContractError, match="not_ready"):
        load_committed_index(base)
    assert recover_pending_publish(base) is True
    assert load_committed_index(base)["nodes"]["Concept_A"]["title"] == "A2"
    assert db_store.get_projection_runtime_v9()["status"] == "ready"

    changed_again = _sample_index()
    changed_again["nodes"]["Concept_A"]["title"] = "A3"
    after_sidecar = build_projection_roots(
        base,
        changed_again,
        _sample_claim_graph(),
        canonical_generation=generations,
    )
    state = db_store.get_projection_runtime_v9()
    with db_store.transaction() as connection:
        db_store.cas_projection_runtime_publish_pending(
            connection,
            expected_status=state["status"],
            expected_projection_generation=state["projection_generation"],
            projection_generation=after_sidecar.projection_generation,
            canonical_generation=after_sidecar.canonical_generation,
            sidecar_json=after_sidecar.sidecar_json,
        )
    # Crash after marker replacement but before ready CAS: recovery recognizes
    # the exact persisted marker and performs only the final ready transition.
    marker = base / "projection_pair_manifest.json"
    marker.write_text(after_sidecar.sidecar_json, encoding="utf-8")
    assert recover_pending_publish(base) is True
    assert load_committed_index(base)["nodes"]["Concept_A"]["title"] == "A3"

    marker.write_bytes(marker.read_bytes() + b" ")
    with pytest.raises(ProjectionV2ContractError, match="sidecar"):
        load_committed_index(base)


def test_reader_detects_sidecar_race_after_materialization(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import projection_format_v2

    db_store.init_db()
    base = isolated_memory / "wiki"
    prepared = projection_format_v2.build_projection_roots(
        base,
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation=indexer.canonical_runtime_generation_snapshot(),
    )
    projection_format_v2.publish_prepared_projection(base, prepared)
    original = projection_format_v2.materialize_index

    def racing_materializer(*args, **kwargs):
        value = original(*args, **kwargs)
        marker = base / "projection_pair_manifest.json"
        payload = marker.read_bytes()
        marker.write_bytes(payload)
        return value

    monkeypatch.setattr(projection_format_v2, "materialize_index", racing_materializer)
    with pytest.raises(
        projection_format_v2.ProjectionV2ContractError,
        match="changed_during_materialization",
    ):
        projection_format_v2.load_committed_index(base)


def test_full_builder_persists_true_candidate_frontier_before_global_prune(
    isolated_memory,
):
    from vector_lake.projection_format_v2 import (
        build_projection_roots,
        load_component_roots,
    )
    from vector_lake.projection_store_v2 import ProjectionStoreV2

    db_store.init_db()
    nodes = {
        f"Concept_{index:02d}": {
            "title": f"Node {index}",
            "type": "concept",
            "sources": ["shared-source"],
            "links": [],
            "categories": [],
            "aliases": [],
            "decay_weight": 1.0,
            "alignment_score": 100.0,
        }
        for index in range(24)
    }
    index_data = _sample_index()
    index_data.update({"nodes": nodes, "aliases": {}, "categories": []})
    index_data["weighted_edges"] = indexer._calculate_weighted_edges(index_data)
    assert len(index_data["_projection_edge_candidates"]) > len(
        index_data["weighted_edges"]
    )
    prepared = build_projection_roots(
        isolated_memory / "wiki",
        index_data,
        _sample_claim_graph(),
        canonical_generation=indexer.canonical_runtime_generation_snapshot(),
    )
    descriptor = load_component_roots(
        isolated_memory / "wiki",
        prepared.sidecar,
    )
    store = ProjectionStoreV2(isolated_memory / "wiki")
    candidates = store.iter_items(
        descriptor["edge_candidates"],
        limit=10_000,
    )
    retained = store.iter_items(descriptor["edges"], limit=10_000)
    assert len(candidates) > len(retained)


def test_schema_rollback_delegate_restores_dynamic_closure_sidecar_last(
    isolated_memory,
):
    from vector_lake import projection_format_v2

    db_store.init_db()
    base = isolated_memory / "wiki"
    generations = indexer.canonical_runtime_generation_snapshot()
    original = projection_format_v2.build_projection_roots(
        base,
        _sample_index(),
        _sample_claim_graph(),
        canonical_generation=generations,
    )
    projection_format_v2.publish_prepared_projection(base, original)
    snapshot = projection_format_v2.schema_migration_projection_snapshot()
    backup = projection_format_v2.schema_migration_projection_backup(
        snapshot,
        final_directory=isolated_memory / "rollback-projection",
    )

    changed = _sample_index()
    changed["nodes"]["Concept_A"]["title"] = "changed"
    replacement = projection_format_v2.build_projection_roots(
        base,
        changed,
        _sample_claim_graph(),
        canonical_generation=generations,
    )
    projection_format_v2.publish_prepared_projection(base, replacement)
    assert (
        projection_format_v2.schema_migration_projection_content_binding(
            projection_format_v2.schema_migration_projection_snapshot()
        )
        != projection_format_v2.schema_migration_projection_content_binding(
            snapshot
        )
    )

    plan = {
        "fingerprint": "sha256:" + "a" * 64,
        "restore": {
            "pre_projection": snapshot,
            "projection_backup": backup,
        },
    }
    staged = projection_format_v2.schema_rollback_stage_projection(plan)
    projection_format_v2.schema_rollback_publish_projection(plan, staged)
    restored = projection_format_v2.schema_migration_projection_snapshot()
    assert projection_format_v2.schema_migration_projection_content_binding(
        restored
    ) == projection_format_v2.schema_migration_projection_content_binding(snapshot)
