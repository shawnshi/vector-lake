import json
import hashlib
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pytest

from vector_lake import db_store
from vector_lake.runtime_health import assess_runtime_health
from vector_lake import tool_doctor
from vector_lake.tool_doctor import doctor_vector_lake
from vector_lake.wiki_utils import peek_meta_dir
from vector_lake.tool_projection import projection_diff_report


def test_read_only_diagnostics_do_not_create_meta_state(isolated_memory):
    expected_meta = isolated_memory / "wiki" / ".meta"
    expected_db = expected_meta / "vector_lake.db"
    assert expected_meta.exists() is False

    assert peek_meta_dir() == expected_meta
    assert db_store.peek_db_path() == expected_db
    schema = db_store.inspect_schema_migration_state()
    health = assess_runtime_health()
    doctor = doctor_vector_lake()
    projection_report = projection_diff_report()

    assert schema["status"] == "missing"
    assert schema["issues"] == ["database_missing"]
    assert health["status"] == "blocked"
    assert "database_blocked:database_missing:" in health["issues"][0]
    assert "[WARN] Gemini Embedding: Unavailable" in doctor
    assert "[FAIL] Schema Migrations:" in doctor
    assert "database_missing" in doctor
    assert "SQLite canonical entities: 0" in projection_report
    assert expected_meta.exists() is False
    assert expected_db.exists() is False


def test_checkpointed_read_only_snapshot_rejects_pending_wal_without_touching(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    wal_path = Path(str(db_path) + "-wal")
    wal_path.write_bytes(b"pending-frame-sentinel")
    before = wal_path.read_bytes()

    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="uncheckpointed_wal",
    ):
        with db_store.checkpointed_read_only_snapshot(db_path):
            pass

    assert wal_path.read_bytes() == before


def test_read_only_transaction_snapshot_is_consistent_across_concurrent_commit(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE snapshot_probe (value INTEGER NOT NULL)")
        writer.execute("INSERT INTO snapshot_probe (value) VALUES (1)")
        writer.commit()

        with db_store.read_only_transaction_snapshot(db_path) as snapshot:
            before = snapshot.execute(
                "SELECT COUNT(*) FROM snapshot_probe"
            ).fetchone()[0]
            writer.execute("INSERT INTO snapshot_probe (value) VALUES (2)")
            writer.commit()
            after = snapshot.execute(
                "SELECT COUNT(*) FROM snapshot_probe"
            ).fetchone()[0]

        assert before == 1
        assert after == 1
        assert writer.execute("SELECT COUNT(*) FROM snapshot_probe").fetchone()[0] == 2
    finally:
        writer.close()


def test_read_only_transaction_snapshot_rejects_commit_between_validation_and_begin(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE snapshot_race_probe (value INTEGER NOT NULL)")
    writer.execute("INSERT INTO snapshot_race_probe (value) VALUES (1)")
    writer.commit()
    real_validate = db_store._validate_nonempty_wal_sidecars

    def commit_after_validation(path):
        token = real_validate(path)
        writer.execute("INSERT INTO snapshot_race_probe (value) VALUES (2)")
        writer.commit()
        return token

    monkeypatch.setattr(
        db_store,
        "_validate_nonempty_wal_sidecars",
        commit_after_validation,
    )
    try:
        with pytest.raises(
            db_store.ReadOnlySnapshotUnavailable,
            match="wal_changed_before_snapshot_begin",
        ):
            with db_store.read_only_transaction_snapshot(db_path):
                pass
    finally:
        writer.close()


def test_read_only_transaction_snapshot_revalidates_frames_after_begin(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    writer = db_store.get_connection()
    writer.execute("PRAGMA wal_autocheckpoint=0")
    with db_store.transaction():
        writer.execute("CREATE TABLE wal_frame_race_probe (value TEXT NOT NULL)")
        writer.execute(
            "INSERT INTO wal_frame_race_probe (value) VALUES (?)",
            ("committed-frame-marker",),
        )

    source_wal = Path(str(db_path) + "-wal")
    source_shm = Path(str(db_path) + "-shm")
    clone_dir = isolated_memory / "scratch" / "wal-frame-validation-race"
    clone_dir.mkdir(parents=True)
    clone_db = clone_dir / "vector_lake.db"
    clone_wal = Path(str(clone_db) + "-wal")
    clone_shm = Path(str(clone_db) + "-shm")
    shutil.copyfile(db_path, clone_db)
    shutil.copyfile(source_wal, clone_wal)
    shutil.copyfile(source_shm, clone_shm)
    real_validate = db_store._validate_nonempty_wal_sidecars
    validation_calls = 0

    def corrupt_after_first_validation(path):
        nonlocal validation_calls
        token = real_validate(path)
        validation_calls += 1
        if validation_calls == 1:
            wal_bytes = bytearray(clone_wal.read_bytes())
            wal_bytes[32 + 24 + 100] ^= 1
            clone_wal.write_bytes(wal_bytes)
        return token

    monkeypatch.setattr(
        db_store,
        "_validate_nonempty_wal_sidecars",
        corrupt_after_first_validation,
    )

    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="wal_changed_before_snapshot_begin",
    ):
        with db_store.read_only_transaction_snapshot(clone_db):
            pass

    assert validation_calls == 1


def test_read_only_transaction_snapshot_rejects_invalid_wal_without_touching(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    wal_path = Path(str(db_path) + "-wal")
    wal_path.write_bytes(b"not-a-sqlite-wal")
    before = wal_path.read_bytes()

    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="invalid_wal_header",
    ):
        with db_store.read_only_transaction_snapshot(db_path):
            pass

    assert wal_path.read_bytes() == before


def test_read_only_transaction_snapshot_rejects_corrupt_wal_index_before_open(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    writer = db_store.get_connection()
    writer.execute("PRAGMA wal_autocheckpoint=0")
    with db_store.transaction():
        writer.execute("CREATE TABLE wal_index_probe (value INTEGER NOT NULL)")
        writer.execute("INSERT INTO wal_index_probe (value) VALUES (1)")

    source_wal = Path(str(db_path) + "-wal")
    source_shm = Path(str(db_path) + "-shm")
    clone_dir = isolated_memory / "scratch" / "corrupt-wal-index"
    clone_dir.mkdir(parents=True)
    clone_db = clone_dir / "vector_lake.db"
    clone_wal = Path(str(clone_db) + "-wal")
    clone_shm = Path(str(clone_db) + "-shm")
    shutil.copyfile(db_path, clone_db)
    shutil.copyfile(source_wal, clone_wal)
    shutil.copyfile(source_shm, clone_shm)
    clone_shm.write_bytes(b"\xA5" * clone_shm.stat().st_size)

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {
        path: (path.stat().st_size, digest(path))
        for path in (clone_db, clone_wal, clone_shm)
    }
    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="invalid_wal_index",
    ):
        with db_store.read_only_transaction_snapshot(clone_db):
            pass
    after = {
        path: (path.stat().st_size, digest(path))
        for path in (clone_db, clone_wal, clone_shm)
    }

    assert after == before


def test_read_only_transaction_snapshot_rejects_matching_bad_wal_index_checksum(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    writer = db_store.get_connection()
    writer.execute("PRAGMA wal_autocheckpoint=0")
    with db_store.transaction():
        writer.execute("CREATE TABLE wal_checksum_probe (value INTEGER NOT NULL)")
        writer.execute("INSERT INTO wal_checksum_probe (value) VALUES (9)")

    source_wal = Path(str(db_path) + "-wal")
    source_shm = Path(str(db_path) + "-shm")
    clone_dir = isolated_memory / "scratch" / "bad-wal-index-checksum"
    clone_dir.mkdir(parents=True)
    clone_db = clone_dir / "vector_lake.db"
    clone_wal = Path(str(clone_db) + "-wal")
    clone_shm = Path(str(clone_db) + "-shm")
    shutil.copyfile(db_path, clone_db)
    shutil.copyfile(source_wal, clone_wal)
    shutil.copyfile(source_shm, clone_shm)
    shm_bytes = bytearray(clone_shm.read_bytes())
    shm_bytes[8] ^= 1
    shm_bytes[56] ^= 1
    clone_shm.write_bytes(shm_bytes)

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {
        path: (path.stat().st_size, digest(path))
        for path in (clone_db, clone_wal, clone_shm)
    }
    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="invalid_wal_index_checksum",
    ):
        with db_store.read_only_transaction_snapshot(clone_db):
            pass
    after = {
        path: (path.stat().st_size, digest(path))
        for path in (clone_db, clone_wal, clone_shm)
    }

    assert after == before


def test_read_only_transaction_snapshot_rejects_corrupt_committed_wal_frame(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    writer = db_store.get_connection()
    writer.execute("PRAGMA wal_autocheckpoint=0")
    with db_store.transaction():
        writer.execute("CREATE TABLE wal_frame_probe (value TEXT NOT NULL)")
        writer.execute(
            "INSERT INTO wal_frame_probe (value) VALUES (?)",
            ("committed-frame-marker",),
        )

    source_wal = Path(str(db_path) + "-wal")
    source_shm = Path(str(db_path) + "-shm")
    clone_dir = isolated_memory / "scratch" / "bad-committed-wal-frame"
    clone_dir.mkdir(parents=True)
    clone_db = clone_dir / "vector_lake.db"
    clone_wal = Path(str(clone_db) + "-wal")
    clone_shm = Path(str(clone_db) + "-shm")
    shutil.copyfile(db_path, clone_db)
    shutil.copyfile(source_wal, clone_wal)
    shutil.copyfile(source_shm, clone_shm)
    wal_bytes = bytearray(clone_wal.read_bytes())
    wal_page_size = int.from_bytes(wal_bytes[8:12], "big")
    if wal_page_size == 1:
        wal_page_size = 65_536
    first_frame_payload_offset = 32 + 24
    assert len(wal_bytes) >= first_frame_payload_offset + wal_page_size
    wal_bytes[first_frame_payload_offset + 100] ^= 1
    clone_wal.write_bytes(wal_bytes)

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {
        path: (path.stat().st_size, digest(path))
        for path in (clone_db, clone_wal, clone_shm)
    }
    with pytest.raises(
        db_store.ReadOnlySnapshotUnavailable,
        match="invalid_wal_frame_checksum",
    ):
        with db_store.read_only_transaction_snapshot(clone_db):
            pass
    after = {
        path: (path.stat().st_size, digest(path))
        for path in (clone_db, clone_wal, clone_shm)
    }

    assert after == before


def test_doctor_uses_immutable_snapshot_for_closed_database(
    isolated_memory,
):
    db_store.init_db()
    db_path = db_store.get_db_path()
    db_store.close_all_connections()
    paths = [
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    ]

    def identity(path):
        if not path.exists():
            return (False, 0, 0)
        stat = path.stat()
        return (True, stat.st_size, stat.st_mtime_ns)

    before = {path: identity(path) for path in paths}
    report = doctor_vector_lake()
    after = {path: identity(path) for path in paths}

    assert "[OK] Schema Migrations:" in report
    assert before == after


def test_dependency_probe_does_not_execute_installed_module(monkeypatch):
    observed = []

    def find_spec(module_name):
        observed.append(module_name)
        return object()

    monkeypatch.setattr(tool_doctor.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(
        tool_doctor.importlib,
        "import_module",
        lambda module_name: (_ for _ in ()).throw(
            AssertionError(f"unexpected import: {module_name}")
        ),
    )

    assert tool_doctor._dependency_available("sqlite_vec") is True
    assert observed == ["sqlite_vec"]


def test_schema_readiness_reasons_explain_migratable_older_schema():
    reasons = tool_doctor._schema_readiness_reasons(
        {
            "ready": False,
            "status": "invalid",
            "user_version": 4,
            "supported_version": 5,
            "issues": [],
        }
    )

    assert reasons == ["database_schema_upgrade_required:4->5"]


def test_doctor_rejects_stale_watchdog_component(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text(
        json.dumps({"nodes": {}, "graph_state": {"dirty": False}}),
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc).isoformat()
    status_path = isolated_memory / "wiki" / ".meta" / ".watchdog_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "idle",
                "current_action": "waiting",
                "updated_at": now,
                "components": {
                    "scheduler": {
                        "status": "idle",
                        "heartbeat_at": "2000-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "VECTOR_LAKE_WATCHDOG_COMPONENT_MAX_AGE_SECONDS",
        "30",
    )

    doctor = doctor_vector_lake()

    watchdog_line = next(
        line for line in doctor.splitlines() if "Watchdog Status:" in line
    )
    assert watchdog_line.startswith("[FAIL]")
    assert "stale=scheduler" in watchdog_line


def test_doctor_rejects_stopped_watchdog_component(isolated_memory):
    db_store.init_db()
    now = datetime.now(timezone.utc).isoformat()
    status_path = isolated_memory / "wiki" / ".meta" / ".watchdog_status.json"
    status_path.write_text(
        json.dumps(
            {
                "status": "idle",
                "updated_at": now,
                "components": {
                    "ingest": {
                        "status": "stopped",
                        "heartbeat_at": now,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    doctor = doctor_vector_lake()
    watchdog_line = next(
        line for line in doctor.splitlines() if "Watchdog Status:" in line
    )

    assert watchdog_line.startswith("[FAIL]")
    assert "unhealthy=ingest" in watchdog_line
