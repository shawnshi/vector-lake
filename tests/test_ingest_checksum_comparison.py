import hashlib
import json
import os

import pytest

from tests.test_mutation_coordinator import _write_purpose_contract
from vector_lake import db_store, tool_ingest


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("checksum_unavailable", [False, True])
def test_versioned_sha256_compares_content_after_observation_changes(
    isolated_memory, monkeypatch, changed, observed, checksum_unavailable
):
    _write_purpose_contract(isolated_memory)
    raw = isolated_memory / "raw" / "synthetic.md"
    raw.write_text("Synthetic baseline source content.\n", encoding="utf-8")
    db_store.init_db()
    before = raw.stat()
    digest = "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()
    observation = {"mtime_ns": before.st_mtime_ns, "size": before.st_size} if observed else {}
    db_store.mark_file_processed(str(raw), digest, **observation)
    conn = db_store.get_connection()
    ledger_before = dict(
        conn.execute("SELECT * FROM processed_files WHERE filepath=?", (str(raw),)).fetchone()
    )
    if changed:
        raw.write_text("Synthetic changed source content.\n", encoding="utf-8")
    os.utime(raw, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    monkeypatch.setattr(
        tool_ingest, "_load_scan_config", lambda: {"supported_extensions": [".md"]}
    )

    if checksum_unavailable:
        real_hash = tool_ingest.calculate_hash
        def unavailable_sha256(filepath, *, algorithm="md5"):
            return "" if algorithm == "sha256" else real_hash(filepath)
        monkeypatch.setattr(tool_ingest, "calculate_hash", unavailable_sha256)

    result = tool_ingest.prepare_ingest_batch(batch_size=1)

    jobs = conn.execute("SELECT payload FROM jobs WHERE task_type='ingest'").fetchall()
    assert len(jobs) == int(changed and not checksum_unavailable), result
    if jobs:
        assert json.loads(jobs[0][0])["hash"] == tool_ingest.calculate_hash(str(raw))
    ledger_after = dict(
        conn.execute("SELECT * FROM processed_files WHERE filepath=?", (str(raw),)).fetchone()
    )
    if not observed and not changed and not checksum_unavailable:
        assert ledger_after["file_hash"] == digest
        assert ledger_after["observed_mtime_ns"] == raw.stat().st_mtime_ns
        assert ledger_after["observed_size"] == raw.stat().st_size
    else:
        assert ledger_after == ledger_before
