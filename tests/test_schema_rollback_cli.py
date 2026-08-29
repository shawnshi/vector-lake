import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vector_lake import cli_app, db_store, governance_store, indexer


_ACTIVE_11_19_1 = Path(
    "C:/Users/shich/.codex/plugins/cache/vector-lake-budget-20260827t2147/"
    "vector-lake/11.19.1+codex.20260827212800"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _projection_hashes() -> dict[str, str]:
    return {
        path.name: _sha256(path)
        for path in (
            indexer.get_index_path(),
            indexer.get_claim_graph_path(),
            indexer.get_projection_manifest_path(),
        )
    }


def _generate_legacy_v1_projection() -> None:
    """Exercise the retained v1 serializer for a physical schema-v8 fixture."""
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


def _downgrade_v9_database_to_v8(path: Path) -> None:
    db_store.close_all_connections()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE projection_runtime_v9")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("PRAGMA user_version = 8")
        connection.commit()
    finally:
        connection.close()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))


def _v8_database_with_projection() -> tuple[Path, str, dict[str, str]]:
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_schema_rollback",
        {
            "entity_id": "entity_schema_rollback",
            "page_key": "Concept_Schema-Rollback",
            "canonical_name": "Schema Rollback",
            "type": "concept",
            "summary": "State captured before schema v9.",
            "raw_text": "Exact v8 projection body.",
        },
    )
    _generate_legacy_v1_projection()
    path = db_store.get_db_path().resolve()
    db_store.close_all_connections()
    _downgrade_v9_database_to_v8(path)
    return path, _sha256(path), _projection_hashes()


def _v8_database_with_v2_projection() -> tuple[Path, dict]:
    db_store.init_db()
    governance_store.upsert_entity(
        "entity_schema_rollback_v2",
        {
            "entity_id": "entity_schema_rollback_v2",
            "page_key": "Concept_Schema-Rollback-V2",
            "canonical_name": "Schema Rollback V2",
            "type": "concept",
            "raw_text": "Exact schema-v8 projection-v2 closure.",
        },
    )
    indexer.generate_index()
    path = db_store.get_db_path().resolve()
    snapshot = db_store._schema_migration_projection_snapshot()
    assert snapshot["format_version"] == 2
    assert snapshot["status"] == "captured"
    db_store.close_all_connections()
    _downgrade_v9_database_to_v8(path)
    return path, snapshot


def _migrate_v8_to_v9(path: Path) -> dict:
    preview = db_store.preview_schema_migration(path)
    assert preview["pre_schema_version"] == 8
    assert preview["pre_projection"]["status"] == "captured"
    return db_store.schema_migration_maintenance(
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )


class _ScriptedReplaceFileW:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(
        self,
        replaced_path,
        replacement_path,
        backup_path,
        _flags,
        _exclude,
        _reserved,
    ):
        if not self.outcomes:
            raise AssertionError("ReplaceFileW exceeded the scripted attempt bound")
        replaced = Path(replaced_path)
        replacement = Path(replacement_path)
        backup = Path(backup_path)
        outcome = self.outcomes.pop(0)
        self.calls.append(outcome if isinstance(outcome, int) else outcome[0])
        if isinstance(outcome, tuple):
            error_code, partial_mutation = outcome
            partial_mutation(replaced, replacement, backup)
        else:
            error_code = outcome
        if error_code:
            ctypes.set_last_error(error_code)
            return 0
        backup.write_bytes(replaced.read_bytes())
        os.replace(replacement, replaced)
        return 1


def _install_scripted_replace(monkeypatch, outcomes):
    scripted = _ScriptedReplaceFileW(outcomes)

    class _Kernel32:
        ReplaceFileW = scripted

    monkeypatch.setattr(
        "vector_lake.wiki_utils.ctypes.WinDLL",
        lambda *_args, **_kwargs: _Kernel32(),
    )
    return scripted


def _isolated_database_replace_paths(isolated_memory, monkeypatch):
    memory_dir = isolated_memory.resolve()
    meta_dir = (memory_dir / "wiki" / ".meta").resolve()
    database_path = (meta_dir / "replace-retry" / "vector_lake.db").resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(database_path))
    assert Path(os.environ["VECTOR_LAKE_MEMORY_DIR"]).resolve() == memory_dir
    assert Path(os.environ["VECTOR_LAKE_META_DIR"]).resolve() == meta_dir
    assert db_store.peek_db_path().resolve() == database_path
    return database_path, database_path.with_name("vector_lake.v8.stage.db")


def test_schema_rollback_preview_requires_authoritative_absolute_receipt(
    isolated_memory,
):
    path = db_store.peek_db_path().resolve()
    before = list(isolated_memory.rglob("*"))

    preview = db_store.preview_schema_rollback("relative.json", path)

    assert preview["dry_run"] is True
    assert preview["can_apply"] is False
    assert "migration_receipt_path_must_be_absolute" in preview["issues"]
    assert list(isolated_memory.rglob("*")) == before
    assert not path.exists()


def test_schema_rollback_round_trip_restores_exact_v8_database_and_projection(
    isolated_memory,
):
    path, _v8_sha256, v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    migration_payload = json.loads(migration_receipt.read_text(encoding="utf-8"))
    assert migration_payload["plan"]["pre_projection"]["status"] == "captured"
    assert migration_payload["projection_backup"]["status"] == "captured"

    governance_store.upsert_entity(
        "entity_after_v9",
        {
            "entity_id": "entity_after_v9",
            "page_key": "Concept_After-V9",
            "canonical_name": "After V9",
            "type": "concept",
            "raw_text": "This post-migration write is intentionally rewound.",
        },
    )
    indexer.generate_index()
    db_store.close_all_connections()

    preview = db_store.preview_schema_rollback(migration_receipt, path)
    assert preview["can_apply"] is True
    assert preview["data_loss_since_migration"] is True
    assert "entities" in preview["changed_runtime_generations"]
    assert preview["projection_action"] == "restore_pre_migration_pair"
    with pytest.raises(RuntimeError, match="--confirm-data-rewind"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        confirm_data_rewind=True,
        db_path=path,
    )

    assert result["applied"] is True
    assert _sha256(path) == migration_payload["backup"]["sha256"]
    assert _projection_hashes() == v8_projection
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&immutable=1", uri=True
    )
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute(
            "SELECT 1 FROM entities WHERE entity_id='entity_after_v9'"
        ).fetchone() is None
    finally:
        connection.close()
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["contract"] == db_store._SCHEMA_ROLLBACK_RECEIPT_CONTRACT
    assert receipt["status"] == "completed"
    assert receipt["post"]["old_runtime_acceptance"] == {
        "projection_rebuild_required": False,
        "projection_restored": True,
        "runtime_switch_required": True,
        "schema_version": 8,
    }
    forward_db = Path(receipt["forward_recovery"]["database"]["path"])
    assert forward_db.is_file()
    assert receipt["forward_recovery"]["database"]["sha256"] == _sha256(
        forward_db
    )
    forward_projection = receipt["forward_recovery"]["projection"]
    assert forward_projection["contract"] == "vector-lake-projection-backup/v2"
    assert forward_projection["format_version"] == 2
    relative_paths = {
        item["relative_path"] for item in forward_projection["artifacts"]
    }
    assert {
        "index.json",
        "claim_graph.json",
        "projection_pair_manifest.json",
    }.issubset(relative_paths)
    assert any(
        relative.startswith(".projection-store/objects/sha256/")
        for relative in relative_paths
    )
    db_store._schema_migration_validate_projection_backup(
        receipt["from_source"]["projection"],
        forward_projection,
        backup_root=path.parent / "schema-migration-backups",
    )
    remigration = db_store.preview_schema_migration(path)
    assert remigration["can_apply"] is True
    assert remigration["pre_schema_version"] == 8
    assert remigration["steps"] == ["schema_v8_to_v9"]
    assert remigration["issues"] == []


def test_schema_rollback_v2_target_uses_transitive_closure_delegate(
    isolated_memory,
):
    path, v8_projection = _v8_database_with_v2_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    migration_payload = json.loads(
        migration_receipt.read_text(encoding="utf-8")
    )
    assert migration_payload["plan"]["pre_projection"]["format_version"] == 2
    assert migration_payload["projection_backup"]["format_version"] == 2

    governance_store.upsert_entity(
        "entity_schema_rollback_v2_after",
        {
            "entity_id": "entity_schema_rollback_v2_after",
            "page_key": "Concept_Schema-Rollback-V2-After",
            "canonical_name": "Schema Rollback V2 After",
            "type": "concept",
            "raw_text": "Post-migration v2 state must be recoverable.",
        },
    )
    indexer.generate_index()
    db_store.close_all_connections()
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    assert preview["can_apply"] is True, preview["issues"]

    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        confirm_data_rewind=True,
        db_path=path,
    )

    assert result["applied"] is True
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()
    restored = db_store._schema_migration_projection_snapshot()
    assert db_store._schema_migration_projection_content_binding(restored) == (
        db_store._schema_migration_projection_content_binding(v8_projection)
    )
    assert result["forward_recovery"]["projection"]["format_version"] == 2


def test_schema_rollback_rejects_tampered_projection_backup_before_forward_write(
    isolated_memory,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    receipt_path = Path(migration["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    projection_path = Path(receipt["projection_backup"]["artifacts"][0]["path"])
    projection_path.write_bytes(projection_path.read_bytes() + b"tamper")
    backup_root = path.parent / "schema-migration-backups"
    before = sorted(str(item) for item in backup_root.rglob("*"))

    preview = db_store.preview_schema_rollback(receipt_path, path)

    assert preview["can_apply"] is False
    assert "projection_backup_artifact_mismatch" in preview["issues"]
    assert sorted(str(item) for item in backup_root.rglob("*")) == before
    assert not any("forward-v9" in item.name for item in backup_root.rglob("*"))


def test_schema_rollback_recovers_completed_receipt_without_second_backup(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    real_atomic_json = db_store._schema_migration_atomic_json

    def fail_completed(path_value, payload):
        if (
            payload.get("contract") == db_store._SCHEMA_ROLLBACK_RECEIPT_CONTRACT
            and payload.get("status") == "completed"
        ):
            raise OSError("injected rollback completion publication failure")
        return real_atomic_json(path_value, payload)

    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", fail_completed)
    with pytest.raises(RuntimeError, match="receipt publication failed"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()
    forward_before = sorted(
        str(item)
        for item in (path.parent / "schema-migration-backups").glob(
            "*forward-v9*"
        )
    )
    recovery = db_store.preview_schema_rollback(migration_receipt, path)
    assert recovery["can_apply"] is True
    assert recovery["no_op"] is True
    assert recovery["recovery_action"] == "publish_completed_receipt"

    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", real_atomic_json)
    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=recovery["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert result["applied"] is False
    assert result["no_op"] is True
    assert result["recovery_action"] == "published_completed_receipt"
    assert Path(result["receipt_path"]).is_file()
    assert sorted(
        str(item)
        for item in (path.parent / "schema-migration-backups").glob(
            "*forward-v9*"
        )
    ) == forward_before


def test_schema_rollback_fingerprint_and_forward_backup_failure_do_not_mutate_live(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    indexer.generate_index()
    db_store.close_all_connections()
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    assert preview["current_source"]["projection"]["format_version"] == 2
    before_database = _sha256(path)
    before_projection = _projection_hashes()

    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation="sha256:" + "0" * 64,
            confirm_no_writers=True,
            db_path=path,
        )
    assert _sha256(path) == before_database
    assert _projection_hashes() == before_projection

    monkeypatch.setattr(
        db_store,
        "_schema_migration_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected forward backup failure")
        ),
    )
    with pytest.raises(OSError, match="forward backup failure"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert _sha256(path) == before_database
    assert _projection_hashes() == before_projection
    assert not any(
        "forward-v9" in item.name
        for item in (path.parent / "schema-migration-backups").rglob("*")
    )
    assert not any(
        ".rollback-v9-to-v8." in item.name
        and item.name.endswith(".pending.json")
        for item in (path.parent / "schema-migration-receipts").glob("*")
    )


def test_schema_rollback_adopts_verified_forward_bundle_after_pending_failure(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    indexer.generate_index()
    db_store.close_all_connections()
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    assert preview["current_source"]["projection"]["format_version"] == 2
    before_database = _sha256(path)
    before_projection = _projection_hashes()
    real_atomic_json = db_store._schema_migration_atomic_json

    def fail_pending(path_value, payload):
        if (
            payload.get("contract") == db_store._SCHEMA_ROLLBACK_RECEIPT_CONTRACT
            and payload.get("status") == "pending"
        ):
            raise OSError("injected rollback pending publication failure")
        return real_atomic_json(path_value, payload)

    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", fail_pending)
    with pytest.raises(RuntimeError, match="pending receipt publication failed"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert _sha256(path) == before_database
    assert _projection_hashes() == before_projection
    assert not Path(preview["forward_recovery"]["pending_receipt_path"]).exists()

    orphan = db_store.preview_schema_rollback(migration_receipt, path)
    assert orphan["can_apply"] is True, orphan["issues"]
    assert orphan["recovery_action"] == "reuse_verified_forward_recovery"
    assert orphan["forward_recovery"]["reuse_existing"] is True
    forward_db = Path(orphan["forward_recovery"]["database_path"])
    forward_projection = Path(
        orphan["forward_recovery"]["projection_directory"]
    )
    assert forward_db.is_file()
    original_forward_database = forward_db.read_bytes()
    tamper_connection = sqlite3.connect(forward_db)
    try:
        db_store._load_sqlite_vec_extension(tamper_connection)
        tamper_connection.execute(
            "UPDATE entities SET canonical_name = canonical_name || ' tampered' "
            "WHERE entity_id = 'entity_schema_rollback'"
        )
        tamper_connection.commit()
    finally:
        tamper_connection.close()
    tampered_database = db_store.preview_schema_rollback(migration_receipt, path)
    assert tampered_database["can_apply"] is False
    assert "rollback_orphan_forward_database_invalid" in tampered_database["issues"]
    forward_db.write_bytes(original_forward_database)
    projection_artifact = next(
        item for item in forward_projection.rglob("*") if item.is_file()
    )
    original_projection = projection_artifact.read_bytes()
    projection_artifact.write_bytes(original_projection + b"tamper")
    tampered = db_store.preview_schema_rollback(migration_receipt, path)
    assert tampered["can_apply"] is False
    assert "rollback_orphan_forward_projection_invalid" in tampered["issues"]
    projection_artifact.write_bytes(original_projection)

    repaired = db_store.preview_schema_rollback(migration_receipt, path)
    assert repaired["can_apply"] is True
    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", real_atomic_json)
    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=repaired["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert result["applied"] is True
    assert result["recovery_action"] == "reuse_verified_forward_recovery"
    assert Path(result["receipt_path"]).is_file()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()


def test_schema_rollback_resumes_partial_projection_after_database_promote_failure(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    before_database = _sha256(path)
    before_projection = _projection_hashes()
    real_promote = db_store._schema_migration_promote_backup

    def fail_forward_database_promote(staging_path, final_path):
        if ".forward-v9-before-v8-rollback." in Path(final_path).name:
            raise OSError("injected forward database promotion failure")
        return real_promote(staging_path, final_path)

    monkeypatch.setattr(
        db_store,
        "_schema_migration_promote_backup",
        fail_forward_database_promote,
    )
    with pytest.raises(OSError, match="forward database promotion failure"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert _sha256(path) == before_database
    assert _projection_hashes() == before_projection
    partial = db_store.preview_schema_rollback(migration_receipt, path)
    assert partial["can_apply"] is True, partial["issues"]
    assert partial["recovery_action"] == "resume_forward_recovery_database"
    assert partial["forward_recovery"]["reuse_database"] is False
    assert partial["forward_recovery"]["reuse_projection"] is True
    assert not Path(partial["forward_recovery"]["database_path"]).exists()
    assert Path(partial["forward_recovery"]["projection_directory"]).is_dir()

    monkeypatch.setattr(
        db_store,
        "_schema_migration_promote_backup",
        real_promote,
    )
    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=partial["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert result["applied"] is True
    assert result["recovery_action"] == "resume_forward_recovery_database"
    assert Path(result["receipt_path"]).is_file()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()


def test_schema_rollback_active_writer_is_rejected_before_forward_backup(
    isolated_memory,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    writer = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(
            RuntimeError,
            match="fingerprint mismatch|active SQLite writer",
        ):
            db_store.schema_rollback_maintenance(
                migration_receipt=migration_receipt,
                apply=True,
                confirmation=preview["fingerprint"],
                confirm_no_writers=True,
                db_path=path,
            )
    finally:
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        connection.close()
    assert not any(
        "forward-v9" in item.name
        for item in (path.parent / "schema-migration-backups").rglob("*")
    )


def test_schema_rollback_database_cas_failure_preserves_live_v9_pair(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    before_database = _sha256(path)
    before_projection = _projection_hashes()

    from vector_lake import wiki_utils

    real_replace = wiki_utils._replace_prepared_file_compare_and_swap

    monkeypatch.setattr(
        "vector_lake.wiki_utils._replace_prepared_file_compare_and_swap",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected database CAS failure")
        ),
    )
    with pytest.raises(OSError, match="database CAS failure"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert _sha256(path) == before_database
    assert _projection_hashes() == before_projection
    assert any(
        "forward-v9" in item.name
        for item in (path.parent / "schema-migration-backups").rglob("*")
    )
    assert any(
        ".rollback-v9-to-v8." in item.name
        and item.name.endswith(".pending.json")
        for item in (path.parent / "schema-migration-receipts").glob("*")
    )

    monkeypatch.setattr(
        "vector_lake.wiki_utils._replace_prepared_file_compare_and_swap",
        real_replace,
    )
    recovery = db_store.preview_schema_rollback(migration_receipt, path)
    assert recovery["recovery_action"] == (
        "resume_database_and_projection_restore"
    )
    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=recovery["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )
    assert result["applied"] is True
    assert result["recovery_action"] == "resumed_and_completed_rollback"
    assert result["post"]["schema_state"]["user_version"] == 8


def test_schema_rollback_pending_recovery_hashes_before_exclusive_lock(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(migration_receipt, path)

    from vector_lake import wiki_utils

    real_replace = wiki_utils._replace_prepared_file_compare_and_swap
    monkeypatch.setattr(
        wiki_utils,
        "_replace_prepared_file_compare_and_swap",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected database CAS failure")
        ),
    )
    with pytest.raises(OSError, match="database CAS failure"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )
    monkeypatch.setattr(
        wiki_utils,
        "_replace_prepared_file_compare_and_swap",
        real_replace,
    )
    recovery = db_store.preview_schema_rollback(migration_receipt, path)
    assert recovery["recovery_action"] == (
        "resume_database_and_projection_restore"
    )

    real_connect = db_store.sqlite3.connect
    real_sha256 = db_store._schema_migration_sha256
    state = {"exclusive": False, "live_hashes": 0}

    class GuardedConnection:
        def __init__(self, delegate):
            object.__setattr__(self, "_delegate", delegate)

        def __getattr__(self, name):
            return getattr(self._delegate, name)

        def __setattr__(self, name, value):
            setattr(self._delegate, name, value)

        def execute(self, statement, *args, **kwargs):
            normalized = str(statement).strip().upper()
            result = self._delegate.execute(statement, *args, **kwargs)
            if normalized == "BEGIN EXCLUSIVE":
                state["exclusive"] = True
            elif normalized == "ROLLBACK":
                state["exclusive"] = False
            return result

        def close(self):
            state["exclusive"] = False
            return self._delegate.close()

    def guarded_connect(database, *args, **kwargs):
        delegate = real_connect(database, *args, **kwargs)
        if str(database).startswith(path.resolve().as_uri()) and "?mode=rw" in str(
            database
        ):
            return GuardedConnection(delegate)
        return delegate

    def guarded_sha256(candidate):
        if Path(candidate).resolve() == path.resolve():
            state["live_hashes"] += 1
            if state["exclusive"]:
                raise PermissionError(13, "hash denied while SQLite is exclusive")
        return real_sha256(candidate)

    monkeypatch.setattr(db_store.sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(db_store, "_schema_migration_sha256", guarded_sha256)
    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=recovery["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert result["applied"] is True
    assert result["recovery_action"] == "resumed_and_completed_rollback"
    assert result["post"]["schema_state"]["user_version"] == 8
    assert state["live_hashes"] > 0
    assert state["exclusive"] is False


def test_schema_rollback_pending_recovery_rejects_source_drift_before_lock(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(migration_receipt, path)

    from vector_lake import wiki_utils

    real_replace = wiki_utils._replace_prepared_file_compare_and_swap
    monkeypatch.setattr(
        wiki_utils,
        "_replace_prepared_file_compare_and_swap",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected database CAS failure")
        ),
    )
    with pytest.raises(OSError, match="database CAS failure"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )
    recovery = db_store.preview_schema_rollback(migration_receipt, path)
    assert recovery["recovery_action"] == (
        "resume_database_and_projection_restore"
    )

    real_sha256 = db_store._schema_migration_sha256
    real_connect = db_store.sqlite3.connect
    real_stage_database = db_store._schema_rollback_stage_database
    state = {"armed": False, "mutated": False, "cas_called": False}

    def arm_after_database_stage(*args, **kwargs):
        stage = real_stage_database(*args, **kwargs)
        state["armed"] = True
        return stage

    def mutate_after_live_hash(candidate):
        digest = real_sha256(candidate)
        if (
            state["armed"]
            and Path(candidate).resolve() == path.resolve()
            and not state["mutated"]
        ):
            writer = real_connect(path)
            try:
                writer.execute(
                    "UPDATE runtime_generations SET generation = generation + 1 "
                    "WHERE surface = 'entities'"
                )
                writer.commit()
            finally:
                writer.close()
            state["mutated"] = True
        return digest

    def observe_replace(*args, **kwargs):
        state["cas_called"] = True
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(db_store, "_schema_migration_sha256", mutate_after_live_hash)
    monkeypatch.setattr(
        db_store,
        "_schema_rollback_stage_database",
        arm_after_database_stage,
    )
    monkeypatch.setattr(
        wiki_utils,
        "_replace_prepared_file_compare_and_swap",
        observe_replace,
    )
    with pytest.raises(
        RuntimeError,
        match="pending recovery source changed",
    ):
        unexpected = db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=recovery["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )
        pytest.fail(f"rollback unexpectedly completed: state={state}, result={unexpected}")

    assert state == {"armed": True, "mutated": True, "cas_called": False}
    connection = real_connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW is Windows-only")
def test_windows_database_cas_retries_access_denied_then_sharing_violation(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import wiki_utils

    database_path, stage_path = _isolated_database_replace_paths(
        isolated_memory,
        monkeypatch,
    )
    database_path.write_bytes(b"schema-v9-live")
    stage_path.write_bytes(b"schema-v8-stage")
    sleeps = []
    monkeypatch.setattr(
        wiki_utils,
        "_sleep_windows_replace_retry",
        sleeps.append,
        raising=False,
    )
    scripted = _install_scripted_replace(monkeypatch, [5, 32, 0])

    wiki_utils._replace_prepared_file_compare_and_swap(
        database_path,
        stage_path,
        _sha256(database_path).removeprefix("sha256:"),
    )

    assert scripted.calls == [5, 32, 0]
    assert sleeps == [0.05, 0.1]
    assert database_path.read_bytes() == b"schema-v8-stage"
    assert not stage_path.exists()
    assert not list(database_path.parent.glob(f"{database_path.name}.*.cas-*"))


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW is Windows-only")
def test_windows_database_cas_permanent_sharing_violation_is_bounded(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import wiki_utils

    database_path, stage_path = _isolated_database_replace_paths(
        isolated_memory,
        monkeypatch,
    )
    database_path.write_bytes(b"schema-v9-live")
    stage_path.write_bytes(b"schema-v8-stage")
    sleeps = []
    monkeypatch.setattr(
        wiki_utils,
        "_sleep_windows_replace_retry",
        sleeps.append,
        raising=False,
    )
    scripted = _install_scripted_replace(monkeypatch, [32, 32, 32, 32, 32])

    with pytest.raises(PermissionError) as exc_info:
        wiki_utils._replace_prepared_file_compare_and_swap(
            database_path,
            stage_path,
            _sha256(database_path).removeprefix("sha256:"),
        )

    assert exc_info.value.winerror == 32
    assert scripted.calls == [32, 32, 32, 32, 32]
    assert sleeps == [0.05, 0.1, 0.2, 0.4]
    assert database_path.read_bytes() == b"schema-v9-live"
    assert stage_path.read_bytes() == b"schema-v8-stage"
    assert not list(database_path.parent.glob(f"{database_path.name}.*.cas-*"))


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW is Windows-only")
def test_windows_database_cas_partial_failure_state_is_never_retried(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import wiki_utils

    database_path, stage_path = _isolated_database_replace_paths(
        isolated_memory,
        monkeypatch,
    )
    database_path.write_bytes(b"schema-v9-live")
    stage_path.write_bytes(b"schema-v8-stage")
    sleeps = []
    monkeypatch.setattr(
        wiki_utils,
        "_sleep_windows_replace_retry",
        sleeps.append,
        raising=False,
    )

    def create_partial_backup(_replaced, _replacement, backup):
        backup.write_bytes(b"partial-replace-state")

    scripted = _install_scripted_replace(
        monkeypatch,
        [(5, create_partial_backup), 0],
    )

    with pytest.raises(PermissionError) as exc_info:
        wiki_utils._replace_prepared_file_compare_and_swap(
            database_path,
            stage_path,
            _sha256(database_path).removeprefix("sha256:"),
        )

    assert exc_info.value.winerror == 5
    assert scripted.calls == [5]
    assert sleeps == []
    assert database_path.read_bytes() == b"schema-v9-live"
    assert stage_path.read_bytes() == b"schema-v8-stage"
    backups = list(database_path.parent.glob(f"{database_path.name}.*.cas-backup"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"partial-replace-state"


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW is Windows-only")
def test_windows_database_cas_nonretryable_error_is_immediate(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import wiki_utils

    database_path, stage_path = _isolated_database_replace_paths(
        isolated_memory,
        monkeypatch,
    )
    database_path.write_bytes(b"schema-v9-live")
    stage_path.write_bytes(b"schema-v8-stage")
    sleeps = []
    monkeypatch.setattr(
        wiki_utils,
        "_sleep_windows_replace_retry",
        sleeps.append,
        raising=False,
    )
    scripted = _install_scripted_replace(monkeypatch, [87, 0])

    with pytest.raises(OSError) as exc_info:
        wiki_utils._replace_prepared_file_compare_and_swap(
            database_path,
            stage_path,
            _sha256(database_path).removeprefix("sha256:"),
        )

    assert exc_info.value.winerror == 87
    assert scripted.calls == [87]
    assert sleeps == []
    assert database_path.read_bytes() == b"schema-v9-live"
    assert stage_path.read_bytes() == b"schema-v8-stage"
    assert not list(database_path.parent.glob(f"{database_path.name}.*.cas-*"))


def test_database_cas_annotates_current_hash_permission_error(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import wiki_utils

    database_path, stage_path = _isolated_database_replace_paths(
        isolated_memory,
        monkeypatch,
    )
    database_path.write_bytes(b"schema-v9-live")
    stage_path.write_bytes(b"schema-v8-stage")

    def deny_hash(path):
        if path == database_path:
            raise PermissionError(13, "Permission denied")
        return _sha256(path).removeprefix("sha256:")

    monkeypatch.setattr(wiki_utils, "_file_sha256", deny_hash)
    with pytest.raises(PermissionError) as exc_info:
        wiki_utils._replace_prepared_file_compare_and_swap(
            database_path,
            stage_path,
            "expected-live-hash",
        )

    message = str(exc_info.value)
    assert "phase=current_hash_read" in message
    assert database_path.name in message
    assert "errno=13" in message


@pytest.mark.skipif(os.name != "nt", reason="WinError is Windows-only")
def test_database_cas_annotates_replacefile_access_denied(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import wiki_utils

    database_path, stage_path = _isolated_database_replace_paths(
        isolated_memory,
        monkeypatch,
    )
    database_path.write_bytes(b"schema-v9-live")
    stage_path.write_bytes(b"schema-v8-stage")
    expected_hash = _sha256(database_path).removeprefix("sha256:")

    def deny_replace(*_args, **_kwargs):
        raise ctypes.WinError(5)

    monkeypatch.setattr(wiki_utils, "_replace_file_with_backup", deny_replace)
    with pytest.raises(PermissionError) as exc_info:
        wiki_utils._replace_prepared_file_compare_and_swap(
            database_path,
            stage_path,
            expected_hash,
        )

    message = str(exc_info.value)
    assert "phase=replace_file" in message
    assert database_path.name in message
    assert "errno=13" in message
    assert "winerror=5" in message
    assert exc_info.value.winerror == 5


def test_schema_rollback_projection_publish_failure_resumes_from_pending(
    isolated_memory,
    monkeypatch,
):
    path, _v8_sha256, v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    migration_receipt = Path(migration["receipt_path"])
    governance_store.upsert_entity(
        "entity_projection_resume",
        {
            "entity_id": "entity_projection_resume",
            "page_key": "Concept_Projection-Resume",
            "canonical_name": "Projection Resume",
            "type": "concept",
            "raw_text": "Post-migration projection drift.",
        },
    )
    indexer.generate_index()
    db_store.close_all_connections()
    preview = db_store.preview_schema_rollback(migration_receipt, path)
    real_publish = db_store._schema_rollback_publish_projection

    monkeypatch.setattr(
        db_store,
        "_schema_rollback_publish_projection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected projection publication failure")
        ),
    )
    with pytest.raises(OSError, match="projection publication failure"):
        db_store.schema_rollback_maintenance(
            migration_receipt=migration_receipt,
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            confirm_data_rewind=True,
            db_path=path,
        )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()
    recovery = db_store.preview_schema_rollback(migration_receipt, path)
    assert recovery["recovery_action"] == "resume_projection_restore"

    monkeypatch.setattr(
        db_store,
        "_schema_rollback_publish_projection",
        real_publish,
    )
    result = db_store.schema_rollback_maintenance(
        migration_receipt=migration_receipt,
        apply=True,
        confirmation=recovery["fingerprint"],
        confirm_no_writers=True,
        confirm_data_rewind=True,
        db_path=path,
    )
    assert result["applied"] is True
    assert result["recovery_action"] == "resumed_and_completed_rollback"
    assert _projection_hashes() == v8_projection


def test_schema_rollback_cli_is_preview_first_and_heavy_only_on_apply(
    monkeypatch,
    capsys,
):
    receipt = str(Path.cwd() / "receipt.json")
    preview_args = cli_app.build_parser().parse_args(
        ["schema-rollback", "--migration-receipt", receipt]
    )
    apply_args = cli_app.build_parser().parse_args(
        [
            "schema-rollback",
            "--migration-receipt",
            receipt,
            "--apply",
            "--confirm-fingerprint",
            "sha256:abc",
            "--confirm-no-writers",
            "--confirm-data-rewind",
        ]
    )
    assert preview_args.apply is False
    assert cli_app._cli_heavy_task_policy(preview_args) is None
    assert cli_app._cli_heavy_task_policy(apply_args) == ("maintenance", 1800.0)
    command_choices = next(
        action.choices
        for action in cli_app.build_parser()._actions
        if isinstance(getattr(action, "choices", None), dict)
        and "schema-rollback" in action.choices
    )
    assert len(command_choices) == 40

    calls = []
    gate_calls = []

    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        "vector_lake.heavy_task_gate.heavy_task",
        lambda task_class, operation, **kwargs: (
            gate_calls.append((task_class, operation, kwargs)) or Lease()
        ),
    )
    monkeypatch.setattr(
        db_store,
        "schema_rollback_maintenance",
        lambda **kwargs: calls.append(kwargs) or {"applied": True},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "cli.py",
            "schema-rollback",
            "--migration-receipt",
            receipt,
            "--apply",
            "--confirm-fingerprint",
            "sha256:abc",
            "--confirm-no-writers",
            "--confirm-data-rewind",
        ],
    )

    assert cli_app.main() == 0
    assert calls == [
        {
            "migration_receipt": receipt,
            "apply": True,
            "confirmation": "sha256:abc",
            "confirm_no_writers": True,
            "confirm_data_rewind": True,
        }
    ]
    assert gate_calls[0][0:2] == ("maintenance", "schema-rollback")
    capsys.readouterr()


@pytest.mark.skipif(
    not (_ACTIVE_11_19_1 / "cli.py").is_file(),
    reason="the frozen active 11.19.1 acceptance runtime is unavailable",
)
def test_schema_rollback_is_accepted_by_fresh_11_19_1_processes(
    isolated_memory,
):
    path, _v8_sha256, _v8_projection = _v8_database_with_projection()
    migration = _migrate_v8_to_v9(path)
    receipt_path = Path(migration["receipt_path"])
    preview = db_store.preview_schema_rollback(receipt_path, path)
    result = db_store.schema_rollback_maintenance(
        migration_receipt=receipt_path,
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )
    assert result["post"]["schema_state"]["user_version"] == 8

    environment = dict(os.environ)
    environment.pop("GEMINI_API_KEY", None)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "VECTOR_LAKE_MEMORY_DIR": str(isolated_memory),
            "VECTOR_LAKE_META_DIR": str(isolated_memory / "wiki" / ".meta"),
            "VECTOR_LAKE_DB_PATH": str(path),
            "VECTOR_LAKE_EMBEDDING_ENABLED": "0",
            "VECTOR_LAKE_QUERY_EMBEDDING": "0",
            "VECTOR_LAKE_CLI_HEAVY_TASK_WAIT_SECONDS": "0",
        }
    )
    search = subprocess.run(
        [
            sys.executable,
            "-B",
            "cli.py",
            "search",
            "Schema Rollback",
            "--top_k",
            "5",
        ],
        cwd=_ACTIVE_11_19_1,
        env=environment,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert search.returncode == 0, search.stderr
    assert "Schema Rollback" in search.stdout

    doctor = subprocess.run(
        [sys.executable, "-B", "cli.py", "doctor"],
        cwd=_ACTIVE_11_19_1,
        env=environment,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # Doctor can still report unrelated watchdog/projection health in this
    # deliberately minimal fixture; schema compatibility itself must be green.
    assert doctor.returncode in {0, 2}, doctor.stderr
    assert "[OK] Schema Migrations" in doctor.stdout
    assert "user_version=8; supported=8; ledger_entries=8" in doctor.stdout

    mcp = subprocess.run(
        [sys.executable, "-B", "-m", "vector_lake.mcp_server"],
        cwd=_ACTIVE_11_19_1,
        env=environment,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert mcp.returncode == 0, mcp.stderr
