import hashlib

import pytest

from vector_lake import db_store, wiki_utils
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
    return db_store.get_connection().execute(
        "SELECT mutation_type, projection_base_hash, status FROM mutation_outbox "
        "WHERE filename = ? ORDER BY id DESC LIMIT 1",
        (filename,),
    ).fetchone()


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
