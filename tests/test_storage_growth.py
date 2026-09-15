from datetime import datetime, timezone

from vector_lake import db_store
from vector_lake.storage_growth import (
    _max_projection_object_files,
    collect_storage_sample,
    record_storage_growth_sample,
    storage_growth_status,
)


def _sample(date_utc: str, database_bytes: int, claim_rows: int) -> dict:
    return {
        "sampled_at": f"{date_utc}T01:00:00Z",
        "date_utc": date_utc,
        "database_bytes": database_bytes,
        "wal_bytes": 0,
        "row_counts": {
            "claim_versions": claim_rows,
            "evidence_versions": claim_rows * 2,
        },
        "version_payload_bytes": database_bytes // 2,
        "backup_bytes": database_bytes * 2,
        "backup_files": 3,
        "backup_scan_complete": True,
    }


def test_storage_growth_history_upserts_one_sample_per_day(isolated_memory):
    meta = isolated_memory / "wiki" / ".meta"
    record_storage_growth_sample(
        sample=_sample("2026-08-20", 100, 10), meta_dir=meta
    )
    record_storage_growth_sample(
        sample=_sample("2026-08-20", 120, 12), meta_dir=meta
    )
    result = record_storage_growth_sample(
        sample=_sample("2026-08-21", 170, 17), meta_dir=meta
    )

    assert result["status"] == "ready"
    assert result["sample_count"] == 2
    assert result["previous"]["database_bytes"] == 120
    assert result["delta"]["database_bytes"] == 50
    assert result["delta"]["row_counts"]["claim_versions"] == 5
    assert result["elapsed_days"] == 1.0
    assert result["per_day_delta"]["database_bytes"] == 50.0
    assert storage_growth_status(meta_dir=meta) == result


def test_storage_growth_normalizes_sparse_samples_per_elapsed_day(isolated_memory):
    meta = isolated_memory / "wiki" / ".meta"
    record_storage_growth_sample(
        sample=_sample("2026-08-20", 100, 10), meta_dir=meta
    )
    result = record_storage_growth_sample(
        sample=_sample("2026-08-25", 200, 20), meta_dir=meta
    )

    assert result["delta"]["database_bytes"] == 100
    assert result["elapsed_days"] == 5.0
    assert result["per_day_delta"]["database_bytes"] == 20.0
    assert result["per_day_delta"]["row_counts"]["claim_versions"] == 2.0


def test_collect_storage_sample_is_read_only_and_counts_versions(isolated_memory):
    db_store.init_db()
    connection = db_store.get_connection()
    connection.execute(
        "INSERT INTO claim_versions("
        "claim_version_id, claim_id, claim_family_id, page_key, version_no, "
        "record_hash, data_json, recorded_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "v1",
            "claim-1",
            "family-1",
            "Concept_One",
            1,
            "hash-1",
            '{"payload":"中"}',
            "2026-08-21T00:00:00Z",
        ),
    )
    connection.commit()

    result = collect_storage_sample(
        sampled_at=datetime(2026, 8, 21, tzinfo=timezone.utc)
    )

    assert result["date_utc"] == "2026-08-21"
    assert result["database_bytes"] > 0
    assert result["row_counts"]["claim_versions"] == 1
    assert result["version_payload_bytes"] >= len('{"payload":"中"}'.encode("utf-8"))


def test_projection_object_scan_budget_is_above_the_live_store():
    # 200,000 used to sit below the live object store (228,636 files), which
    # froze projection_object_bytes and projection_orphan_bytes below the real
    # totals while scan_complete reported False and nothing surfaced it.
    assert _max_projection_object_files() >= 2_000_000
    assert _max_projection_object_files() >= 228_636


def test_projection_object_scan_budget_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_STORAGE_OBJECT_SCAN_MAX_FILES", "500000")
    assert _max_projection_object_files() == 500_000

    monkeypatch.setenv("VECTOR_LAKE_STORAGE_OBJECT_SCAN_MAX_FILES", "7")
    assert _max_projection_object_files() == 1_000

    monkeypatch.setenv("VECTOR_LAKE_STORAGE_OBJECT_SCAN_MAX_FILES", "9" * 20)
    assert _max_projection_object_files() == 50_000_000

    monkeypatch.setenv("VECTOR_LAKE_STORAGE_OBJECT_SCAN_MAX_FILES", "bogus")
    assert _max_projection_object_files() == 2_000_000
