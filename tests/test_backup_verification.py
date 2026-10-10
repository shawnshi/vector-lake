import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from vector_lake import backup_retention, db_store
from vector_lake.tool_projection import create_maintenance_backup


def test_unfinished_and_unknown_directories_are_never_removed(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    for name in ["known-old", "unfinished-new", "operator-notes"]:
        directory = root / name
        directory.mkdir()
        (directory / "data.txt").write_text("synthetic", encoding="utf-8")
    result = backup_retention.prune_backups(root, keep_count=1, max_bytes=1, dry_run=False)
    assert result["deleted"] == []
    assert all((root / name).is_dir() for name in ["known-old", "unfinished-new", "operator-notes"])


def test_old_database_without_receipt_is_not_a_deletion_candidate(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    for index in range(3):
        with sqlite3.connect(root / f"vector_lake_{index}.db.bak") as connection:
            connection.execute("CREATE TABLE synthetic (id INTEGER)")
    result = backup_retention.prune_backups(root, keep_count=1, dry_run=False)
    assert result["deleted"] == []
    assert len(list(root.glob("*.db.bak"))) == 3


def _backup(isolated_memory):
    db_store.init_db()
    return Path(db_store.backup_database())


def test_database_producer_seals_only_integrity_checked_snapshot(isolated_memory):
    backup = _backup(isolated_memory)
    result = backup_retention.plan_backup_retention(backup.parent)
    assert [item["name"] for item in result["keep"]] == [backup.name]
    receipt = json.loads(backup.with_name(backup.name + ".verified.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "complete"
    assert receipt["sqlite_integrity"] == "ok"
    assert receipt["files"][0]["name"] == backup.name
    assert len(receipt["files"][0]["sha256"]) == 64
    with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_modified_newest_backup_cannot_displace_last_verified_copy(isolated_memory):
    old = _backup(isolated_memory)
    new = _backup(isolated_memory)
    original_stat = new.stat()
    data = bytearray(new.read_bytes())
    data[0] ^= 1
    new.write_bytes(data)
    os.utime(new, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    result = backup_retention.prune_backups(new.parent, keep_count=1, max_bytes=1, dry_run=False)
    assert result["deleted"] == []
    assert [item["name"] for item in result["keep"]] == [old.name]
    assert any(item["name"] == new.name for item in result["protected"])
    assert old.exists() and new.exists()


def test_maintenance_producer_publishes_a_verified_directory(isolated_memory):
    db_store.init_db()
    directory = Path(create_maintenance_backup("synthetic"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "complete"
    assert manifest["sqlite_integrity"] == "ok"
    result = backup_retention.plan_backup_retention(directory.parent)
    assert [item["name"] for item in result["keep"]] == [directory.name]
    assert not list(directory.parent.glob("*.partial"))


def test_extra_file_protects_previously_verified_directory(isolated_memory):
    db_store.init_db()
    old = Path(create_maintenance_backup("old"))
    new = Path(create_maintenance_backup("new"))
    (old / "operator-notes.txt").write_text("do not delete", encoding="utf-8")
    result = backup_retention.prune_backups(old.parent, keep_count=1, max_bytes=1, dry_run=False)
    assert result["deleted"] == []
    assert any(item["name"] == old.name for item in result["protected"])
    assert old.exists() and new.exists()


def test_failed_integrity_check_never_publishes_verified_backup(isolated_memory, monkeypatch):
    db_store.init_db()
    original_connection = db_store.get_connection()

    class CorruptBackup:
        def backup(self, destination):
            destination.execute("PRAGMA ignore_check_constraints=ON")
            destination.execute("CREATE TABLE invalid (v INTEGER CHECK(v>0))")
            destination.execute("INSERT INTO invalid VALUES (-1)")
            destination.commit()
            destination.execute("PRAGMA ignore_check_constraints=OFF")

    monkeypatch.setattr(db_store, "get_connection", lambda: CorruptBackup())
    with pytest.raises(RuntimeError, match="integrity check failed"):
        db_store.backup_database()
    root = isolated_memory / "wiki" / ".meta" / "backups"
    assert not list(root.glob("*.db.bak"))
    assert not list(root.glob("*.verified.json"))
    assert original_connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


@pytest.mark.parametrize("remove_retained", [False, True])
def test_retained_snapshot_is_rechecked_before_deletion(isolated_memory, monkeypatch, remove_retained):
    old = _backup(isolated_memory)
    new = _backup(isolated_memory)
    real_plan = backup_retention._plan

    def race(*args):
        plan = real_plan(*args)
        if remove_retained:
            new.unlink()
        else:
            new.write_bytes(b"changed after planning")
        return plan

    monkeypatch.setattr(backup_retention, "_plan", race)
    result = backup_retention.prune_backups(old.parent, keep_count=1, dry_run=False)
    assert result["deleted"] == []
    assert old.exists()
    assert any("no verified retained backup" in failure for failure in result["failures"])


def test_new_unknown_member_is_not_recursively_deleted(isolated_memory, monkeypatch):
    db_store.init_db()
    old = Path(create_maintenance_backup("old"))
    Path(create_maintenance_backup("new"))
    real_verify = backup_retention._verified_members
    calls = []

    def race(path, deadline):
        members = real_verify(path, deadline)
        if path == old:
            calls.append(1)
            if len(calls) == 2:
                (old / "operator-notes.txt").write_text("preserve", encoding="utf-8")
        return members

    monkeypatch.setattr(backup_retention, "_verified_members", race)
    result = backup_retention.prune_backups(old.parent, keep_count=1, dry_run=False)
    assert (old / "operator-notes.txt").read_text(encoding="utf-8") == "preserve"
    assert old.exists()
    assert result["deleted"] == [] and result["failures"]


def test_database_member_is_not_inferred_from_extension(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(isolated_memory / "canonical.sqlite3"))
    db_store.init_db()
    old = Path(create_maintenance_backup("old"))
    new = Path(create_maintenance_backup("new"))
    result = backup_retention.prune_backups(old.parent, keep_count=1, dry_run=False)
    assert result["deleted"] == [old.name]
    assert new.exists() and not old.exists()


def test_nonregular_receipt_descriptor_is_rejected_after_open(tmp_path, monkeypatch):
    root = tmp_path / "backups"
    directory = root / "unknown"
    directory.mkdir(parents=True)
    receipt = directory / "manifest.json"
    receipt.write_text("{}", encoding="utf-8")
    reader, writer = os.pipe()
    real_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(receipt):
            return os.dup(reader)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced_open)
    try:
        result = backup_retention.plan_backup_retention(root)
        assert result["entry_count"] == 0
        assert result["protected"]
    finally:
        os.close(reader)
        os.close(writer)


def test_verification_budget_is_shared_across_the_whole_prune(isolated_memory, monkeypatch):
    old = _backup(isolated_memory)
    _backup(isolated_memory)
    clock = [0.0]
    monkeypatch.setattr(backup_retention, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    real_plan = backup_retention._plan

    def race(*args):
        plan = real_plan(*args)
        clock[0] = backup_retention.VERIFICATION_SECONDS + 1
        return plan

    monkeypatch.setattr(backup_retention, "_plan", race)
    result = backup_retention.prune_backups(old.parent, keep_count=1, dry_run=False)
    assert result["deleted"] == []
    assert old.exists()
    assert result["failures"]
