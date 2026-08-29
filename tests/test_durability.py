from __future__ import annotations

import os

import pytest

from vector_lake import durability
from vector_lake import db_store
from vector_lake.wiki_utils import atomic_write_text


def test_durability_profile_is_full_by_default_and_invalid_values_fail_closed(
    monkeypatch,
):
    monkeypatch.delenv("VECTOR_LAKE_DURABILITY_PROFILE", raising=False)
    assert durability.durability_profile() == "full"
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "unknown")
    with pytest.raises(RuntimeError, match="must be 'full' or 'best_effort'"):
        durability.durability_profile()


def test_durable_replace_orders_source_move_target_and_directory_barriers(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.tmp"
    target = tmp_path / "target.txt"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    calls = []
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "full")
    monkeypatch.setattr(
        durability,
        "sync_file",
        lambda path: calls.append(("file", os.fspath(path))),
    )
    monkeypatch.setattr(
        durability,
        "sync_directory",
        lambda path: calls.append(("directory", os.fspath(path))),
    )
    monkeypatch.setattr(
        durability,
        "_windows_replace_file_write_through",
        lambda left, right: (
            calls.append(("replace", os.fspath(left), os.fspath(right))),
            os.replace(left, right),
        )[-1],
    )

    durability.durable_replace_file(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert calls == [
        ("file", os.fspath(source)),
        ("replace", os.fspath(source), os.fspath(target)),
        ("file", os.fspath(target)),
        ("directory", os.fspath(tmp_path)),
    ]


def test_atomic_write_does_not_ack_when_write_through_replace_fails(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "state.txt"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "full")
    monkeypatch.setattr(durability, "sync_file", lambda _path: None)
    monkeypatch.setattr(durability, "sync_directory", lambda _path: None)
    monkeypatch.setattr(
        durability,
        "_windows_replace_file_write_through",
        lambda _source, _target: (_ for _ in ()).throw(OSError("flush failed")),
    )

    with pytest.raises(OSError, match="flush failed"):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through acceptance")
def test_windows_full_durability_replace_and_directory_barrier(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    target = tmp_path / "target.txt"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "full")

    durability.durable_replace_file(source, target)

    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "new"


def test_sqlite_connections_apply_the_selected_durability_profile(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "full")
    db_store.close_all_connections()
    db_store.init_db()
    assert int(db_store.get_connection().execute("PRAGMA synchronous").fetchone()[0]) == 2

    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "best_effort")
    assert int(db_store.get_connection().execute("PRAGMA synchronous").fetchone()[0]) == 1
