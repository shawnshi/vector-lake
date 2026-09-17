"""Durability of the temp-file-then-rename write paths.

``os.replace`` makes a new file visible atomically, but it only swaps a directory
entry.  Without an ``fsync`` the replacement inode's data can still be in the page
cache at that moment, so a crash can leave a zero-length or torn canonical file
while the previous content is already unlinked.  These tests pin the ordering:
the bytes reach stable storage *before* the entry is swapped.
"""

import json
import os
from pathlib import Path

from vector_lake import indexer, wiki_utils


def test_flush_durable_forces_the_handle_to_storage(tmp_path, monkeypatch):
    synced_fds = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced_fds.append(fd))

    with open(tmp_path / "x.txt", "w", encoding="utf-8") as handle:
        handle.write("data")
        wiki_utils.flush_durable(handle)

    assert len(synced_fds) == 1, "flush_durable did not call os.fsync exactly once"


def test_fsync_directory_is_a_noop_on_windows(tmp_path):
    # Windows cannot open a directory for fsync; the call must not raise there.
    wiki_utils.fsync_directory(tmp_path)


def test_atomic_write_text_syncs_the_temp_file_before_publishing(tmp_path, monkeypatch):
    target = tmp_path / "Concept_Sync.md"
    target.write_text("old", encoding="utf-8")

    observed = []
    real_flush_durable = wiki_utils.flush_durable

    def recording_flush(handle):
        # At this instant the rename must not have happened yet.  Flush the text
        # buffer first so the read reflects what the fsync is about to persist.
        handle.flush()
        observed.append(
            (
                handle.name,
                target.read_text(encoding="utf-8"),
                Path(handle.name).read_text(encoding="utf-8"),
            )
        )
        real_flush_durable(handle)

    monkeypatch.setattr(wiki_utils, "flush_durable", recording_flush)
    wiki_utils.atomic_write_text(target, "new content")

    assert len(observed) == 1
    temp_name, destination_at_flush, temp_at_flush = observed[0]
    assert temp_name.endswith(".tmp")
    assert destination_at_flush == "old", "the destination was replaced before the temp file was synced"
    assert temp_at_flush == "new content"
    assert target.read_text(encoding="utf-8") == "new content"
    assert not Path(temp_name).exists(), "the temp file was left behind"


def test_atomic_write_text_syncs_the_containing_directory(tmp_path, monkeypatch):
    synced_directories = []
    monkeypatch.setattr(wiki_utils, "fsync_directory", synced_directories.append)

    wiki_utils.atomic_write_text(tmp_path / "Concept_Dir.md", "body")

    assert synced_directories == [tmp_path]


def test_fsync_directory_swallows_an_unopenable_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("no dir fd")))
    # Must not raise: the data file itself is already synced.
    wiki_utils.fsync_directory(tmp_path)


def test_index_stage_write_is_synced_before_publish(tmp_path, monkeypatch):
    observed = []
    real_flush_durable = indexer.flush_durable

    def recording_flush(handle):
        handle.flush()
        observed.append(Path(handle.name).read_text(encoding="utf-8"))
        real_flush_durable(handle)

    monkeypatch.setattr(indexer, "flush_durable", recording_flush)
    stage = tmp_path / "index.json.tmp"
    indexer._write_json_stage(str(stage), {"nodes": {}})

    assert observed == ['{"nodes":{}}'], "the staged projection was not synced before publish"
    assert json.loads(stage.read_text(encoding="utf-8")) == {"nodes": {}}


def test_write_json_payload_syncs_the_directory_after_the_swap(tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(indexer, "fsync_directory", published.append)

    output = tmp_path / "index.json"
    indexer._write_json_payload(str(output), {"nodes": {"a": {}}})

    assert published == [str(tmp_path)]
    assert json.loads(output.read_text(encoding="utf-8")) == {"nodes": {"a": {}}}
