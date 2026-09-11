import hashlib

from vector_lake import auto_ingest_worker


def test_filename_prefixed_error_preserves_subject_and_actionable_reason():
    fields = auto_ingest_worker._receipt_error_fields(
        "Policy__.md: categories must be a list with exactly one domain."
    )

    assert fields["error_subject"] == "Policy__.md"
    assert fields["error_code"] == "categories_must_be_a_list_with_exactly_one_domain."


def test_legacy_colon_error_is_unchanged():
    fields = auto_ingest_worker._receipt_error_fields(
        "codex_event_log_type_is_not_allowed:error"
    )

    assert fields["error_code"] == "codex_event_log_type_is_not_allowed"
    assert fields["error_subject"] == ""


def test_plain_error_is_unchanged():
    fields = auto_ingest_worker._receipt_error_fields("plain error")

    assert fields["error_code"] == "plain_error"
    assert fields["error_subject"] == ""


def test_md_suffixed_error_without_colon_is_unchanged():
    text = "Canonical_version_conflict_for_Concept_AI_.md"
    fields = auto_ingest_worker._receipt_error_fields(text)

    assert fields["error_code"] == text
    assert fields["error_subject"] == ""


def test_colon_error_with_non_wiki_head_is_unchanged():
    fields = auto_ingest_worker._receipt_error_fields("not a wiki.md: actionable reason")

    assert fields["error_code"] == "not_a_wiki.md"
    assert fields["error_subject"] == ""


def test_reason_sanitization_and_truncation_are_bounded():
    fields = auto_ingest_worker._receipt_error_fields(
        f"Concept__.md: {'bad value!?/' * 30}"
    )

    assert fields["error_subject"] == "Concept__.md"
    assert len(fields["error_code"]) == 120
    assert set(fields["error_code"]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
    )
    assert "_" in fields["error_code"]


def test_filename_prefixed_error_fingerprint_uses_original_text():
    text = "Policy__.md: categories must be a list with exactly one domain."
    fields = auto_ingest_worker._receipt_error_fields(text)

    expected = hashlib.sha256(f"Error\0{text}".encode("utf-8")).hexdigest()
    assert fields["error_fingerprint"] == expected


def test_none_error_has_empty_diagnostic_fields():
    fields = auto_ingest_worker._receipt_error_fields(None)

    assert fields["error_code"] == ""
    assert fields["error_subject"] == ""
    assert fields["error_fingerprint"] == ""


def test_stage_event_merges_subject_without_mutating_metadata(monkeypatch):
    captured = {}
    metadata = {"source": "caller"}
    original = dict(metadata)

    monkeypatch.setattr(
        auto_ingest_worker.db_store,
        "record_ingest_stage_event",
        lambda **kwargs: captured.update(kwargs),
    )

    auto_ingest_worker._record_ingest_stage_event_safe(
        {"job_id": "job", "hash": "revision", "lease_generation": 2},
        stage="finalization",
        transition="failed",
        error="Policy__.md: categories must be a list",
        metadata=metadata,
    )

    assert metadata == original
    assert captured["metadata"] == {
        "source": "caller",
        "error_subject": "Policy__.md",
    }
    assert captured["metadata"] is not metadata


def test_stage_event_keeps_caller_supplied_subject(monkeypatch):
    captured = {}
    metadata = {"error_subject": "caller-subject"}

    monkeypatch.setattr(
        auto_ingest_worker.db_store,
        "record_ingest_stage_event",
        lambda **kwargs: captured.update(kwargs),
    )

    auto_ingest_worker._record_ingest_stage_event_safe(
        {"job_id": "job", "hash": "revision"},
        stage="finalization",
        transition="failed",
        error="Policy__.md: categories must be a list",
        metadata=metadata,
    )

    assert captured["metadata"]["error_subject"] == "caller-subject"


def test_stage_event_passes_metadata_through_when_subject_is_empty(monkeypatch):
    captured = {}
    metadata = {"source": "caller"}

    monkeypatch.setattr(
        auto_ingest_worker.db_store,
        "record_ingest_stage_event",
        lambda **kwargs: captured.update(kwargs),
    )

    auto_ingest_worker._record_ingest_stage_event_safe(
        {"job_id": "job", "hash": "revision"},
        stage="finalization",
        transition="failed",
        error="plain error",
        metadata=metadata,
    )

    assert captured["metadata"] is metadata
