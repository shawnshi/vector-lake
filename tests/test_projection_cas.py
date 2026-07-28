import hashlib
import threading

import pytest

from vector_lake import db_store, governance_store, mutation_coordinator, wiki_utils
from vector_lake.mutation_coordinator import (
    execute_mutation_batch,
    execute_mutation_plan,
    materialize_markdown_projection,
)
from tests.test_mutation_coordinator import (
    _source_content,
    _write_purpose_contract,
)


def _latest_outbox(filename: str):
    return (
        db_store.get_connection()
        .execute(
            "SELECT mutation_type, projection_base_hash, status FROM mutation_outbox "
            "WHERE filename = ? ORDER BY id DESC LIMIT 1",
            (filename,),
        )
        .fetchone()
    )


def test_create_projection_uses_absence_cas_and_preserves_concurrent_file(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    target = isolated_memory / "wiki" / "Source_Test.md"

    def concurrent_create(_outbox_ids):
        target.write_text("manual concurrent projection", encoding="utf-8")

    result = execute_mutation_batch(
        [{"filename": target.name, "content": _source_content()}],
        transaction_callback=concurrent_create,
        return_details=True,
    )

    assert result["deferred"] == [target.name]
    assert target.read_text(encoding="utf-8") == "manual concurrent projection"
    row = _latest_outbox(target.name)
    assert row["projection_base_hash"] == ""
    assert row["status"] == "pending"


def test_update_projection_cas_is_automatic_and_preserves_concurrent_edit(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    original = _source_content()
    desired = original.replace("Primary source content.", "Canonical update.")
    execute_mutation_plan("Source_Test.md", content=original)
    target = isolated_memory / "wiki" / "Source_Test.md"
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    def concurrent_edit(_outbox_ids):
        target.write_text("manual concurrent projection", encoding="utf-8")

    result = execute_mutation_batch(
        [{"filename": target.name, "content": desired}],
        transaction_callback=concurrent_edit,
        return_details=True,
    )

    assert result["deferred"] == [target.name]
    assert target.read_text(encoding="utf-8") == "manual concurrent projection"
    row = _latest_outbox(target.name)
    assert row["projection_base_hash"] == base_hash
    assert row["status"] == "pending"


def test_explicit_projection_hash_conflict_rolls_back_canonical_batch(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    original = _source_content()
    desired = original.replace("Primary source content.", "Derived stale update.")
    execute_mutation_plan("Source_Test.md", content=original)
    target = isolated_memory / "wiki" / "Source_Test.md"
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    version_before = governance_store.canonical_page_versions({"Source_Test"})[
        "Source_Test"
    ]
    outbox_before = (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM mutation_outbox")
        .fetchone()[0]
    )
    concurrent_content = "manual concurrent projection"

    def inject_concurrent_edit():
        target.write_text(concurrent_content, encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="Projection changed before canonical mutation commit",
    ):
        execute_mutation_batch(
            [
                {
                    "filename": target.name,
                    "content": desired,
                    "expected_projection_hash": base_hash,
                }
            ],
            precondition_callback=inject_concurrent_edit,
        )

    assert target.read_text(encoding="utf-8") == concurrent_content
    assert (
        governance_store.canonical_page_versions({"Source_Test"})["Source_Test"]
        == version_before
    )
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM mutation_outbox")
        .fetchone()[0]
        == outbox_before
    )


def test_concurrent_unversioned_updates_rebind_projection_hash_in_transaction(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    original = _source_content()
    content_a = original.replace("Primary source content.", "Writer A.")
    content_b = original.replace("Primary source content.", "Writer B.")
    execute_mutation_plan("Source_Test.md", content=original)
    target = isolated_memory / "wiki" / "Source_Test.md"
    both_prepared = threading.Barrier(2)
    a_projected = threading.Event()
    real_prepare = mutation_coordinator._prepare_mutations
    real_materialize = mutation_coordinator.materialize_markdown_projection

    def gated_prepare(*args, **kwargs):
        prepared = real_prepare(*args, **kwargs)
        both_prepared.wait(timeout=5)
        if any(
            mutation.get("content") and "Writer B." in mutation["content"]
            for mutation in prepared
        ):
            assert a_projected.wait(timeout=5)
        return prepared

    def recording_materialize(
        filename,
        mutation_type,
        payload_text=None,
        validation_mode="full",
        projection_base_hash=None,
    ):
        result = real_materialize(
            filename,
            mutation_type,
            payload_text,
            validation_mode=validation_mode,
            projection_base_hash=projection_base_hash,
        )
        if payload_text and "Writer A." in payload_text:
            a_projected.set()
        return result

    monkeypatch.setattr(
        mutation_coordinator,
        "_prepare_mutations",
        gated_prepare,
    )
    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        recording_materialize,
    )
    results = {}
    errors = {}

    def run_update(label, content):
        try:
            results[label] = execute_mutation_batch(
                [{"filename": target.name, "content": content}],
                return_details=True,
            )
        except Exception as exc:
            errors[label] = exc
        finally:
            db_store.close_connection()

    thread_a = threading.Thread(target=run_update, args=("a", content_a))
    thread_b = threading.Thread(target=run_update, args=("b", content_b))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == {}
    assert results["a"]["deferred"] == []
    assert results["b"]["deferred"] == []
    assert target.read_text(encoding="utf-8") == content_b
    latest = _latest_outbox(target.name)
    assert (
        latest["projection_base_hash"]
        == hashlib.sha256(content_a.encode("utf-8")).hexdigest()
    )


def test_delete_projection_cas_is_automatic_and_preserves_concurrent_edit(
    isolated_memory,
):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    def concurrent_edit(_outbox_ids):
        target.write_text("manual concurrent projection", encoding="utf-8")

    result = execute_mutation_batch(
        [{"filename": target.name, "is_delete": True}],
        transaction_callback=concurrent_edit,
        return_details=True,
    )

    assert result["deferred"] == [target.name]
    assert target.read_text(encoding="utf-8") == "manual concurrent projection"
    row = _latest_outbox(target.name)
    assert row["mutation_type"] == "delete"
    assert row["projection_base_hash"] == base_hash
    assert row["status"] == "pending"


def test_delete_projection_cas_restores_change_injected_before_displacement(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    target = isolated_memory / "wiki" / "Source_Test.md"
    target.write_text(_source_content(), encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    concurrent_content = "manual edit injected between hash and displacement"
    original_replace = wiki_utils.os.replace
    injected = False

    def replace_after_concurrent_edit(source, destination):
        nonlocal injected
        if not injected and source == target:
            injected = True
            target.write_text(concurrent_content, encoding="utf-8")
        return original_replace(source, destination)

    monkeypatch.setattr(wiki_utils.os, "replace", replace_after_concurrent_edit)

    with pytest.raises(RuntimeError, match="compare-and-swap conflict"):
        materialize_markdown_projection(
            target.name,
            "delete",
            projection_base_hash=base_hash,
        )

    assert injected is True
    assert target.read_text(encoding="utf-8") == concurrent_content
    assert not tuple(target.parent.glob(f"{target.name}.*.cas-delete"))


def test_projection_retry_is_idempotent_after_successful_update(isolated_memory):
    _write_purpose_contract(isolated_memory)
    target = isolated_memory / "wiki" / "Source_Test.md"
    original = _source_content()
    desired = original.replace("Primary source content.", "Desired projection.")
    target.write_text(original, encoding="utf-8")
    base_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    materialize_markdown_projection(
        target.name,
        "update",
        desired,
        projection_base_hash=base_hash,
    )
    materialize_markdown_projection(
        target.name,
        "update",
        desired,
        projection_base_hash=base_hash,
    )

    assert target.read_text(encoding="utf-8") == desired
