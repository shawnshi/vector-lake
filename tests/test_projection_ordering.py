import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from vector_lake import db_store, governance_store, indexer, mutation_coordinator
from vector_lake.watchdog_app import process_mutation_outbox_batch
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


def test_foreground_writer_waits_before_canonical_commit_of_same_page(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    first = _source_content().replace("Primary source content.", "Writer A.")
    second = _source_content().replace("Primary source content.", "Writer B.")
    entered = threading.Event()
    attempted = threading.Event()
    b_callback = threading.Event()
    release = threading.Event()
    original = mutation_coordinator.materialize_markdown_projection
    original_prepare = mutation_coordinator._prepare_mutations

    def prepare(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        if any(item["content"] == second for item in prepared):
            attempted.set()
        return prepared

    monkeypatch.setattr(mutation_coordinator, "_prepare_mutations", prepare)

    def held_materializer(filename, mutation_type, payload_text, **kwargs):
        if payload_text == first:
            entered.set()
            assert release.wait(5)
        return original(filename, mutation_type, payload_text, **kwargs)

    monkeypatch.setattr(mutation_coordinator, "materialize_markdown_projection", held_materializer)

    def writer(content):
        try:
            return mutation_coordinator.execute_mutation_batch(
                [{"filename": "Source_Test.md", "content": content}],
                canonical_callback=b_callback.set if content == second else None,
            )
        finally:
            db_store.close_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(writer, first)
        assert entered.wait(5)
        b = pool.submit(writer, second)
        assert attempted.wait(5)
        try:
            canonical = governance_store.canonical_page_versions({"Source_Test"})
            assert canonical
            assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 1
            assert not b_callback.wait(0.2), "B reached its canonical callback while A still owned the page"
            assert not b.done()
        finally:
            release.set()
        assert a.result(timeout=10)[0]
        assert b.result(timeout=10)[0]
        assert b_callback.is_set()
    assert (isolated_memory / "wiki" / "Source_Test.md").read_text(encoding="utf-8") == second


def test_old_foreground_materializer_cannot_overwrite_new_intent(isolated_memory):
    _write_purpose_contract(isolated_memory)
    a = _source_content().replace("Primary source content.", "Writer A.")
    b = _source_content().replace("Primary source content.", "Writer B.")
    mutation_coordinator.execute_mutation_plan("Source_Test.md", content=a)
    old_id = db_store.get_connection().execute("SELECT id FROM mutation_outbox").fetchone()[0]
    mutation_coordinator.execute_mutation_plan("Source_Test.md", content=b)
    with pytest.raises(db_store.MutationSuperseded):
        mutation_coordinator.materialize_markdown_projection("Source_Test.md", "update", a, outbox_id=old_id)
    assert (isolated_memory / "wiki" / "Source_Test.md").read_text(encoding="utf-8") == b


def test_canonical_change_outside_projection_protocol_fails_closed(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    content = _source_content()
    mutation_coordinator.execute_mutation_plan("Source_Test.md", content=content)
    target = isolated_memory / "wiki" / "Source_Test.md"
    target.unlink()
    conn = db_store.get_connection()
    row = conn.execute("SELECT entity_id,data_json FROM entities WHERE f_page_key='Source_Test'").fetchone()
    import json
    data = json.loads(row["data_json"])
    data["raw_text"] = "A different canonical revision."
    governance_store.upsert_entity(row["entity_id"], data)
    monkeypatch.setattr(indexer, "update_index_items", lambda names: None)
    stats = process_mutation_outbox_batch()
    assert not target.exists()
    assert stats["completed"] == 0 and stats["failed"] == 1
    assert conn.execute("SELECT status FROM mutation_outbox").fetchone()[0] == "failed"


def test_legacy_intent_is_not_silently_bound_to_current_canonical(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    mutation_coordinator.execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"
    target.unlink()
    with db_store.transaction() as conn:
        conn.execute("UPDATE mutation_outbox SET base_version=NULL")
    monkeypatch.setattr(indexer, "update_index_items", lambda names: None)
    stats = process_mutation_outbox_batch()
    assert not target.exists()
    assert stats["failed"] == 1


def test_index_failure_does_not_let_old_worker_fail_a_newer_intent(isolated_memory, monkeypatch):
    db_store.enqueue_mutation("Concept_Order.md", "delete", idempotency_key="old")

    def replace_then_fail(names):
        db_store.enqueue_mutation("Concept_Order.md", "delete", idempotency_key="new")
        raise OSError("synthetic index failure after newer intent")

    monkeypatch.setattr(indexer, "update_index_items", replace_then_fail)
    stats = process_mutation_outbox_batch()
    rows = db_store.get_connection().execute("SELECT status FROM mutation_outbox ORDER BY projection_generation").fetchall()
    assert [row[0] for row in rows] == ["superseded", "pending"]
    assert stats["completed"] == 0 and stats["retrying"] == 0


def test_callback_cannot_bind_old_payload_to_changed_canonical_page(isolated_memory):
    _write_purpose_contract(isolated_memory)

    def change_prepared_page():
        import json
        conn = db_store.get_connection()
        row = conn.execute("SELECT entity_id,data_json FROM entities WHERE f_page_key='Source_Test'").fetchone()
        data = json.loads(row["data_json"])
        data["raw_text"] = "Changed inside canonical callback."
        governance_store.upsert_entity(row["entity_id"], data)

    with pytest.raises(mutation_coordinator.ProjectionVersionConflict, match="callback"):
        mutation_coordinator.execute_mutation_batch(
            [{"filename": "Source_Test.md", "content": _source_content()}], canonical_callback=change_prepared_page,
        )
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 0
    assert not (isolated_memory / "wiki" / "Source_Test.md").exists()


def test_expired_worker_cannot_materialize_even_before_reclaim(isolated_memory):
    _write_purpose_contract(isolated_memory)
    content = _source_content()
    mutation_coordinator.execute_mutation_plan("Source_Test.md", content=content)
    claim = db_store.claim_mutation_outbox()[0]
    target = isolated_memory / "wiki" / "Source_Test.md"
    target.unlink()
    with db_store.transaction() as conn:
        conn.execute("UPDATE mutation_outbox SET lease_until='2000-01-01T00:00:00+00:00'")
    with pytest.raises(db_store.MutationClaimLost):
        mutation_coordinator.materialize_markdown_projection("Source_Test.md", "update", content, outbox_id=claim["id"], claim=claim)
    assert not target.exists()


def test_inherited_reentrant_tracker_does_not_bypass_physical_page_lock(isolated_memory, monkeypatch):
    import hashlib
    from filelock import FileLock, Timeout
    from vector_lake.wiki_utils import get_runtime_tmp_dir

    filename = "Source_Test.md"
    import os
    key = os.path.normcase(str((isolated_memory / "wiki" / filename).resolve()))
    root = get_runtime_tmp_dir() / "projection-locks"
    root.mkdir()
    path = root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock")
    monkeypatch.setattr(mutation_coordinator._PROJECTION_LOCAL, "held", {key}, raising=False)
    monkeypatch.setattr(mutation_coordinator._PROJECTION_LOCAL, "pid", -1, raising=False)
    monkeypatch.setattr(mutation_coordinator, "PROJECTION_LOCK_SECONDS", 0.05)
    with FileLock(str(path)):
        with pytest.raises(Timeout):
            with mutation_coordinator.projection_locks([filename]):
                raise AssertionError("foreign-process tracker bypassed the physical lock")


def test_outer_sql_transaction_cannot_publish_uncommitted_markdown(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    with pytest.raises(RuntimeError, match="outside a SQLite transaction"):
        with db_store.transaction():
            mutation_coordinator.execute_mutation_plan("Source_Test.md", content=_source_content())
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM mutation_outbox").fetchone()[0] == 0
    assert not (isolated_memory / "wiki" / "Source_Test.md").exists()


def test_page_lock_rejects_inverse_sql_lock_order(isolated_memory):
    db_store.init_db()
    with db_store.transaction():
        with pytest.raises(RuntimeError, match="outside a SQLite transaction"):
            with mutation_coordinator.projection_locks(["Source_Test.md"]):
                raise AssertionError("inverse lock ordering was accepted")


def test_inside_wiki_symlink_alias_cannot_mutate_another_page(isolated_memory):
    target = isolated_memory / "wiki" / "Source_Target.md"
    target.write_text("original", encoding="utf-8")
    alias = target.with_name("Source_Alias.md")
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not permit creating symlinks")
    with pytest.raises(ValueError, match="aliases"):
        mutation_coordinator.materialize_markdown_projection("Source_Alias.md", "update", "replacement", outbox_id=1)
    assert target.read_text(encoding="utf-8") == "original"


def test_case_folded_batch_cannot_own_one_physical_page_twice(isolated_memory):
    import os
    if os.path.normcase("A") != os.path.normcase("a"):
        pytest.skip("platform does not fold filename case")
    with pytest.raises(ValueError, match="same physical"):
        with mutation_coordinator.projection_locks(["Source_Case.md", "Source_CASE.md"]):
            raise AssertionError("two case aliases were accepted")


def test_existing_case_alias_is_not_a_second_canonical_page(isolated_memory):
    target = isolated_memory / "wiki" / "Source_Case.md"
    target.write_text("original", encoding="utf-8")
    variant = target.with_name("Source_CASE.md")
    if not variant.exists():
        pytest.skip("filesystem is case-sensitive")
    with pytest.raises(ValueError, match="aliases"):
        mutation_coordinator.resolve_wiki_mutation_path(variant.name)


def test_legacy_uppercase_extension_binds_actual_canonical_page_key(isolated_memory):
    _write_purpose_contract(isolated_memory)
    filename = "Source_Test.MD"
    target = isolated_memory / "wiki" / filename
    target.write_text(_source_content(), encoding="utf-8")
    mutation_coordinator.execute_mutation_batch([{"filename": filename, "content": _source_content()}], validation_mode="schema")
    row = db_store.get_connection().execute("SELECT * FROM mutation_outbox").fetchone()
    assert row["base_version"] == governance_store.canonical_page_versions({"Source_Test"})["Source_Test"]
    assert row["base_version"]


def test_deleted_legacy_filename_completes_and_removes_index_node(isolated_memory):
    import json
    from tests.test_mutation_coordinator import _named_source_content

    _write_purpose_contract(isolated_memory)
    filename = "Source_-Legacy.md"
    target = isolated_memory / "wiki" / filename
    target.write_text(_named_source_content("source_legacy_name", "Legacy Name"), encoding="utf-8")
    indexer.update_index_items([filename])
    index_path = isolated_memory / "wiki" / "index.json"
    old_index = json.loads(index_path.read_text(encoding="utf-8"))
    old_index["nodes"]["Source_-Legacy"] = {"id": "source_legacy_name", "title": "Legacy Name", "type": "source", "domain": "General"}
    index_path.write_text(json.dumps(old_index), encoding="utf-8")
    assert "Source_-Legacy" in json.loads(index_path.read_text(encoding="utf-8"))["nodes"]
    mutation_coordinator.execute_mutation_plan(filename, is_delete=True)
    assert not target.exists()
    stats = process_mutation_outbox_batch(backoff_base=0)
    assert stats["completed"] == 1 and stats["retrying"] == 0 and stats["failed"] == 0
    assert "Source_-Legacy" not in json.loads(index_path.read_text(encoding="utf-8"))["nodes"]
    assert db_store.get_connection().execute("SELECT status FROM mutation_outbox").fetchone()[0] == "completed"
