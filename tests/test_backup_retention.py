"""Backup retention: what it plans, what it refuses to touch, what it removes."""

import os
from pathlib import Path

import pytest

from vector_lake import backup_retention

GiB = 1024**3


def _database_backup(root: Path, name: str, size: int, mtime: float, sidecars=True) -> Path:
    path = root / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    if sidecars:
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            sidecar.write_bytes(b"")
            os.utime(sidecar, (mtime, mtime))
    return path


def _directory_backup(root: Path, name: str, size: int, mtime: float) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / "vector_lake.db").write_bytes(b"x" * size)
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    os.utime(directory, (mtime, mtime))
    return directory


@pytest.fixture
def backup_root(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    return root


def test_missing_root_is_not_an_error(tmp_path):
    plan = backup_retention.plan_backup_retention(tmp_path / "nope", keep_count=3)

    assert plan["entry_count"] == 0
    assert plan["remove"] == []
    assert plan["total_bytes"] == 0


def test_under_the_bound_removes_nothing(backup_root):
    _database_backup(backup_root, "vector_lake_1.db.bak", 1000, 100.0)
    _database_backup(backup_root, "vector_lake_2.db.bak", 1000, 200.0)

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=3, max_bytes=GiB)

    assert plan["remove"] == []
    assert plan["entry_count"] == 2
    assert [entry["name"] for entry in plan["keep"]] == [
        "vector_lake_2.db.bak",
        "vector_lake_1.db.bak",
    ]


def test_keep_count_drops_the_oldest_entries(backup_root):
    for index in range(4):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 10, float(index))

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=2, max_bytes=0)

    assert [entry["name"] for entry in plan["keep"]] == [
        "vector_lake_3.db.bak",
        "vector_lake_2.db.bak",
    ]
    assert sorted(entry["name"] for entry in plan["remove"]) == [
        "vector_lake_0.db.bak",
        "vector_lake_1.db.bak",
    ]


def test_byte_cap_drops_the_entries_that_would_cross_it(backup_root):
    _database_backup(backup_root, "vector_lake_3.db.bak", 100, 300.0)
    _database_backup(backup_root, "vector_lake_2.db.bak", 100, 200.0)
    _database_backup(backup_root, "vector_lake_1.db.bak", 100, 100.0)

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=1, max_bytes=100)

    # keep_count=1 is unconditional, so the cap is measured against its size only.
    assert [entry["name"] for entry in plan["keep"]] == ["vector_lake_3.db.bak"]
    assert [entry["name"] for entry in plan["remove"]] == [
        "vector_lake_2.db.bak",
        "vector_lake_1.db.bak",
    ]
    assert plan["removable_bytes"] == 200


def test_the_newest_entry_is_never_removable(backup_root):
    _database_backup(backup_root, "vector_lake_1.db.bak", 10_000, 100.0)
    _database_backup(backup_root, "vector_lake_2.db.bak", 10_000, 200.0)

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=0, max_bytes=0)

    assert [entry["name"] for entry in plan["keep"]] == ["vector_lake_2.db.bak"]


def test_sidecars_travel_with_their_database_unit(backup_root):
    _database_backup(backup_root, "vector_lake_1.db.bak", 10, 100.0)
    _database_backup(backup_root, "vector_lake_2.db.bak", 10, 200.0)

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=1, max_bytes=0)

    assert plan["remove"][0]["members"] == [
        "vector_lake_1.db.bak",
        "vector_lake_1.db.bak-wal",
        "vector_lake_1.db.bak-shm",
    ]


def test_unrecognized_files_are_reported_and_never_planned_for_removal(backup_root):
    (backup_root / "notes.txt").write_text("keep me", encoding="utf-8")
    _database_backup(backup_root, "vector_lake_1.db.bak", 10, 100.0)

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=1, max_bytes=0)

    assert plan["unrecognized"] == ["notes.txt"]
    assert plan["remove"] == []
    assert (backup_root / "notes.txt").exists()


def test_maintenance_directories_are_units_too(backup_root):
    _directory_backup(backup_root, "maintenance_20260916T000000Z", 500, 100.0)
    _directory_backup(backup_root, "maintenance_20260917T000000Z", 500, 200.0)

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=1, max_bytes=0)

    assert plan["remove"][0]["kind"] == "directory"
    assert plan["remove"][0]["name"] == "maintenance_20260916T000000Z"


def test_dry_run_removes_nothing(backup_root):
    for index in range(3):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 10, float(index))

    result = backup_retention.prune_backups(backup_root, keep_count=1, max_bytes=0, dry_run=True)

    assert result["dry_run"] is True
    assert result["deleted"] == []
    assert len(list(backup_root.glob("*.db.bak"))) == 3


def test_prune_removes_exactly_the_planned_entries(backup_root):
    for index in range(3):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 10, float(index))

    result = backup_retention.prune_backups(backup_root, keep_count=1, max_bytes=0, dry_run=False)

    assert result["deleted"] == ["vector_lake_1.db.bak", "vector_lake_0.db.bak"]
    assert result["failures"] == []
    assert [path.name for path in sorted(backup_root.iterdir())] == [
        "vector_lake_2.db.bak",
        "vector_lake_2.db.bak-shm",
        "vector_lake_2.db.bak-wal",
    ]


def test_prune_removes_a_directory_unit_recursively(backup_root):
    _directory_backup(backup_root, "maintenance_old", 10, 100.0)
    _directory_backup(backup_root, "maintenance_new", 10, 200.0)

    result = backup_retention.prune_backups(backup_root, keep_count=1, max_bytes=0, dry_run=False)

    assert result["deleted"] == ["maintenance_old"]
    assert not (backup_root / "maintenance_old").exists()
    assert (backup_root / "maintenance_new" / "manifest.json").exists()


def test_a_symlinked_entry_is_refused_rather_than_followed(tmp_path, backup_root):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete", encoding="utf-8")
    link = backup_root / "vector_lake_1.db.bak"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this host")

    result = backup_retention.prune_backups(backup_root, keep_count=1, max_bytes=0, dry_run=False)

    assert result["remove"] == []
    assert result["unrecognized"] == ["vector_lake_1.db.bak"]
    assert (outside / "precious.txt").exists()


def test_a_directory_outside_the_root_is_refused(tmp_path, backup_root, monkeypatch):
    """Defence in depth: a doctored plan must not redirect a recursive delete."""
    _database_backup(backup_root, "vector_lake_1.db.bak", 10, 100.0)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "data.bin").write_bytes(b"x")

    real_plan = backup_retention._plan

    def doctored(root, keep_count, max_bytes):
        plan = real_plan(root, keep_count, max_bytes)
        plan["remove"] = [
            backup_retention.BackupEntry((outside,), "directory", 1, 0.0)
        ]
        return plan

    monkeypatch.setattr(backup_retention, "_plan", doctored)

    result = backup_retention.prune_backups(backup_root, keep_count=1, max_bytes=0, dry_run=False)

    assert result["deleted"] == []
    assert result["failures"] and "refused" in result["failures"][0]
    assert (outside / "data.bin").exists()


def test_the_live_bound_deletes_nothing(backup_root):
    """The shipped default must be a guard, not a purge."""
    for index in range(3):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 1024, float(index))

    plan = backup_retention.plan_backup_retention(backup_root)

    assert plan["keep_count"] == backup_retention.DEFAULT_KEEP_COUNT
    assert plan["max_bytes"] == backup_retention.DEFAULT_MAX_BYTES
    assert plan["remove"] == []


def test_bounds_come_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv(backup_retention.KEEP_COUNT_ENV, "5")
    monkeypatch.setenv(backup_retention.MAX_BYTES_ENV, "2048")
    assert backup_retention.resolve_bounds() == (5, 2048)


def test_an_unparsable_bound_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(backup_retention.KEEP_COUNT_ENV, "many")
    monkeypatch.delenv(backup_retention.MAX_BYTES_ENV, raising=False)

    assert backup_retention.resolve_bounds() == (
        backup_retention.DEFAULT_KEEP_COUNT,
        backup_retention.DEFAULT_MAX_BYTES,
    )


def test_the_byte_ceiling_can_go_below_keep_count_but_never_to_zero(backup_root):
    for index in range(3):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 100, float(index))

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=3, max_bytes=100)

    assert [entry["name"] for entry in plan["keep"]] == ["vector_lake_2.db.bak"]
    assert sorted(entry["name"] for entry in plan["remove"]) == [
        "vector_lake_0.db.bak",
        "vector_lake_1.db.bak",
    ]


def test_a_byte_ceiling_below_one_copy_still_keeps_the_newest(backup_root):
    for index in range(3):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 1000, float(index))

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=3, max_bytes=1)

    assert [entry["name"] for entry in plan["keep"]] == ["vector_lake_2.db.bak"]
    assert len(plan["remove"]) == 2


def test_a_non_positive_byte_ceiling_leaves_keep_count_alone(backup_root):
    for index in range(3):
        _database_backup(backup_root, f"vector_lake_{index}.db.bak", 10_000, float(index))

    plan = backup_retention.plan_backup_retention(backup_root, keep_count=3, max_bytes=0)

    assert len(plan["keep"]) == 3
    assert plan["remove"] == []
