import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vector_lake import db_store, raw_revision, tool_ingest


def _configure_isolated_scan(monkeypatch) -> None:
    monkeypatch.setattr(tool_ingest, "_load_ingest_config", lambda: {})
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "isolated raw inventory instructions",
    )
    monkeypatch.setattr(
        tool_ingest,
        "_projection_hash_for_canonical_version",
        lambda *_args: "a" * 64,
    )


def _seed_canonical_processed_file(raw_path: Path):
    snapshot = raw_revision.stable_raw_revision(
        raw_path,
        include_legacy_md5=False,
    )
    db_store.init_db()
    db_store.mark_file_processed(
        str(raw_path.resolve()),
        snapshot.canonical_revision,
        observed_mtime_ns=snapshot.observed_mtime_ns,
        observed_size=snapshot.observed_size,
    )
    return snapshot


def _count_raw_bytes(monkeypatch):
    reads = {}
    real_read = raw_revision._read_raw_chunk

    def counted_read(handle):
        chunk = real_read(handle)
        if chunk:
            path = str(Path(handle.name).resolve())
            reads[path] = reads.get(path, 0) + len(chunk)
        return chunk

    monkeypatch.setattr(raw_revision, "_read_raw_chunk", counted_read)
    return reads


def test_unchanged_full_inventory_reads_metadata_only(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "unchanged.txt"
    raw_path.write_bytes(b"unchanged bytes" * 1024)
    snapshot = _seed_canonical_processed_file(raw_path)
    _configure_isolated_scan(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "0")
    reads = _count_raw_bytes(monkeypatch)
    connection = db_store.get_connection()
    changes_before = connection.total_changes

    result = tool_ingest.prepare_ingest_batch(batch_size=1, _enqueue_all=True)

    assert result == (
        f"{tool_ingest.FULL_SCAN_COMPLETE_TOKEN}\n"
        f"{tool_ingest.NO_NEW_REVISIONS_MESSAGE}"
    )
    assert reads.get(str(raw_path.resolve()), 0) == 0
    assert connection.total_changes == changes_before
    marker = connection.execute(
        "SELECT file_hash, observed_mtime_ns, observed_size "
        "FROM processed_files WHERE filepath = ?",
        (str(raw_path.resolve()),),
    ).fetchone()
    assert tuple(marker) == (
        snapshot.canonical_revision,
        snapshot.observed_mtime_ns,
        snapshot.observed_size,
    )


def test_full_inventory_metadata_change_reads_current_bytes(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "metadata-change.txt"
    raw_path.write_bytes(b"before")
    _seed_canonical_processed_file(raw_path)
    current_bytes = b"after-with-a-different-size"
    raw_path.write_bytes(current_bytes)
    _configure_isolated_scan(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "0")
    reads = _count_raw_bytes(monkeypatch)

    result = tool_ingest.prepare_ingest_batch(batch_size=1, _enqueue_all=True)

    assert result.startswith(tool_ingest.FULL_SCAN_COMPLETE_TOKEN)
    assert reads[str(raw_path.resolve())] == len(current_bytes)
    payload = json.loads(
        db_store.get_connection()
        .execute("SELECT payload FROM jobs WHERE task_type = 'ingest'")
        .fetchone()["payload"]
    )
    assert payload["hash"] == "sha256:" + hashlib.sha256(current_bytes).hexdigest()


def test_explicit_candidate_always_reads_unchanged_file(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "candidate.txt"
    raw_bytes = b"explicit candidate bytes"
    raw_path.write_bytes(raw_bytes)
    _seed_canonical_processed_file(raw_path)
    _configure_isolated_scan(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "0")
    reads = _count_raw_bytes(monkeypatch)

    result = tool_ingest.prepare_ingest_batch(
        batch_size=1,
        candidate_paths=[str(raw_path)],
    )

    assert result == tool_ingest.NO_NEW_REVISIONS_MESSAGE
    assert reads[str(raw_path.resolve())] == len(raw_bytes)


def test_scrub_reads_and_detects_same_size_same_mtime_tamper(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "scrub-tamper.txt"
    original_bytes = b"version-one"
    tampered_bytes = b"version-two"
    assert len(original_bytes) == len(tampered_bytes)
    raw_path.write_bytes(original_bytes)
    original_snapshot = _seed_canonical_processed_file(raw_path)
    original_stat = raw_path.stat()
    raw_path.write_bytes(tampered_bytes)
    os.utime(
        raw_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    _configure_isolated_scan(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "1")
    reads = _count_raw_bytes(monkeypatch)

    result = tool_ingest.prepare_ingest_batch(batch_size=1, _enqueue_all=True)

    assert result.startswith(tool_ingest.FULL_SCAN_COMPLETE_TOKEN)
    assert reads[str(raw_path.resolve())] == len(tampered_bytes)
    payload = json.loads(
        db_store.get_connection()
        .execute("SELECT payload FROM jobs WHERE task_type = 'ingest'")
        .fetchone()["payload"]
    )
    assert payload["hash"] != original_snapshot.canonical_revision
    assert payload["hash"] == (
        "sha256:" + hashlib.sha256(tampered_bytes).hexdigest()
    )


def test_legacy_md5_full_inventory_reads_once_and_enqueues_canonical_revision(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "legacy.txt"
    raw_bytes = b"legacy marker bytes" * 1024
    raw_path.write_bytes(raw_bytes)
    snapshot = raw_revision.stable_raw_revision(raw_path)
    assert snapshot.legacy_md5 is not None
    db_store.init_db()
    with db_store.transaction() as connection:
        connection.execute(
            "INSERT INTO processed_files "
            "(filepath, file_hash, processed_at, observed_mtime_ns, observed_size) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(raw_path.resolve()),
                snapshot.legacy_md5,
                datetime.now(timezone.utc).isoformat(),
                snapshot.observed_mtime_ns,
                snapshot.observed_size,
            ),
        )
    _configure_isolated_scan(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "0")
    reads = _count_raw_bytes(monkeypatch)

    result = tool_ingest.prepare_ingest_batch(batch_size=1, _enqueue_all=True)

    assert result.startswith(tool_ingest.FULL_SCAN_COMPLETE_TOKEN)
    assert reads[str(raw_path.resolve())] == len(raw_bytes)
    connection = db_store.get_connection()
    payload = json.loads(
        connection.execute(
            "SELECT payload FROM jobs WHERE task_type = 'ingest'"
        ).fetchone()["payload"]
    )
    assert payload["hash"] == snapshot.canonical_revision
    marker = connection.execute(
        "SELECT file_hash FROM processed_files WHERE filepath = ?",
        (str(raw_path.resolve()),),
    ).fetchone()["file_hash"]
    assert marker == snapshot.legacy_md5


def test_scrub_bucket_is_deterministic_once_per_period(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "7")
    filepath = "C:/isolated/raw/deterministic.txt"
    start_day = 750_000

    due_days = [
        day
        for day in range(start_day, start_day + 7)
        if tool_ingest._raw_inventory_scrub_due(filepath, day_ordinal=day)
    ]

    assert len(due_days) == 1
    assert tool_ingest._raw_inventory_scrub_due(
        filepath,
        day_ordinal=due_days[0],
    )
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "0")
    assert not tool_ingest._raw_inventory_scrub_due(
        filepath,
        day_ordinal=due_days[0],
    )


def test_eight_day_fake_clock_detects_missed_same_stat_event_within_seven_days(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.raw_scrub_contract import bind_raw_scrub_day

    raw_path = isolated_memory / "raw" / "missed-event.txt"
    original_bytes = b"stable-before"
    tampered_bytes = b"stable-after!"
    assert len(original_bytes) == len(tampered_bytes)
    raw_path.write_bytes(original_bytes)
    original_snapshot = _seed_canonical_processed_file(raw_path)
    original_stat = raw_path.stat()
    raw_path.write_bytes(tampered_bytes)
    os.utime(
        raw_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    _configure_isolated_scan(monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RAW_FULL_SCAN_SCRUB_DAYS", "7")
    reads = _count_raw_bytes(monkeypatch)
    start_day = 750_000
    detected_offset = None

    for offset in range(8):
        with bind_raw_scrub_day(start_day + offset):
            tool_ingest.prepare_ingest_batch(batch_size=1, _enqueue_all=True)
        row = db_store.get_connection().execute(
            "SELECT payload FROM jobs WHERE task_type = 'ingest' LIMIT 1"
        ).fetchone()
        if row is not None:
            detected_offset = offset
            payload = json.loads(row["payload"])
            break

    assert detected_offset is not None
    assert detected_offset <= 6
    assert reads[str(raw_path.resolve())] == len(tampered_bytes)
    assert payload["hash"] != original_snapshot.canonical_revision
    assert payload["hash"] == "sha256:" + hashlib.sha256(tampered_bytes).hexdigest()


def test_stable_raw_metadata_rejects_reparse_escape(isolated_memory):
    raw_root = isolated_memory / "raw"
    outside = isolated_memory / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    linked = raw_root / "linked-outside.txt"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(raw_revision.RawSourceContainmentError):
        raw_revision.stable_raw_metadata(linked, allowed_roots=[raw_root])


def test_stable_revision_still_rejects_content_change_during_read(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "unstable.txt"
    original_bytes = b"A" * 4096
    raw_path.write_bytes(original_bytes)
    real_read = raw_revision._read_raw_chunk
    changed = False
    write_blocked = False

    def mutate_after_read(handle):
        nonlocal changed, write_blocked
        chunk = real_read(handle)
        if chunk and not changed:
            changed = True
            try:
                raw_path.write_bytes(b"B" * len(original_bytes))
            except PermissionError:
                write_blocked = True
            else:
                details = raw_path.stat()
                os.utime(
                    raw_path,
                    ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000_000),
                )
        return chunk

    monkeypatch.setattr(raw_revision, "_read_raw_chunk", mutate_after_read)

    if os.name == "nt":
        snapshot = raw_revision.stable_raw_revision(raw_path)
        assert write_blocked
        assert raw_path.read_bytes() == original_bytes
        assert snapshot.canonical_revision == (
            "sha256:" + hashlib.sha256(original_bytes).hexdigest()
        )
    else:
        with pytest.raises(raw_revision.RawSourceUnstableError):
            raw_revision.stable_raw_revision(raw_path)
        assert not write_blocked


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing contract")
def test_windows_stable_raw_handle_allows_readers_and_denies_writers(
    isolated_memory,
):
    raw_path = isolated_memory / "raw" / "shared-read.txt"
    original_bytes = b"stable shared read"
    replacement_bytes = b"replacement bytes!"
    replacement_path = raw_path.with_name("replacement.txt")
    assert len(replacement_bytes) == len(original_bytes)
    raw_path.write_bytes(original_bytes)
    replacement_path.write_bytes(replacement_bytes)

    with raw_revision._open_stable_raw_handle(raw_path) as first:
        with raw_revision._open_stable_raw_handle(raw_path) as second:
            assert first.read() == original_bytes
            assert second.read() == original_bytes
            with pytest.raises(OSError):
                raw_path.write_bytes(replacement_bytes)
            with pytest.raises(OSError):
                raw_path.unlink()
            with pytest.raises(OSError):
                os.replace(replacement_path, raw_path)
            assert raw_path.read_bytes() == original_bytes

    os.replace(replacement_path, raw_path)
    assert raw_path.read_bytes() == replacement_bytes

    with raw_path.open("r+b"):
        with pytest.raises(raw_revision.RawSourceUnstableError):
            raw_revision._open_stable_raw_handle(raw_path)
