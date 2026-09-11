import hashlib
import json
import sqlite3

import pytest

from vector_lake import db_store
from vector_lake.memory_search_normalization import (
    BUILD_MARKER,
    NORMALIZATION_CONTRACT_DOMAIN,
    casefold_any,
    casefold_text,
    register_sqlite_functions,
)


def _oracle(value):
    return str(value or "").casefold()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("straße", "strasse"),
        ("SS", "ss"),
        ("Σςσ", "σσσ"),
        ("中文𠀀", "中文𠀀"),
        ("Ａ", "ａ"),
    ],
)
def test_casefold_contract_matches_independent_oracle_without_nfkc(value, expected):
    assert casefold_text(value) == _oracle(value) == expected
    assert casefold_text("Ａ") != "a"


def test_casefold_any_is_deterministic_bounded_and_rejects_malformed_arguments():
    conn = sqlite3.connect(":memory:")
    register_sqlite_functions(conn)
    encoded = json.dumps(["strasse", "σ", "𠀀"], ensure_ascii=False)
    assert conn.execute("SELECT casefold_any(?, ?)", ("Straße", encoded)).fetchone()[0]
    assert conn.execute("SELECT casefold_any(?, ?)", ("ς", encoded)).fetchone()[0]
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT casefold_any('text', 'not-json')").fetchone()
    with pytest.raises(ValueError):
        casefold_any("text", json.dumps(["x"] * 129))


def test_udf_accepts_valid_long_payload_and_bounds_each_term():
    terms = ["x" * 512, "strasse"]
    encoded = json.dumps(terms)
    assert len(encoded) > 512
    assert casefold_any("Straße", encoded) == 1
    with pytest.raises(ValueError, match="longer than 512"):
        casefold_any("text", json.dumps(["x" * 513]))


def test_udf_cache_is_safe_under_concurrent_eviction():
    from concurrent.futures import ThreadPoolExecutor
    from vector_lake.memory_search_normalization import _decode_terms
    inputs = [json.dumps(["strasse", str(i)]) for i in range(200)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda encoded: casefold_any("Straße", encoded), inputs))
    assert _decode_terms.cache_info().currsize <= 64


def test_managed_readonly_connection_registers_udf_without_writes(isolated_memory, monkeypatch):
    db_store.init_db()
    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "readonly")
    conn = db_store.get_connection()
    assert conn.execute("SELECT casefold_any(?,?)", ("Straße", '["strasse"]')).fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("CREATE TABLE forbidden_normalization_probe(id)")
    db_store.close_all_connections()


def test_partial_queries_fence_both_fts_branches_to_replayed_prefix():
    from vector_lake import governance_store
    sql, params = governance_store._indexed_operational_memory_query(
        ["strasse", "σ"], {"fact"}, cursor="memory_1", target="memory_9",
        include_pending=True, candidate_limit=20,
    )
    assert sql.count("docs.memory_id <= ?") == 2
    assert params.count("memory_1") == 3  # two FTS branches and the canonical tail
    ready_sql, _ = governance_store._indexed_operational_memory_query(
        ["strasse", "σ"], None, cursor="memory_9", target="memory_9",
        include_pending=False, candidate_limit=20,
    )
    assert "docs.memory_id <= ?" not in ready_sql


def test_build_marker_and_proof_generation_are_separate_64hex_domains():
    digests = ("0" * 64, "1" * 64, "2" * 64, "3" * 64)
    generation = db_store._operational_memory_search_proof_generation(digests)
    old_digest = hashlib.sha256()
    for value in digests:
        db_store._update_integrity_digest_value(old_digest, value)
    assert len(BUILD_MARKER) == len(generation) == 64
    assert generation != BUILD_MARKER
    assert generation != old_digest.hexdigest()
    assert NORMALIZATION_CONTRACT_DOMAIN.encode() not in b"".join(
        value.encode() for value in digests
    )


def test_registered_udf_is_available_after_managed_connection_reconnect(
    isolated_memory,
):
    first = db_store.get_connection()
    assert first.execute(
        "SELECT casefold_any(?, ?)", ("STRASSE", '["strasse"]')
    ).fetchone()[0] == 1
    db_store.close_all_connections()
    second = db_store.get_connection()
    assert second.execute(
        "SELECT casefold_any(?, ?)", ("Straße", '["strasse"]')
    ).fetchone()[0] == 1
