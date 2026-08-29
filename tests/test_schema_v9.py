import hashlib
import json
import sqlite3

import pytest

from vector_lake import db_store


_GENERATIONS = {
    surface: offset
    for offset, surface in enumerate(
        db_store.CANONICAL_PROJECTION_SURFACES,
        start=1,
    )
}


def _sidecar(*, index_root: str = "b" * 64, marker: str = "") -> dict:
    claim_root = "c" * 64
    generation = hashlib.sha256(
        json.dumps(
            {
                "canonical_generation": _GENERATIONS,
                "claim_graph_root_sha256": claim_root,
                "index_root_sha256": index_root,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "contract": "vector-lake-projection-v2",
        "format_version": 2,
        "projection_generation": generation,
        "canonical_generation": dict(_GENERATIONS),
        "index_root_sha256": index_root,
        "claim_graph_root_sha256": claim_root,
        "published_at_utc": "2026-08-28T00:00:00+00:00",
    }
    if marker:
        payload["marker"] = marker
    return payload


def test_fresh_schema_v9_seeds_exact_rebuild_required_singleton(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path().resolve()
    state = db_store.get_projection_runtime_v9()

    assert state == {
        "format_version": 2,
        "status": "rebuild_required",
        "projection_generation": None,
        "canonical_generation": None,
        "canonical_generation_json": None,
        "sidecar_sha256": None,
        "sidecar": None,
        "sidecar_json": None,
        "previous_sidecar": None,
        "previous_sidecar_json": None,
        "updated_at": state["updated_at"],
    }
    inspected = db_store.inspect_schema_migration_state(path)
    assert inspected["ready"] is True
    assert inspected["user_version"] == 9
    assert inspected["ledger"][-1] == {
        "version": 9,
        "name": db_store._SCHEMA_MIGRATIONS[9][0],
        "checksum": db_store._SCHEMA_MIGRATIONS[9][1],
        "applied_at": inspected["ledger"][-1]["applied_at"],
    }


def test_projection_runtime_v9_publish_cas_ready_and_previous_pointer(
    isolated_memory,
):
    db_store.init_db()
    connection = db_store.get_connection()
    first_sidecar = _sidecar()
    first_generation = first_sidecar["projection_generation"]

    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        db_store.cas_projection_runtime_publish_pending(
            connection,
            expected_status="rebuild_required",
            expected_projection_generation=None,
            projection_generation=first_generation,
            canonical_generation=_GENERATIONS,
            sidecar_json=first_sidecar,
        )

    connection.execute("BEGIN IMMEDIATE")
    pending = db_store.cas_projection_runtime_publish_pending(
        connection,
        expected_status="rebuild_required",
        expected_projection_generation=None,
        projection_generation=first_generation,
        canonical_generation=_GENERATIONS,
        sidecar_json=first_sidecar,
    )
    connection.execute("COMMIT")
    expected_sidecar_json = json.dumps(
        first_sidecar,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert pending["status"] == "publish_pending"
    assert pending["sidecar_json"] == expected_sidecar_json
    assert pending["sidecar_sha256"] == hashlib.sha256(
        expected_sidecar_json.encode("utf-8")
    ).hexdigest()

    stale_sidecar = _sidecar(index_root="d" * 64)
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="publish CAS mismatch"):
        db_store.cas_projection_runtime_publish_pending(
            connection,
            expected_status="rebuild_required",
            expected_projection_generation=None,
            projection_generation=stale_sidecar["projection_generation"],
            canonical_generation=_GENERATIONS,
            sidecar_json=stale_sidecar,
        )
    connection.execute("ROLLBACK")

    connection.execute("BEGIN IMMEDIATE")
    ready = db_store.mark_projection_runtime_ready(
        connection,
        expected_projection_generation=first_generation,
        expected_sidecar_sha256=pending["sidecar_sha256"],
    )
    connection.execute("COMMIT")
    assert ready["status"] == "ready"
    assert ready["previous_sidecar"] is None

    next_sidecar = _sidecar(index_root="d" * 64, marker="next")
    next_generation = next_sidecar["projection_generation"]
    connection.execute("BEGIN IMMEDIATE")
    next_pending = db_store.cas_projection_runtime_publish_pending(
        connection,
        expected_status="ready",
        expected_projection_generation=first_generation,
        projection_generation=next_generation,
        canonical_generation=_GENERATIONS,
        sidecar_json=next_sidecar,
    )
    connection.execute("COMMIT")
    assert next_pending["previous_sidecar"] == first_sidecar

    connection.execute("BEGIN IMMEDIATE")
    rebuilt = db_store.mark_projection_runtime_rebuild_required(
        connection,
        expected_projection_generation=next_generation,
    )
    connection.execute("COMMIT")
    assert rebuilt["status"] == "rebuild_required"
    assert rebuilt["sidecar"] is None
    assert rebuilt["previous_sidecar"] is None


def test_projection_runtime_v9_rejects_bad_generation_and_oversize_sidecar(
    isolated_memory,
):
    db_store.init_db()
    connection = db_store.get_connection()
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="canonical_generation_invalid"):
        db_store.cas_projection_runtime_publish_pending(
            connection,
            expected_status="rebuild_required",
            expected_projection_generation=None,
            projection_generation="a" * 64,
            canonical_generation={"entities": 1},
            sidecar_json=_sidecar(),
        )
    with pytest.raises(ValueError, match="sidecar_too_large"):
        db_store.cas_projection_runtime_publish_pending(
            connection,
            expected_status="rebuild_required",
            expected_projection_generation=None,
            projection_generation=_sidecar()["projection_generation"],
            canonical_generation=_GENERATIONS,
            sidecar_json=_sidecar(marker="x" * (64 * 1024)),
        )
    connection.execute("ROLLBACK")
    assert db_store.get_projection_runtime_v9()["status"] == "rebuild_required"


def test_schema_v9_inspection_fails_closed_on_bad_table_and_bad_state(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path().resolve()
    connection = db_store.get_connection()
    sidecar = _sidecar()
    generation = sidecar["projection_generation"]
    connection.execute("BEGIN IMMEDIATE")
    pending = db_store.cas_projection_runtime_publish_pending(
        connection,
        expected_status="rebuild_required",
        expected_projection_generation=None,
        projection_generation=generation,
        canonical_generation=_GENERATIONS,
        sidecar_json=sidecar,
    )
    db_store.mark_projection_runtime_ready(
        connection,
        expected_projection_generation=generation,
        expected_sidecar_sha256=pending["sidecar_sha256"],
    )
    connection.execute(
        "UPDATE projection_runtime_v9 SET sidecar_sha256 = ? WHERE singleton = 1",
        ("0" * 64,),
    )
    connection.execute("COMMIT")
    db_store.close_all_connections()

    invalid_state = db_store.inspect_schema_migration_state(path)
    assert invalid_state["ready"] is False
    assert any(
        issue.startswith("projection_runtime_state_invalid:sidecar_sha256_mismatch")
        for issue in invalid_state["issues"]
    )

    raw = sqlite3.connect(path)
    try:
        raw.execute("ALTER TABLE projection_runtime_v9 RENAME TO projection_runtime_bad")
        raw.execute("CREATE TABLE projection_runtime_v9 (singleton INTEGER PRIMARY KEY)")
        raw.commit()
    finally:
        raw.close()
    invalid_table = db_store.inspect_schema_migration_state(path)
    assert invalid_table["ready"] is False
    assert "projection_runtime_schema_sql_mismatch:projection_runtime_v9" in (
        invalid_table["issues"]
    )
