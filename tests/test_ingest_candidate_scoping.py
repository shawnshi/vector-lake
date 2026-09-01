import json
from datetime import datetime, timezone

from vector_lake import db_store, tool_ingest
from vector_lake.mutation_coordinator import execute_mutation_plan
from tests.test_mutation_coordinator import _write_purpose_contract


class _RecordingRows(list):
    def fetchall(self):
        return list(self)


class _RecordingConnection:
    def __init__(self):
        self.calls = []
        self.functions = []

    def create_function(self, name, arity, callback, **options):
        self.functions.append((name, arity, callback, options))

    def execute(self, sql, parameters=()):
        self.calls.append((" ".join(sql.split()), tuple(parameters)))
        return _RecordingRows()


def test_candidate_lookup_chunks_paths_but_scans_source_json_once():
    connection = _RecordingConnection()
    paths = [f"C:/raw/candidate-{index:04d}.md" for index in range(805)]
    identities = [f"raw/candidate-{index:04d}.md" for index in range(805)]

    assert tool_ingest._candidate_processed_files(connection, paths) == {}
    assert tool_ingest._candidate_legacy_ingest_identities(connection, paths) == set()
    assert tool_ingest._candidate_source_entities(connection, identities) == []

    groups = {
        "processed": [
            parameters
            for sql, parameters in connection.calls
            if "FROM processed_files" in sql
        ],
        "legacy": [
            parameters for sql, parameters in connection.calls if "FROM jobs" in sql
        ],
        "sources": [
            parameters
            for sql, parameters in connection.calls
            if "JOIN json_each" in sql
        ],
    }
    assert {
        name: [len(chunk) for chunk in chunks] for name, chunks in groups.items()
    } == {
        "processed": [400, 400, 5],
        "legacy": [400, 400, 5],
        "sources": [0],
    }
    assert all(
        len(parameters) <= 400 for chunks in groups.values() for parameters in chunks
    )
    assert connection.functions[0][0:2] == (
        "vector_lake_source_identity_is_candidate",
        1,
    )
    is_candidate = connection.functions[0][2]
    assert is_candidate("MEMORY/raw/candidate-0001.md") == 1
    assert is_candidate("MEMORY/raw/unrelated.md") == 0
    assert connection.functions[1][0:3] == (
        "vector_lake_source_identity_is_candidate",
        1,
        None,
    )


def test_candidate_prepare_queries_only_related_paths_and_source_identities(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "candidate-scoped.md"
    raw_path.write_text("candidate revision", encoding="utf-8")
    wiki_path = isolated_memory / "wiki" / "Source_Existing-Candidate.MD"
    _write_purpose_contract(isolated_memory)
    raw_stat = raw_path.stat()
    source_page = tool_ingest._auto_source_page(
        {
            "filepath": str(raw_path.resolve()),
            "hash": tool_ingest.calculate_hash(str(raw_path)),
            "canonical_name": wiki_path.name,
            "source_observed_at": datetime.fromtimestamp(
                raw_stat.st_mtime_ns / 1_000_000_000,
                tz=timezone.utc,
            ).isoformat(),
        }
    )
    execute_mutation_plan(wiki_path.name, content=source_page["content"])
    connection = db_store.get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with db_store.transaction():
        connection.execute(
            "INSERT INTO processed_files "
            "(filepath, file_hash, processed_at, observed_mtime_ns, observed_size) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str((isolated_memory / "raw" / "unrelated.md").resolve()),
                "unrelated-hash",
                now,
                0,
                0,
            ),
        )
    unrelated_job = db_store.enqueue_job(
        "ingest",
        {
            "filepath": str((isolated_memory / "raw" / "unrelated-job.md").resolve()),
            "hash": "unrelated-job-hash",
            "canonical_name": "Source_Unrelated-Job.md",
        },
    )
    with db_store.transaction():
        connection.execute(
            "UPDATE jobs SET idempotency_key = NULL WHERE job_id = ?",
            (unrelated_job,),
        )
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "scoped instructions",
    )
    traced_sql = []
    connection.set_trace_callback(traced_sql.append)

    result = json.loads(
        tool_ingest.prepare_ingest_batch(
            batch_size=1,
            candidate_paths=[str(raw_path)],
        )
    )

    connection.set_trace_callback(None)
    normalized_sql = [" ".join(statement.split()) for statement in traced_sql]
    assert result["filepath"] == str(raw_path.resolve())
    assert result["canonical_name"] == wiki_path.name
    assert len(result["source_projection_hash"]) == 64
    assert any(
        "FROM processed_files WHERE filepath IN (" in statement
        for statement in normalized_sql
    )
    assert not any(
        statement
        == (
            "SELECT filepath, file_hash, observed_mtime_ns, observed_size "
            "FROM processed_files"
        )
        for statement in normalized_sql
    )
    legacy_reads = [
        statement
        for statement in normalized_sql
        if "FROM jobs WHERE task_type = 'ingest' AND idempotency_key IS NULL"
        in statement
    ]
    assert legacy_reads
    assert all("$.filepath') END IN (" in statement for statement in legacy_reads)
    assert sum("JOIN json_each" in statement for statement in normalized_sql) == 1
    assert not any(
        statement.startswith("SELECT data_json FROM entities WHERE")
        and "status != 'Merged'" in statement
        and "type = 'source'" in statement
        for statement in normalized_sql
    )


def test_full_scan_keeps_existing_unscoped_inventory_queries(
    isolated_memory,
    monkeypatch,
):
    raw_path = isolated_memory / "raw" / "full-scan.md"
    raw_path.write_text("full scan revision", encoding="utf-8")
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    connection = db_store.get_connection()
    monkeypatch.setattr(tool_ingest, "_load_ingest_config", lambda: {})
    monkeypatch.setattr(
        tool_ingest,
        "_build_ingest_instructions",
        lambda *_args: "full scan instructions",
    )
    traced_sql = []
    connection.set_trace_callback(traced_sql.append)

    tool_ingest.prepare_ingest_batch(batch_size=1, _enqueue_all=True)

    connection.set_trace_callback(None)
    normalized_sql = [" ".join(statement.split()) for statement in traced_sql]
    assert (
        "SELECT filepath, file_hash, observed_mtime_ns, observed_size "
        "FROM processed_files"
    ) in normalized_sql
    legacy_reads = [
        statement
        for statement in normalized_sql
        if "FROM jobs WHERE task_type = 'ingest' AND idempotency_key IS NULL"
        in statement
    ]
    assert legacy_reads
    assert all("$.filepath') END IN (" not in statement for statement in legacy_reads)
    assert any(
        statement.startswith("SELECT data_json FROM entities WHERE")
        and "status != 'Merged'" in statement
        and "type = 'source'" in statement
        for statement in normalized_sql
    )
