import hashlib
import json
import re
import sqlite3

import pytest

from vector_lake import db_store, tool_storage_baseline
from vector_lake.tool_storage_baseline import inspect_storage_baseline


def _fts_ddl(tokenizer: str = "unicode61 remove_diacritics 1") -> str:
    return (
        "CREATE VIRTUAL TABLE wiki_search_index USING fts5("
        "node_key, title, summary, text, "
        f"tokenize='{tokenizer}')"
    )


def _vec_ddl(dimension: int = 3) -> str:
    return (
        "CREATE VIRTUAL TABLE vec_embeddings USING vec0("
        "entity_id TEXT PRIMARY KEY, "
        f"embedding float[{dimension}])"
    )


def _connection(
    *,
    dimension: int = 3,
    tokenizer: str = "unicode61 remove_diacritics 1",
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    db_store._load_sqlite_vec_extension(connection)
    connection.execute(_fts_ddl(tokenizer))
    connection.execute(_vec_ddl(dimension))
    connection.execute("BEGIN")
    return connection


def _insert_vector(connection: sqlite3.Connection, entity_id: str, values) -> bytes:
    blob = db_store.serialize_float32_vector(values)
    connection.execute(
        "INSERT INTO vec_embeddings(entity_id, embedding) VALUES (?, ?)",
        (entity_id, blob),
    )
    return blob


def _metadata(*entity_ids: str, dimension: int = 3):
    return {
        entity_id: {
            "model": "test-embedding-model",
            "content_hash": "a" * 64,
            "content_recipe": "title+aliases+summary+raw_text:v1",
            "dimension": dimension,
        }
        for entity_id in entity_ids
    }


def _tokenizer_metadata():
    return {
        "engine": "fts5-unicode61",
        "package_version": sqlite3.sqlite_version,
        "segmentation_recipe": {
            "tokenize": "unicode61 remove_diacritics 1",
            "pretokenizer": None,
        },
    }


def _projection_authority_args(connection, expected_nodes=(), expected_fts_rows=()):
    rows = tool_storage_baseline._normalize_expected_fts_rows(expected_fts_rows)
    corpus_digest = hashlib.sha256()
    for row in rows:
        corpus_digest.update(tool_storage_baseline._stable_row_bytes(row))
    snapshot = {
        surface: 1
        for surface in tool_storage_baseline._CANONICAL_PROJECTION_SURFACES
    }
    connection.execute(
        "CREATE TABLE IF NOT EXISTS runtime_generations ("
        "surface TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
    )
    connection.executemany(
        "INSERT OR REPLACE INTO runtime_generations(surface, generation) VALUES (?, ?)",
        sorted(snapshot.items()),
    )
    canonical_token = tool_storage_baseline._canonical_generation_token(snapshot)
    manifest = {
        "contract": "index-claim-graph-pair",
        "version": 1,
        "generation": "1" * 32,
        "published_at": "2026-08-02T00:00:00+00:00",
        "canonical_generation": {
            "status": "verified",
            "algorithm": "runtime-generations-sha256-v2",
            "token": canonical_token,
            "runtime_generations": snapshot,
        },
    }
    nodes = {
        node_key: {
            "title": title,
            "summary": summary,
            "raw_text": text,
            "aliases": [],
        }
        for node_key, title, summary, text in rows
    }
    assert set(nodes) == set(expected_nodes)
    index_payload = json.dumps(
        {
            "nodes": nodes,
            "weighted_edges": [],
            "projection_manifest": manifest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    graph_payload = json.dumps(
        {
            "nodes": [],
            "edges": [],
            "projection_manifest": manifest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sidecar_payload = json.dumps(
        {
            "contract": "index-claim-graph-sidecar",
            "version": 1,
            "projection_manifest": manifest,
            "artifacts": {
                "index.json": {
                    "sha256": hashlib.sha256(index_payload).hexdigest(),
                    "bytes": len(index_payload),
                    "node_count": len(nodes),
                    "edge_count": 0,
                },
                "claim_graph.json": {
                    "sha256": hashlib.sha256(graph_payload).hexdigest(),
                    "bytes": len(graph_payload),
                    "node_count": 0,
                    "edge_count": 0,
                },
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    metadata = {
        "status": "verified",
        "contract": "index-claim-graph-sidecar@1",
        "generation": "1" * 32,
        "canonical_generation_token": canonical_token,
        "manifest_sha256": hashlib.sha256(sidecar_payload).hexdigest(),
        "index_sha256": hashlib.sha256(index_payload).hexdigest(),
        "claim_graph_sha256": hashlib.sha256(graph_payload).hexdigest(),
        "expected_node_keyset_sha256": tool_storage_baseline._keyset_sha256(
            expected_nodes
        ),
        "expected_fts_corpus_sha256": corpus_digest.hexdigest(),
    }
    return {
        "projection_metadata": metadata,
        "projection_artifacts": {
            "projection_pair_manifest.json": sidecar_payload,
            "index.json": index_payload,
            "claim_graph.json": graph_payload,
        },
    }


@pytest.mark.parametrize(
    "expected_rows",
    [
        {
            "Concept_A": ("alpha", "first", "alpha body"),
            "Concept_B": ("beta", "second", "beta body"),
        },
        [
            ("Concept_A", "alpha", "first", "alpha body"),
            ("Concept_B", "beta", "second", "beta body"),
        ],
    ],
)
def test_clean_baseline_supports_mapping_and_tuple_expected_rows(expected_rows):
    connection = _connection()
    try:
        connection.executemany(
            "INSERT INTO wiki_search_index(node_key, title, summary, text) "
            "VALUES (?, ?, ?, ?)",
            [
                ("Concept_A", "alpha", "first", "alpha body"),
                ("Concept_B", "beta", "second", "beta body"),
            ],
        )
        _insert_vector(connection, "Concept_A", [1.0, 0.0, 0.0])
        _insert_vector(connection, "Concept_B", [0.0, 1.0, 0.0])

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A", "Concept_B"},
            expected_fts_rows=expected_rows,
            query_probes={"alpha": ["Concept_A"]},
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            top_k=2,
            sample_size=2,
            tokenizer_metadata=_tokenizer_metadata(),
            vector_metadata=_metadata("Concept_A", "Concept_B"),
            **_projection_authority_args(
                connection,
                {"Concept_A", "Concept_B"}, expected_rows
            ),
        )

        assert result["read_only"] is True
        assert result["connection_owned_by_caller"] is True
        assert result["fts"]["schema_matches_target"] is True
        assert result["fts"]["corpus_matches_expected"] is True
        assert result["fts"]["actual_corpus_sha256"] == result["fts"][
            "expected_corpus_sha256"
        ]
        assert result["fts"]["query_probes_complete"] is True
        assert result["rebuild_required"] == {
            "fts_index": False,
            "fts_tokenizer_metadata_incomplete": False,
            "projection_authority_incomplete": False,
            "vec_exact_repack_blocked": False,
            "vec_regeneration_blocked": False,
            "any": False,
        }
        assert result["vec0"]["dimension_counts"] == {"3": 2}
        assert result["vec0"]["hash_complete"] is True
        assert result["vec0"]["malformed_count"] == 0
        assert result["vec0"]["top_k_probes_complete"] is True
        assert all(
            probe["self_rank"] == 1 for probe in result["vec0"]["top_k_probes"]
        )
        assert result["repack_ready"] is True
        assert result["regenerate_ready"] is True
    finally:
        connection.close()


def test_expected_corpus_hash_is_independent_from_actual_rows():
    connection = _connection()
    try:
        expected_mapping = {"Concept_A": ("alpha", "", "expected")}
        expected_tuples = [("Concept_A", "alpha", "", "expected")]
        connection.execute(
            "INSERT INTO wiki_search_index VALUES (?, ?, ?, ?)",
            ("Concept_A", "alpha", "", "actual"),
        )

        mapping_result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=expected_mapping,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )
        tuple_result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=expected_tuples,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )

        assert mapping_result["fts"]["expected_corpus_sha256"] == tuple_result[
            "fts"
        ]["expected_corpus_sha256"]
        assert mapping_result["fts"]["actual_corpus_sha256"] != mapping_result[
            "fts"
        ]["expected_corpus_sha256"]
        assert mapping_result["fts"]["content_mismatch_key_count"] == 1
        assert mapping_result["fts"]["rebuild_required"] is True
    finally:
        connection.close()


def test_fts_tokenizer_drift_returns_rebuild_plan_instead_of_raising():
    connection = _connection(tokenizer="porter unicode61")
    try:
        connection.execute(
            "INSERT INTO wiki_search_index VALUES (?, ?, ?, ?)",
            ("Concept_A", "alpha", "", "alpha"),
        )

        result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows={"Concept_A": ("alpha", "", "alpha")},
            query_probes={"alpha": ["Concept_A"]},
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )

        assert result["fts"]["schema_matches_target"] is False
        assert result["fts"]["actual_tokenizer"] == "porter unicode61"
        assert result["fts"]["target_tokenizer"] == "unicode61 remove_diacritics 1"
        assert result["fts"]["corpus_matches_expected"] is True
        assert result["fts"]["rebuild_required"] is True
        assert result["errors"] == []
    finally:
        connection.close()


def test_fts_reports_duplicate_missing_orphan_and_content_drift():
    connection = _connection()
    try:
        connection.executemany(
            "INSERT INTO wiki_search_index VALUES (?, ?, ?, ?)",
            [
                ("Concept_A", "alpha", "", "first"),
                ("Concept_A", "alpha", "", "duplicate"),
                ("Concept_C", "charlie", "", "orphan"),
            ],
        )

        result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows={
                "Concept_A": ("alpha", "", "first"),
                "Concept_B": ("beta", "", "missing"),
            },
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )
        fts = result["fts"]

        assert fts["duplicate_key_count"] == 1
        assert fts["duplicate_row_count"] == 1
        assert fts["missing_keys"] == ["Concept_B"]
        assert fts["orphan_keys"] == ["Concept_C"]
        assert fts["content_mismatch_keys"] == ["Concept_A"]
        assert fts["rebuild_required"] is True
    finally:
        connection.close()


def test_vec0_malformed_rows_block_repack_and_missing_metadata_blocks_regeneration():
    connection = _connection()
    try:
        _insert_vector(connection, "", [1.0, 0.0, 0.0])
        _insert_vector(connection, "Concept_A", [0.0, 0.0, 0.0])

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A", "Concept_B"},
            expected_fts_rows=[],
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )
        vec0 = result["vec0"]

        assert vec0["malformed_counts"]["blank_id"] == 1
        assert vec0["malformed_counts"]["zero_norm"] == 1
        assert vec0["missing_ids"] == ["Concept_B"]
        assert vec0["orphan_ids"] == [""]
        assert vec0["repack_ready"] is False
        assert vec0["metadata"]["missing_count"] == 2
        assert vec0["regenerate_ready"] is False
    finally:
        connection.close()


def test_regeneration_requires_complete_model_content_recipe_metadata():
    connection = _connection()
    try:
        _insert_vector(connection, "Concept_A", [1.0, 0.0, 0.0])
        projection_rows = {"Concept_A": ("", "", "")}
        incomplete = {
            "Concept_A": {
                "model": "test-model",
                "content_hash": "a" * 64,
                "dimension": 3,
            }
        }

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            vector_metadata=incomplete,
            **_projection_authority_args(
                connection,
                {"Concept_A"},
                projection_rows,
            ),
        )

        assert result["repack_ready"] is True
        assert result["vec0"]["metadata"]["invalid"] == {
            "Concept_A": ["content_recipe_missing"]
        }
        assert result["regenerate_ready"] is False
    finally:
        connection.close()


def test_tokenizer_metadata_is_hashed_and_missing_fields_are_explicit():
    connection = _connection()
    try:
        missing = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=[],
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )
        complete = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=[],
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            tokenizer_metadata=_tokenizer_metadata(),
        )

        status = missing["fts"]["tokenizer_metadata"]
        assert status["complete"] is False
        assert status["missing_fields"] == [
            "engine",
            "package_version",
            "segmentation_recipe",
        ]
        assert missing["rebuild_required"]["fts_tokenizer_metadata_incomplete"] is True
        complete_status = complete["fts"]["tokenizer_metadata"]
        assert complete_status["complete"] is True
        assert re.fullmatch(r"[0-9a-f]{64}", complete_status["stable_sha256"])
        assert complete["rebuild_required"]["fts_tokenizer_metadata_incomplete"] is False
        assert status["stable_sha256"] != complete_status["stable_sha256"]
    finally:
        connection.close()


def test_baseline_is_deterministic_uses_only_public_tables_and_keeps_connection_open():
    raw = _connection()
    try:
        raw.execute(
            "INSERT INTO wiki_search_index VALUES (?, ?, ?, ?)",
            ("Concept_A", "alpha", "", "alpha"),
        )
        _insert_vector(raw, "Concept_A", [1.0, 0.0, 0.0])

        class RecordingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.statements = []

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                return self.connection.execute(statement, parameters)

        projection_rows = {"Concept_A": ("alpha", "", "alpha")}
        authority_args = _projection_authority_args(
            raw,
            {"Concept_A"},
            projection_rows,
        )
        recording = RecordingConnection(raw)
        kwargs = {
            "expected_nodes": {"Concept_A"},
            "expected_fts_rows": projection_rows,
            "query_probes": {"alpha": ["Concept_A"]},
            "target_wiki_fts_ddl": _fts_ddl(),
            "target_vec_ddl": _vec_ddl(),
            "expected_dimension": 3,
            "top_k": 1,
            "sample_size": 1,
            "tokenizer_metadata": _tokenizer_metadata(),
            "vector_metadata": _metadata("Concept_A"),
            **authority_args,
        }

        first = inspect_storage_baseline(recording, **kwargs)
        second = inspect_storage_baseline(recording, **kwargs)

        assert first["fts"]["actual_corpus_sha256"] == second["fts"][
            "actual_corpus_sha256"
        ]
        assert first["vec0"]["id_blob_sha256"] == second["vec0"][
            "id_blob_sha256"
        ]
        assert first["vec0"]["top_k_probes"] == second["vec0"]["top_k_probes"]
        assert first["baseline_fingerprint"] == second["baseline_fingerprint"]
        payload = dict(first)
        fingerprint = payload.pop("baseline_fingerprint")
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == fingerprint
        submitted_sql = "\n".join(recording.statements).casefold()
        assert "runtime_generations" in recording.statements[0].casefold()
        assert next(
            index
            for index, statement in enumerate(recording.statements)
            if "sqlite_master" in statement.casefold()
        ) > 0
        assert re.search(r"\b(insert|update|delete|drop|alter|create|vacuum|reindex)\b", submitted_sql) is None
        for forbidden in (
            "wiki_search_index_data",
            "wiki_search_index_idx",
            "wiki_search_index_content",
            "wiki_search_index_docsize",
            "wiki_search_index_config",
            "vec_embeddings_chunks",
            "vec_embeddings_rowids",
            "vec_embeddings_vector_chunks",
        ):
            assert forbidden not in submitted_sql
        assert raw.execute("SELECT 1").fetchone()[0] == 1
    finally:
        raw.close()


def test_top_k_probe_requires_the_sample_itself_to_rank_first():
    connection = _connection()
    try:
        _insert_vector(connection, "Concept_A", [1.0, 0.0, 0.0])
        _insert_vector(connection, "Concept_B", [1.0, 0.0, 0.0])
        projection_rows = {
            "Concept_A": ("", "", ""),
            "Concept_B": ("", "", ""),
        }

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A", "Concept_B"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            top_k=1,
            sample_size=2,
            tokenizer_metadata=_tokenizer_metadata(),
            vector_metadata=_metadata("Concept_A", "Concept_B"),
            **_projection_authority_args(
                connection,
                {"Concept_A", "Concept_B"},
                projection_rows,
            ),
        )

        assert any(
            not probe["self_first"] for probe in result["vec0"]["top_k_probes"]
        )
        assert result["vec0"]["top_k_probes_complete"] is False
        assert result["repack_ready"] is False
    finally:
        connection.close()


def test_vector_metadata_recipe_is_bound_into_the_baseline_fingerprint():
    connection = _connection()
    try:
        _insert_vector(connection, "Concept_A", [1.0, 0.0, 0.0])
        _insert_vector(connection, "Concept_B", [0.0, 1.0, 0.0])
        _insert_vector(connection, "Concept_C", [0.0, 0.0, 1.0])
        expected_nodes = {"Concept_A", "Concept_B", "Concept_C"}
        projection_rows = {
            node_key: ("", "", "")
            for node_key in expected_nodes
        }
        common = {
            "expected_nodes": expected_nodes,
            "expected_fts_rows": projection_rows,
            "target_wiki_fts_ddl": _fts_ddl(),
            "target_vec_ddl": _vec_ddl(),
            "expected_dimension": 3,
            "sample_size": 1,
            "tokenizer_metadata": _tokenizer_metadata(),
            **_projection_authority_args(
                connection,
                expected_nodes,
                projection_rows,
            ),
        }
        first = inspect_storage_baseline(
            connection,
            vector_metadata=_metadata(*expected_nodes),
            **common,
        )
        changed_metadata = _metadata(*expected_nodes)
        changed_metadata["Concept_C"].update(
            {
                "model": "different-model",
                "content_hash": "f" * 64,
                "content_recipe": "different-recipe:v2",
            }
        )
        second = inspect_storage_baseline(
            connection,
            vector_metadata=changed_metadata,
            **common,
        )

        assert first["regenerate_ready"] is True
        assert second["regenerate_ready"] is True
        assert first["vec0"]["metadata"]["stable_sha256"] != second["vec0"][
            "metadata"
        ]["stable_sha256"]
        assert first["baseline_fingerprint"] != second["baseline_fingerprint"]
    finally:
        connection.close()


def test_regeneration_rejects_target_dimension_drift():
    connection = _connection()
    try:
        _insert_vector(connection, "Concept_A", [1.0, 0.0, 0.0])
        projection_rows = {"Concept_A": ("", "", "")}

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(4),
            expected_dimension=3,
            tokenizer_metadata=_tokenizer_metadata(),
            vector_metadata=_metadata("Concept_A"),
            **_projection_authority_args(
                connection,
                {"Concept_A"},
                projection_rows,
            ),
        )

        assert result["vec0"]["target_declared_dimension"] == 4
        assert result["vec0"]["target_dimension_matches_expected"] is False
        assert result["vec0"]["surface_regenerate_ready"] is False
        assert result["regenerate_ready"] is False
    finally:
        connection.close()


def test_missing_projection_authority_blocks_repack_and_regeneration():
    connection = _connection()
    try:
        _insert_vector(connection, "Concept_A", [1.0, 0.0, 0.0])

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=[],
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            tokenizer_metadata=_tokenizer_metadata(),
            vector_metadata=_metadata("Concept_A"),
        )

        assert result["projection_authority"]["complete"] is False
        assert result["vec0"]["surface_repack_ready"] is True
        assert result["vec0"]["surface_regenerate_ready"] is True
        assert result["repack_ready"] is False
        assert result["regenerate_ready"] is False
        assert result["rebuild_required"]["projection_authority_incomplete"] is True
    finally:
        connection.close()


def test_formatted_projection_declarations_without_artifact_bytes_fail_closed():
    connection = _connection()
    try:
        projection_rows = {"Concept_A": ("", "", "")}
        authority_args = _projection_authority_args(
            connection,
            {"Concept_A"},
            projection_rows,
        )

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            projection_metadata=authority_args["projection_metadata"],
        )

        projection = result["projection_authority"]
        assert projection["complete"] is False
        assert projection["missing_artifacts"] == [
            "claim_graph.json",
            "index.json",
            "projection_pair_manifest.json",
        ]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    [
        ({"generation": None}, None),
        ({"status": "stale"}, "status_not_verified"),
        ({"contract": "anything"}, "contract_unsupported"),
        ({"generation": "not-a-generation"}, "generation_invalid"),
        ({"canonical_generation_token": "not-a-hash"}, "canonical_generation_token_invalid"),
        ({"manifest_sha256": "not-a-hash"}, "manifest_sha256_invalid"),
        ({"expected_node_keyset_sha256": "f" * 64}, "expected_node_keyset_sha256_mismatch"),
        ({"expected_fts_corpus_sha256": "f" * 64}, "expected_fts_corpus_sha256_mismatch"),
    ],
)
def test_projection_authority_rejects_missing_invalid_or_mismatched_fields(
    mutation,
    expected_issue,
):
    connection = _connection()
    try:
        projection_rows = {"Concept_A": ("", "", "")}
        authority_args = _projection_authority_args(
            connection,
            {"Concept_A"},
            projection_rows,
        )
        authority = authority_args["projection_metadata"]
        authority.update(mutation)

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            projection_metadata=authority,
            projection_artifacts=authority_args["projection_artifacts"],
        )

        projection = result["projection_authority"]
        assert projection["complete"] is False
        if expected_issue is None:
            assert "generation" in projection["missing_fields"]
        else:
            assert expected_issue in projection["invalid_fields"]
        assert result["rebuild_required"]["projection_authority_incomplete"] is True
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "replacement", "expected_issue"),
    [
        ("generation", "2" * 32, "generation_mismatch"),
        (
            "canonical_generation_token",
            "f" * 64,
            "canonical_generation_token_mismatch",
        ),
        ("manifest_sha256", "f" * 64, "manifest_sha256_mismatch"),
        ("index_sha256", "f" * 64, "index_sha256_mismatch"),
        ("claim_graph_sha256", "f" * 64, "claim_graph_sha256_mismatch"),
    ],
)
def test_projection_authority_rejects_well_formed_but_unbound_declarations(
    field,
    replacement,
    expected_issue,
):
    connection = _connection()
    try:
        projection_rows = {"Concept_A": ("", "", "")}
        authority_args = _projection_authority_args(
            connection,
            {"Concept_A"},
            projection_rows,
        )
        authority_args["projection_metadata"][field] = replacement

        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            **authority_args,
        )

        assert result["projection_authority"]["complete"] is False
        assert expected_issue in result["projection_authority"]["invalid_fields"]
    finally:
        connection.close()


def test_projection_authority_rejects_tampered_artifact_bytes_and_generation():
    connection = _connection()
    try:
        projection_rows = {"Concept_A": ("alpha", "", "alpha")}
        tampered_args = _projection_authority_args(
            connection,
            {"Concept_A"},
            projection_rows,
        )
        tampered_args["projection_artifacts"]["index.json"] += b" "
        with pytest.raises(ValueError, match="index.json_sha256_mismatch"):
            inspect_storage_baseline(
                connection,
                expected_nodes={"Concept_A"},
                expected_fts_rows=projection_rows,
                target_wiki_fts_ddl=_fts_ddl(),
                target_vec_ddl=_vec_ddl(),
                **tampered_args,
            )

        stale_args = _projection_authority_args(
            connection,
            {"Concept_A"},
            projection_rows,
        )
        connection.execute(
            "UPDATE runtime_generations SET generation = generation + 1 "
            "WHERE surface = ?",
            (tool_storage_baseline._CANONICAL_PROJECTION_SURFACES[0],),
        )
        stale = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            **stale_args,
        )
        assert stale["projection_authority"]["complete"] is False
        assert "runtime_generation_mismatch" in stale["projection_authority"][
            "invalid_fields"
        ]
    finally:
        connection.close()


def test_projection_authority_rechecks_generation_after_storage_scans():
    raw = _connection()
    try:
        projection_rows = {"Concept_A": ("", "", "")}
        authority_args = _projection_authority_args(
            raw,
            {"Concept_A"},
            projection_rows,
        )
        _insert_vector(raw, "Concept_A", [1.0, 0.0, 0.0])

        class MutatingCursor:
            def __init__(self, cursor, connection):
                self.cursor = cursor
                self.connection = connection

            def fetchall(self):
                rows = self.cursor.fetchall()
                self.connection.execute(
                    "UPDATE runtime_generations SET generation = generation + 1 "
                    "WHERE surface = ?",
                    (tool_storage_baseline._CANONICAL_PROJECTION_SURFACES[0],),
                )
                return rows

        class MutatingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.mutated = False

            @property
            def in_transaction(self):
                return self.connection.in_transaction

            def execute(self, statement, parameters=()):
                cursor = self.connection.execute(statement, parameters)
                if "from main.runtime_generations" in statement.casefold() and not self.mutated:
                    self.mutated = True
                    return MutatingCursor(cursor, self.connection)
                return cursor

        result = inspect_storage_baseline(
            MutatingConnection(raw),
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            vector_metadata=_metadata("Concept_A"),
            **authority_args,
        )

        projection = result["projection_authority"]
        assert projection["runtime_generation_rechecked"] is True
        assert projection["complete"] is False
        assert "runtime_generation_changed_during_scan" in projection["invalid_fields"]
        assert result["repack_ready"] is False
    finally:
        raw.close()


def test_projection_authority_rejects_transaction_end_during_final_generation_read():
    raw = _connection()
    try:
        projection_rows = {"Concept_A": ("", "", "")}
        authority_args = _projection_authority_args(
            raw,
            {"Concept_A"},
            projection_rows,
        )
        _insert_vector(raw, "Concept_A", [1.0, 0.0, 0.0])

        class EndingCursor:
            def __init__(self, cursor, connection):
                self.cursor = cursor
                self.connection = connection

            def fetchall(self):
                rows = self.cursor.fetchall()
                self.connection.commit()
                return rows

        class EndingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.generation_reads = 0

            @property
            def in_transaction(self):
                return self.connection.in_transaction

            def execute(self, statement, parameters=()):
                cursor = self.connection.execute(statement, parameters)
                if "from main.runtime_generations" in statement.casefold():
                    self.generation_reads += 1
                    if self.generation_reads == 2:
                        return EndingCursor(cursor, self.connection)
                return cursor

        proxy = EndingConnection(raw)
        result = inspect_storage_baseline(
            proxy,
            expected_nodes={"Concept_A"},
            expected_fts_rows=projection_rows,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            vector_metadata=_metadata("Concept_A"),
            **authority_args,
        )

        projection = result["projection_authority"]
        assert proxy.generation_reads == 2
        assert projection["caller_transaction_active"] is False
        assert projection["complete"] is False
        assert "caller_transaction_lost" in projection["invalid_fields"]
        assert result["repack_ready"] is False
    finally:
        raw.close()


def test_projection_authority_binds_expected_fts_corpus_to_index_bytes():
    connection = _connection()
    try:
        authority_args = _projection_authority_args(
            connection,
            {"Concept_A"},
            {"Concept_A": ("alpha", "", "alpha")},
        )

        with pytest.raises(ValueError, match="artifact_expected_fts_corpus_mismatch"):
            inspect_storage_baseline(
                connection,
                expected_nodes={"Concept_A"},
                expected_fts_rows={"Concept_A": ("beta", "", "beta")},
                target_wiki_fts_ddl=_fts_ddl(),
                target_vec_ddl=_vec_ddl(),
                **authority_args,
            )
    finally:
        connection.close()


def test_projection_authority_input_budgets_fail_before_any_sql(monkeypatch):
    raw = _connection()

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.statements = []

        def execute(self, statement, parameters=()):
            self.statements.append(statement)
            return self.connection.execute(statement, parameters)

    recording = RecordingConnection(raw)
    try:
        with pytest.raises(ValueError, match="unsupported field"):
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
                projection_metadata={"attestation": {"nested": ["x"] * 1000}},
            )
        assert recording.statements == []

        oversized_key = "x" * 10_000
        with pytest.raises(ValueError) as error:
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
                projection_artifacts={oversized_key: b"{}"},
            )
        assert oversized_key not in str(error.value)
        assert recording.statements == []

        tokenizer_recipe = {}
        for _ in range(tool_storage_baseline._MAX_TOKENIZER_RECIPE_DEPTH + 1):
            tokenizer_recipe = {"nested": tokenizer_recipe}
        with pytest.raises(ValueError, match="segmentation_recipe exceeds depth"):
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
                tokenizer_metadata={
                    "engine": "rjieba",
                    "package_version": "test",
                    "segmentation_recipe": tokenizer_recipe,
                },
            )
        assert recording.statements == []

        vector_record = {}
        for _ in range(tool_storage_baseline._MAX_VECTOR_METADATA_DEPTH + 1):
            vector_record = {"nested": vector_record}
        with pytest.raises(ValueError, match="vector_metadata exceeds depth"):
            inspect_storage_baseline(
                recording,
                expected_nodes={"Concept_A"},
                expected_fts_rows=[],
                vector_metadata={"Concept_A": vector_record},
            )
        assert recording.statements == []

        with pytest.raises(TypeError, match="must be a mapping"):
            inspect_storage_baseline(
                recording,
                expected_nodes={"Concept_A"},
                expected_fts_rows=[],
                vector_metadata={"Concept_A": "invalid"},
            )
        assert recording.statements == []

        nested = {}
        for _ in range(tool_storage_baseline._MAX_PROJECTION_JSON_DEPTH + 1):
            nested = {"nested": nested}
        with pytest.raises(ValueError, match="exceeds JSON depth"):
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
                projection_artifacts={
                    "projection_pair_manifest.json": json.dumps(nested).encode(),
                    "index.json": b"{}",
                    "claim_graph.json": b"{}",
                },
            )
        assert recording.statements == []

        class ExplodingBytearray(bytearray):
            def __bytes__(self):
                raise AssertionError("payload was materialized before its size check")

        monkeypatch.setattr(
            tool_storage_baseline,
            "_MAX_PROJECTION_ARTIFACT_BYTES",
            2,
        )
        monkeypatch.setattr(
            tool_storage_baseline,
            "_MAX_PROJECTION_ARTIFACT_TOTAL_BYTES",
            3,
        )
        with pytest.raises(ValueError, match="aggregate byte budget"):
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
                projection_artifacts={
                    "index.json": ExplodingBytearray(b"{}"),
                    "claim_graph.json": ExplodingBytearray(b"{}"),
                },
            )
        assert recording.statements == []

        monkeypatch.setattr(
            tool_storage_baseline,
            "_MAX_PROJECTION_ARTIFACT_BYTES",
            1,
        )
        with pytest.raises(ValueError, match="exceeds 1 bytes"):
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
                projection_artifacts={
                    "index.json": ExplodingBytearray(b"{}"),
                },
            )
        assert recording.statements == []
    finally:
        raw.close()


def test_structurally_invalid_projection_artifacts_fail_before_any_sql():
    raw = _connection()

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.statements = []

        @property
        def in_transaction(self):
            return self.connection.in_transaction

        def execute(self, statement, parameters=()):
            self.statements.append(statement)
            return self.connection.execute(statement, parameters)

    try:
        projection_rows = {"Concept_A": ("", "", "")}
        authority_args = _projection_authority_args(
            raw,
            {"Concept_A"},
            projection_rows,
        )
        sidecar = json.loads(
            authority_args["projection_artifacts"]["projection_pair_manifest.json"]
        )
        sidecar["contract"] = "unsupported"
        authority_args["projection_artifacts"]["projection_pair_manifest.json"] = (
            json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode()
        )
        recording = RecordingConnection(raw)

        with pytest.raises(ValueError, match="sidecar_contract_invalid"):
            inspect_storage_baseline(
                recording,
                expected_nodes={"Concept_A"},
                expected_fts_rows=projection_rows,
                target_wiki_fts_ddl=_fts_ddl(),
                target_vec_ddl=_vec_ddl(),
                **authority_args,
            )
        assert recording.statements == []
    finally:
        raw.close()


def test_storage_baseline_requires_caller_owned_transaction_before_sql():
    raw = sqlite3.connect(":memory:")

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.statements = []

        def execute(self, statement, parameters=()):
            self.statements.append(statement)
            return self.connection.execute(statement, parameters)

    recording = RecordingConnection(raw)
    try:
        with pytest.raises(ValueError, match="active caller-owned SQLite transaction"):
            inspect_storage_baseline(
                recording,
                expected_nodes=set(),
                expected_fts_rows=[],
            )
        assert recording.statements == []
    finally:
        raw.close()


def test_fts_query_probe_report_is_bounded_and_hashes_omitted_evidence():
    connection = _connection()
    try:
        connection.execute(
            "INSERT INTO wiki_search_index VALUES (?, ?, ?, ?)",
            ("Concept_A", "alpha", "", "alpha"),
        )
        expected_ids = ["Concept_A", *(f"Concept_{index}" for index in range(9))]
        probes = [("alpha" + " " * 600, expected_ids)] + [
            ("alpha", expected_ids) for _ in range(4)
        ]

        result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows={"Concept_A": ("alpha", "", "alpha")},
            query_probes=probes,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            sample_size=2,
        )
        changed_probes = list(probes)
        changed_probes[-1] = ("alpha", [*expected_ids, "Concept_Omitted_Change"])
        changed = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows={"Concept_A": ("alpha", "", "alpha")},
            query_probes=changed_probes,
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
            sample_size=2,
        )
        fts = result["fts"]

        assert fts["query_probe_summary"]["input_count"] == 5
        assert fts["query_probe_summary"]["executed_count"] == 2
        assert fts["query_probe_summary"]["truncated"] is True
        assert fts["query_probe_summary"]["sample_limit"] == 2
        assert re.fullmatch(
            r"[0-9a-f]{64}", fts["query_probe_summary"]["stable_sha256"]
        )
        assert len(fts["query_probes"]) == 2
        assert len(fts["query_probes"][0]["query"]) <= 512
        assert fts["query_probes"][0]["query_truncated"] is True
        assert len(fts["query_probes"][0]["expected_ids"]) == 2
        assert len(fts["query_probes"][0]["missing_expected_ids"]) == 2
        assert fts["query_probes_complete"] is False
        assert fts["query_probes"] == changed["fts"]["query_probes"]
        assert fts["query_probe_summary"]["stable_sha256"] != changed["fts"][
            "query_probe_summary"
        ]["stable_sha256"]
        assert result["baseline_fingerprint"] != changed["baseline_fingerprint"]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "query_probes",
    [
        [("alpha", ())] * 129,
        [("a" * 4097, ())],
        [("alpha", [f"Concept_{index}" for index in range(257)])],
        [("alpha", ["x" * 1025])],
    ],
)
def test_fts_query_probe_input_budgets_fail_closed(query_probes):
    connection = _connection()
    try:
        statements = []

        class RecordingConnection:
            def execute(self, statement, parameters=()):
                statements.append(statement)
                return connection.execute(statement, parameters)

        with pytest.raises(ValueError):
            inspect_storage_baseline(
                RecordingConnection(),
                expected_nodes=set(),
                expected_fts_rows=[],
                query_probes=query_probes,
                target_wiki_fts_ddl=_fts_ddl(),
                target_vec_ddl=_vec_ddl(),
                expected_dimension=3,
            )
        assert statements == []
    finally:
        connection.close()


def test_missing_fts_table_binds_one_shot_query_probe_generator_once():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")

        def probes():
            yield "alpha", ("Concept_A",)
            yield "beta", ("Concept_B",)

        result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=[],
            query_probes=probes(),
            expected_dimension=3,
        )
        summary = result["fts"]["query_probe_summary"]

        assert summary["input_count"] == 2
        assert summary["executed_count"] == 0
        assert summary["stable_sha256"] == tool_storage_baseline._canonical_json_sha256(
            [
                ("alpha", ("Concept_A",)),
                ("beta", ("Concept_B",)),
            ]
        )
        assert summary["stable_sha256"] != tool_storage_baseline._canonical_json_sha256(
            []
        )
    finally:
        connection.close()


def test_oversize_actual_fts_identifier_fails_closed_without_echoing_value():
    connection = _connection()
    try:
        oversize = "x" * 1025
        connection.execute(
            "INSERT INTO wiki_search_index VALUES (?, ?, ?, ?)",
            (oversize, "alpha", "", "alpha"),
        )

        result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=[],
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )

        assert "actual FTS node_key exceeds 1024 UTF-8 bytes" == result["fts"][
            "scan_error"
        ]
        assert oversize not in json.dumps(result, ensure_ascii=False)
    finally:
        connection.close()


def test_oversize_actual_vec0_identifier_fails_closed_without_echoing_value():
    connection = _connection()
    try:
        oversize = "x" * 1025
        _insert_vector(connection, oversize, [1.0, 0.0, 0.0])

        result = inspect_storage_baseline(
            connection,
            expected_nodes=set(),
            expected_fts_rows=[],
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )

        assert result["vec0"]["malformed_counts"] == {"id_too_long": 1}
        assert result["vec0"]["repack_ready"] is False
        assert oversize not in json.dumps(result, ensure_ascii=False)
        assert result["vec0"]["malformed_samples"]["id_too_long"][0].startswith(
            "<oversize-id bytes=1025 sha256="
        )
    finally:
        connection.close()


def test_vec0_top_k_probe_sanitizes_late_oversize_result_identifier():
    oversize = "x" * 1025

    class ResultCursor:
        def fetchall(self):
            return [(oversize, 0.0)]

    class ResultConnection:
        def execute(self, statement, parameters=()):
            return ResultCursor()

    probes, complete = tool_storage_baseline._run_vec_top_k_probes(
        ResultConnection(),
        samples=[("rank", "Concept_A", b"vector", "a" * 64)],
        known_ids={"Concept_A", oversize},
        top_k=1,
    )

    assert complete is False
    assert oversize not in json.dumps(probes, ensure_ascii=False)
    assert probes[0]["result_ids"][0].startswith(
        "<oversize-id bytes=1025 sha256="
    )


def test_missing_public_tables_return_structured_not_ready_result():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")
        result = inspect_storage_baseline(
            connection,
            expected_nodes={"Concept_A"},
            expected_fts_rows={"Concept_A": ("alpha", "", "alpha")},
            target_wiki_fts_ddl=_fts_ddl(),
            target_vec_ddl=_vec_ddl(),
            expected_dimension=3,
        )

        assert result["fts"]["actual_ddl"] is None
        assert result["fts"]["schema_matches_target"] is False
        assert result["fts"]["rebuild_required"] is True
        assert result["vec0"]["actual_ddl"] is None
        assert result["vec0"]["extension_error"]
        assert result["repack_ready"] is False
        assert result["regenerate_ready"] is False
    finally:
        connection.close()
