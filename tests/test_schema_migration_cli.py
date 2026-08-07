import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from vector_lake import cli_app, db_store


def _physical_tree_identity(root: Path) -> list[tuple]:
    result = []
    if not root.exists():
        return result
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        stat = path.stat()
        result.append(
            (
                str(path.relative_to(root)),
                path.is_dir(),
                int(stat.st_size),
                int(stat.st_mtime_ns),
                int(stat.st_ctime_ns),
            )
        )
    return result


def _downgrade_ledger(path: Path, version: int, *, duplicate_indexes=False) -> None:
    db_store.close_all_connections()
    connection = sqlite3.connect(path)
    try:
        if duplicate_indexes:
            connection.execute("CREATE INDEX idx_date ON timeline_events(event_date)")
            connection.execute("CREATE INDEX idx_entity ON timeline_events(entity_id)")
        connection.execute("DELETE FROM schema_migrations WHERE version > ?", (version,))
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))


def _ready_database(version: int, *, duplicate_indexes=False) -> Path:
    db_store.init_db()
    path = db_store.get_db_path().resolve()
    db_store.close_all_connections()
    if version != db_store._SCHEMA_VERSION:
        _downgrade_ledger(
            path,
            version,
            duplicate_indexes=duplicate_indexes,
        )
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _create_uncheckpointed_wal(path: Path) -> sqlite3.Connection:
    writer = sqlite3.connect(path, timeout=0, isolation_level=None)
    writer.execute("PRAGMA busy_timeout=0")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO entities "
        "(entity_id, canonical_name, data_json, updated_at) "
        "VALUES ('checkpoint-entity', 'Checkpoint Entity', '{}', "
        "'2026-08-07T00:00:00+00:00')"
    )
    assert Path(str(path) + "-wal").stat().st_size > 0
    return writer


def test_schema_migration_missing_preview_is_physically_read_only(isolated_memory):
    path = db_store.peek_db_path().resolve()
    before = _physical_tree_identity(isolated_memory)

    preview = db_store.schema_migration_maintenance(db_path=path)

    assert preview["dry_run"] is True
    assert preview["applied"] is False
    assert preview["can_apply"] is False
    assert preview["issues"] == ["database_missing"]
    assert preview["fingerprint"].startswith("sha256:")
    assert _physical_tree_identity(isolated_memory) == before
    assert not path.exists()
    assert not (path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME).exists()


def test_schema_migration_existing_preview_preserves_all_physical_identity(
    isolated_memory,
):
    path = _ready_database(5)
    before = _physical_tree_identity(isolated_memory)

    preview = db_store.preview_schema_migration(path)

    assert preview["can_apply"] is True
    assert preview["pre_schema_version"] == 5
    assert preview["steps"] == ["schema_v5_to_v6"]
    assert _physical_tree_identity(isolated_memory) == before


@pytest.mark.parametrize("version", [1, 2, 3])
def test_schema_migration_explicitly_rejects_v1_to_v3(version, isolated_memory):
    path = _ready_database(version)

    preview = db_store.preview_schema_migration(path)

    assert preview["can_apply"] is False
    assert f"unsupported_source_schema_v{version}:minimum_supported_v4" in (
        preview["issues"]
    )
    with pytest.raises(RuntimeError, match="minimum_supported_v4"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )


@pytest.mark.parametrize(
    ("version", "expected_steps"),
    [
        (4, ["schema_v4_to_v5", "schema_v5_to_v6"]),
        (5, ["schema_v5_to_v6"]),
    ],
)
def test_schema_migration_applies_with_verified_backup_and_receipt(
    version,
    expected_steps,
    isolated_memory,
):
    path = _ready_database(version, duplicate_indexes=(version == 4))
    preview = db_store.preview_schema_migration(path)

    result = db_store.schema_migration_maintenance(
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert result["applied"] is True
    assert result["projection_rebuild_required"] is True
    assert result["plan_fingerprint"] == preview["fingerprint"]
    assert result["pre"]["user_version"] == version
    assert result["post"]["ready"] is True
    assert result["post"]["user_version"] == 6
    backup_path = Path(result["backup"]["path"])
    assert backup_path.is_file()
    assert result["backup"]["sha256"] == _sha256(backup_path)
    assert result["backup"]["quick_check"] == "ok"
    assert not Path(str(backup_path) + "-wal").exists()
    assert not Path(str(backup_path) + "-shm").exists()
    backup = sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == version
    finally:
        backup.close()
    assert not Path(str(backup_path) + "-wal").exists()
    assert not Path(str(backup_path) + "-shm").exists()
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    pending_receipt = json.loads(
        Path(result["pending_receipt_path"]).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "completed"
    assert pending_receipt["status"] == "pending"
    assert receipt["steps"] == expected_steps
    assert receipt["plan_fingerprint"] == preview["fingerprint"]
    assert receipt["plan"] == db_store._schema_migration_plan_core(preview)
    assert receipt["source_binding"]["source_identity"] == preview["source_identity"]
    assert receipt["backup"]["sha256"] == result["backup"]["sha256"]
    assert receipt["projection_rebuild_required"] is True
    assert db_store.inspect_schema_migration_state(path)["ready"] is True


def test_schema_migration_v6_apply_is_idempotent_no_op(isolated_memory):
    path = _ready_database(6)
    preview = db_store.preview_schema_migration(path)

    result = db_store.schema_migration_maintenance(
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert preview["no_op"] is True
    assert result["applied"] is False
    assert result["no_op"] is True
    assert result["backup"] is None
    assert result["receipt_path"] is None
    assert not (path.parent / "schema-migration-backups").exists()
    assert not (path.parent / "schema-migration-receipts").exists()


def test_schema_migration_wrong_fingerprint_fails_before_backup_or_ddl(
    isolated_memory,
):
    path = _ready_database(5)
    before = _physical_tree_identity(isolated_memory)

    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation="sha256:" + "0" * 64,
            confirm_no_writers=True,
            db_path=path,
        )

    assert not (path.parent / "schema-migration-backups").exists()
    assert not (path.parent / "schema-migration-receipts").exists()
    assert _physical_tree_identity(isolated_memory) == before
    assert db_store.inspect_schema_migration_state(path)["user_version"] == 5


def test_schema_migration_requires_explicit_no_writer_confirmation(isolated_memory):
    path = _ready_database(5)
    preview = db_store.preview_schema_migration(path)

    with pytest.raises(RuntimeError, match="--confirm-no-writers"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=preview["fingerprint"],
            db_path=path,
        )

    assert not (path.parent / "schema-migration-backups").exists()


def test_schema_migration_rejects_an_external_writer_before_backup(isolated_memory):
    path = _ready_database(5)
    preview = db_store.preview_schema_migration(path)
    writer = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError):
            db_store.schema_migration_maintenance(
                apply=True,
                confirmation=preview["fingerprint"],
                confirm_no_writers=True,
                db_path=path,
            )
    finally:
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()

    assert not (path.parent / "schema-migration-backups").exists()
    assert not (path.parent / "schema-migration-receipts").exists()


def test_schema_migration_rejects_commit_after_locked_preview_before_backup(
    isolated_memory,
    monkeypatch,
):
    path = _ready_database(5)
    initial_preview = db_store.preview_schema_migration(path)
    real_preview = db_store.preview_schema_migration
    calls = 0

    def preview_then_commit(db_path=None):
        nonlocal calls
        plan = real_preview(db_path)
        calls += 1
        if calls == 2:
            writer = sqlite3.connect(path)
            try:
                writer.execute(
                    "INSERT INTO entities "
                    "(entity_id, canonical_name, data_json, updated_at) "
                    "VALUES ('toctou-entity', 'TOCTOU Entity', '{}', "
                    "'2026-08-07T00:00:00+00:00')"
                )
                writer.commit()
            finally:
                writer.close()
        return plan

    monkeypatch.setattr(db_store, "preview_schema_migration", preview_then_commit)

    with pytest.raises(
        RuntimeError,
        match="changed after the locked schema migration preview",
    ):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=initial_preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT canonical_name FROM entities WHERE entity_id='toctou-entity'"
        ).fetchone() == ("TOCTOU Entity",)
    finally:
        connection.close()
    assert not (path.parent / "schema-migration-backups").exists()
    assert not (path.parent / "schema-migration-receipts").exists()


def test_schema_migration_v4_to_v6_rolls_back_as_one_transaction(
    isolated_memory,
    monkeypatch,
):
    path = _ready_database(4, duplicate_indexes=True)
    preview = db_store.preview_schema_migration(path)
    real_v6 = db_store._apply_controlled_schema_v6_migration

    def fail_after_v6(connection, *, maintenance_lock):
        real_v6(connection, maintenance_lock=maintenance_lock)
        raise RuntimeError("injected v6 failure")

    monkeypatch.setattr(
        db_store,
        "_apply_controlled_schema_v6_migration",
        fail_after_v6,
    )

    with pytest.raises(RuntimeError, match="injected v6 failure"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('idx_date', 'idx_entity')"
            )
        }
        assert indexes == {"idx_date", "idx_entity"}
    finally:
        connection.close()
    backups = list((path.parent / "schema-migration-backups").glob("*.db"))
    pending = list(
        (path.parent / "schema-migration-receipts").glob("*.pending.json")
    )
    assert len(backups) == 1
    assert len(pending) == 1
    manifest = json.loads(pending[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "pending"
    assert manifest["backup"]["sha256"] == _sha256(backups[0])


def test_schema_migration_pending_receipt_recovers_after_completion_publish_failure(
    isolated_memory,
    monkeypatch,
):
    path = _ready_database(5)
    preview = db_store.preview_schema_migration(path)
    real_atomic_json = db_store._schema_migration_atomic_json

    def fail_completed(path_value, payload):
        if payload.get("status") == "completed":
            raise OSError("injected completed receipt failure")
        return real_atomic_json(path_value, payload)

    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", fail_completed)
    with pytest.raises(RuntimeError, match="receipt publication failed"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert db_store.inspect_schema_migration_state(path)["user_version"] == 6
    completed_path, pending_path = db_store._schema_migration_receipt_paths(path)
    assert not completed_path.exists()
    assert json.loads(pending_path.read_text(encoding="utf-8"))["status"] == "pending"
    backups_before = list((path.parent / "schema-migration-backups").glob("*.db"))
    recovery_preview = db_store.preview_schema_migration(path)
    assert recovery_preview["no_op"] is True
    assert recovery_preview["projection_rebuild_required"] is True
    assert recovery_preview["pending_receipt"]["path"] == str(pending_path)

    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", real_atomic_json)
    recovered = db_store.schema_migration_maintenance(
        apply=True,
        confirmation=recovery_preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert recovered["applied"] is False
    assert recovered["no_op"] is True
    assert recovered["projection_rebuild_required"] is True
    assert list((path.parent / "schema-migration-backups").glob("*.db")) == (
        backups_before
    )
    assert json.loads(completed_path.read_text(encoding="utf-8"))["status"] == (
        "completed"
    )


def test_schema_migration_pending_publish_failure_stops_before_ddl(
    isolated_memory,
    monkeypatch,
):
    path = _ready_database(5)
    preview = db_store.preview_schema_migration(path)
    real_atomic_json = db_store._schema_migration_atomic_json

    def fail_pending(path_value, payload):
        if payload.get("status") == "pending":
            raise OSError("injected pending receipt failure")
        return real_atomic_json(path_value, payload)

    monkeypatch.setattr(db_store, "_schema_migration_atomic_json", fail_pending)
    with pytest.raises(RuntimeError, match="before DDL"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert db_store.inspect_schema_migration_state(path)["user_version"] == 5
    assert len(list((path.parent / "schema-migration-backups").glob("*.db"))) == 1
    assert not (path.parent / "schema-migration-receipts").exists()


def test_schema_migration_checkpoint_wal_returns_a_fresh_preview(isolated_memory):
    path = _ready_database(5)
    writer = _create_uncheckpointed_wal(path)
    try:
        preview = db_store.preview_schema_migration(path)
        assert preview["issues"] == ["database_has_uncheckpointed_wal"]

        result = db_store.schema_migration_maintenance(
            checkpoint_wal=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )
    finally:
        writer.close()

    assert result["checkpoint_wal_applied"] is True
    assert result["checkpoint_result"]["busy"] == 0
    assert result["can_apply"] is True
    assert result["pre_schema_version"] == 5
    assert not (path.parent / "schema-migration-backups").exists()
    assert not (path.parent / "schema-migration-receipts").exists()


def test_schema_migration_checkpoint_rejects_an_external_writer(isolated_memory):
    path = _ready_database(5)
    writer = _create_uncheckpointed_wal(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE entities SET canonical_name='Busy Writer' "
        "WHERE entity_id='checkpoint-entity'"
    )
    preview = db_store.preview_schema_migration(path)
    try:
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            db_store.schema_migration_maintenance(
                checkpoint_wal=True,
                confirmation=preview["fingerprint"],
                confirm_no_writers=True,
                db_path=path,
            )
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert db_store.inspect_schema_migration_state(path)["user_version"] == 5
    assert not (path.parent / "schema-migration-backups").exists()


def test_schema_migration_parser_and_dispatch_are_cli_only_and_apply_gated(
    isolated_memory,
    monkeypatch,
    capsys,
):
    preview_args = cli_app.build_parser().parse_args(["schema-migrate"])
    apply_args = cli_app.build_parser().parse_args(
        [
            "schema-migrate",
            "--apply",
            "--confirm-fingerprint",
            "sha256:abc",
            "--confirm-no-writers",
        ]
    )
    checkpoint_args = cli_app.build_parser().parse_args(
        [
            "schema-migrate",
            "--checkpoint-wal",
            "--confirm-fingerprint",
            "sha256:abc",
            "--confirm-no-writers",
        ]
    )
    with pytest.raises(SystemExit):
        cli_app.build_parser().parse_args(
            ["schema-migrate", "--apply", "--checkpoint-wal"]
        )
    assert preview_args.apply is False
    assert preview_args.confirm_fingerprint == ""
    assert preview_args.confirm_no_writers is False
    assert cli_app._cli_heavy_task_policy(preview_args) is None
    assert cli_app._cli_heavy_task_policy(apply_args) == ("maintenance", 1800.0)
    assert cli_app._cli_heavy_task_policy(checkpoint_args) == (
        "maintenance",
        1800.0,
    )
    command_choices = next(
        action.choices
        for action in cli_app.build_parser()._actions
        if isinstance(getattr(action, "choices", None), dict)
        and "schema-migrate" in action.choices
    )
    assert len(command_choices) == 32

    calls = []
    gate_calls = []

    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_heavy_task(task_class, operation, **kwargs):
        gate_calls.append((task_class, operation, kwargs))
        return Lease()

    def fake_migration(**kwargs):
        calls.append(kwargs)
        return {"applied": True}

    from vector_lake import heavy_task_gate

    monkeypatch.setattr(heavy_task_gate, "heavy_task", fake_heavy_task)
    monkeypatch.setattr(db_store, "schema_migration_maintenance", fake_migration)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cli.py",
            "schema-migrate",
            "--apply",
            "--confirm-fingerprint",
            "sha256:abc",
            "--confirm-no-writers",
        ],
    )

    assert cli_app.main() == 0
    assert calls == [
        {
            "apply": True,
            "checkpoint_wal": False,
            "confirmation": "sha256:abc",
            "confirm_no_writers": True,
        }
    ]
    assert gate_calls[0][0:2] == ("maintenance", "schema-migrate")
    capsys.readouterr()

    calls.clear()
    gate_calls.clear()
    monkeypatch.setattr(
        "sys.argv",
        [
            "cli.py",
            "schema-migrate",
            "--checkpoint-wal",
            "--confirm-fingerprint",
            "sha256:def",
            "--confirm-no-writers",
        ],
    )
    assert cli_app.main() == 0
    assert calls == [
        {
            "apply": False,
            "checkpoint_wal": True,
            "confirmation": "sha256:def",
            "confirm_no_writers": True,
        }
    ]
    assert gate_calls[0][0:2] == ("maintenance", "schema-migrate")
    capsys.readouterr()
