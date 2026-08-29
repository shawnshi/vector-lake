from collections import namedtuple
from pathlib import Path

import pytest

from vector_lake import backup_capacity, db_store, tool_projection


DiskUsage = namedtuple("DiskUsage", "total used free")


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "backups"
    second = tmp_path / "schema-migration-backups"
    first.mkdir()
    second.mkdir()
    return first, second


def test_capacity_status_counts_all_backup_roots(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    (roots[0] / "one.bin").write_bytes(b"a" * 40)
    (roots[1] / "two.bin").write_bytes(b"b" * 60)
    monkeypatch.setattr(
        backup_capacity.shutil,
        "disk_usage",
        lambda _path: DiskUsage(1_000, 100, 900),
    )
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_BYTES", "10")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_RATIO", "0.1")

    status = backup_capacity.backup_capacity_status(
        estimated_new_bytes=50,
        backup_roots=roots,
        disk_anchor=tmp_path,
    )

    assert status["allowed"] is True
    assert status["current_backup_bytes"] == 100
    assert status["projected_backup_bytes"] == 150
    assert status["projected_free_bytes"] == 850
    assert status["minimum_free_required_bytes"] == 100
    assert status["warnings"] == ["backup_max_total_bytes_unconfigured"]


def test_enforced_total_quota_blocks_before_staging(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    (roots[0] / "existing.bin").write_bytes(b"x" * 60)
    monkeypatch.setattr(
        backup_capacity.shutil,
        "disk_usage",
        lambda _path: DiskUsage(10_000, 1_000, 9_000),
    )
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MAX_TOTAL_BYTES", "100")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_RATIO", "0")

    with pytest.raises(backup_capacity.BackupCapacityError) as error:
        backup_capacity.assert_backup_capacity(
            estimated_new_bytes=50,
            operation="unit-test",
            backup_roots=roots,
            disk_anchor=tmp_path,
        )

    assert error.value.reason == "max_total_bytes"
    assert error.value.status["projected_backup_bytes"] == 110
    assert error.value.status["operation"] == "unit-test"


def test_report_mode_exposes_overage_without_blocking(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    (roots[0] / "existing.bin").write_bytes(b"x" * 60)
    monkeypatch.setattr(
        backup_capacity.shutil,
        "disk_usage",
        lambda _path: DiskUsage(10_000, 1_000, 9_000),
    )
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MAX_TOTAL_BYTES", "100")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_QUOTA_MODE", "report")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_RATIO", "0")

    status = backup_capacity.assert_backup_capacity(
        estimated_new_bytes=50,
        operation="unit-test",
        backup_roots=roots,
        disk_anchor=tmp_path,
    )

    assert status["allowed"] is True
    assert status["quota_exceeded"] is True
    assert "backup_max_total_bytes_exceeded" in status["warnings"]


def test_minimum_free_space_is_always_enforced(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    monkeypatch.setattr(
        backup_capacity.shutil,
        "disk_usage",
        lambda _path: DiskUsage(1_000, 800, 200),
    )
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_QUOTA_MODE", "report")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_BYTES", "150")
    monkeypatch.setenv("VECTOR_LAKE_BACKUP_MIN_FREE_RATIO", "0")

    with pytest.raises(backup_capacity.BackupCapacityError) as error:
        backup_capacity.assert_backup_capacity(
            estimated_new_bytes=100,
            operation="unit-test",
            backup_roots=roots,
            disk_anchor=tmp_path,
        )

    assert error.value.reason == "minimum_free_space"
    assert error.value.status["projected_free_bytes"] == 100


def test_incomplete_inventory_fails_closed(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    monkeypatch.setattr(
        backup_capacity,
        "_inventory_tree_bytes",
        lambda _root: (10, 1, False, ["inventory_probe_failed"]),
    )
    monkeypatch.setattr(
        backup_capacity.shutil,
        "disk_usage",
        lambda _path: DiskUsage(10_000, 1_000, 9_000),
    )

    with pytest.raises(backup_capacity.BackupCapacityError) as error:
        backup_capacity.assert_backup_capacity(
            estimated_new_bytes=1,
            operation="unit-test",
            backup_roots=roots,
            disk_anchor=tmp_path,
        )

    assert error.value.reason == "backup_inventory_incomplete"


def test_estimate_adds_headroom_and_one_megabyte_floor(tmp_path, monkeypatch):
    sources = [tmp_path / name for name in ("db", "index", "graph", "manifest")]
    for source in sources:
        source.write_bytes(b"x" * 100)
    monkeypatch.setattr(backup_capacity, "peek_db_path", lambda: sources[0])
    monkeypatch.setattr(backup_capacity, "get_index_path", lambda: sources[1])
    monkeypatch.setattr(backup_capacity, "get_claim_graph_path", lambda: sources[2])
    monkeypatch.setattr(
        backup_capacity,
        "get_projection_manifest_path",
        lambda: sources[3],
    )

    assert backup_capacity.estimate_maintenance_backup_bytes() == 1024 * 1024


def test_legacy_projection_hard_cap_rejects_before_json_read(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    index_path = wiki / "index.json"
    with index_path.open("wb") as handle:
        handle.seek(backup_capacity.LEGACY_PROJECTION_FILE_MAX_BYTES)
        handle.write(b"0")
    monkeypatch.setattr(backup_capacity, "get_wiki_dir", lambda: wiki)

    with pytest.raises(
        ValueError,
        match="legacy_projection_file_too_large:index.json",
    ):
        backup_capacity.projection_v2_reachable_inventory()


def test_maintenance_backup_preflight_runs_before_staging(
    isolated_memory,
    monkeypatch,
):
    backup_root = isolated_memory / "wiki" / ".meta" / "backups"
    before = set(backup_root.iterdir()) if backup_root.exists() else set()
    rejection = backup_capacity.BackupCapacityError(
        "minimum_free_space",
        {"allowed": False},
    )
    monkeypatch.setattr(
        tool_projection,
        "assert_backup_capacity",
        lambda **_kwargs: (_ for _ in ()).throw(rejection),
    )
    monkeypatch.setattr(
        tool_projection,
        "estimate_maintenance_backup_bytes",
        lambda: 123,
    )

    with pytest.raises(backup_capacity.BackupCapacityError):
        tool_projection.create_maintenance_backup("preflight")

    after = set(backup_root.iterdir()) if backup_root.exists() else set()
    assert after == before


def test_sqlite_backup_preflight_runs_before_destination_directory_creation(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    db_store.init_db()
    destination = tmp_path / "not-created" / "backup.db"
    rejection = backup_capacity.BackupCapacityError(
        "max_total_bytes",
        {"allowed": False},
    )
    monkeypatch.setattr(
        backup_capacity,
        "assert_backup_capacity",
        lambda **_kwargs: (_ for _ in ()).throw(rejection),
    )

    with pytest.raises(backup_capacity.BackupCapacityError):
        db_store.backup_database(destination)

    assert destination.parent.exists() is False


def test_default_schema_inventory_follows_overridden_database_parent(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "separate-volume" / "vector_lake.db"
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(database_path))

    roots = backup_capacity._default_backup_roots()

    assert roots == (
        isolated_memory / "wiki" / ".meta" / "backups",
        database_path.parent / "schema-migration-backups",
    )


def test_schema_migration_preflight_uses_target_volume_before_staging(
    isolated_memory,
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "separate-volume" / "vector_lake.db"
    backup_dir = database_path.parent / "schema-migration-backups"
    observed = {}
    rejection = backup_capacity.BackupCapacityError(
        "minimum_free_space",
        {"allowed": False},
    )

    def reject(**kwargs):
        observed.update(kwargs)
        raise rejection

    monkeypatch.setattr(backup_capacity, "assert_backup_capacity", reject)
    monkeypatch.setattr(
        backup_capacity,
        "estimate_database_backup_bytes",
        lambda _path: 123,
    )

    with pytest.raises(backup_capacity.BackupCapacityError):
        db_store._schema_migration_backup(
            None,
            database_path=database_path,
            plan={"pre_schema_version": 7, "fingerprint": "sha256:" + "a" * 64},
        )

    assert observed["estimated_new_bytes"] == 123
    assert observed["disk_anchor"] == backup_dir
    assert observed["backup_roots"] == (
        isolated_memory / "wiki" / ".meta" / "backups",
        backup_dir,
    )
    assert not backup_dir.exists()
