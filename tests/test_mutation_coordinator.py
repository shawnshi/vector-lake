import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from vector_lake import db_store, governance_store, indexer
from vector_lake.mutation_coordinator import execute_mutation_batch, execute_mutation_plan
from vector_lake.wiki_utils import atomic_write_text


def _write_purpose_contract(memory_dir):
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.0"
intent_keywords: [test]
intent_weight_boost: 0.1
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
  derived: Derived operational evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
""",
        encoding="utf-8",
    )


def _source_content():
    return """---
id: source_test
title: Test Source
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Source]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/test.pdf]
strategic_scope: core
evidence_tier: primary
---
Primary source content.
"""


def _named_source_content(entity_id: str, title: str, body: str = "Primary source content."):
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Source]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/test.pdf]
strategic_scope: core
evidence_tier: primary
---
{body}
"""


@pytest.mark.parametrize("filename", ["../escape.md", "C:\\escape.md", "System_/../../escape.md", "subdir/Concept_Test.md"])
def test_mutation_rejects_paths_outside_wiki(isolated_memory, filename):
    _write_purpose_contract(isolated_memory)
    with pytest.raises(ValueError, match="filename|path|boundary|basename"):
        execute_mutation_plan(filename, content=_source_content())
    assert not (isolated_memory.parent / "escape.md").exists()


def test_atomic_write_preserves_mixed_newline_bytes(isolated_memory):
    target = isolated_memory / "scratch" / "mixed-newlines.txt"
    content = "frontmatter\nbody-crlf\r\nnext-cr\rlast\n"

    atomic_write_text(target, content)

    assert target.read_bytes() == content.encode("utf-8")


def test_atomic_write_projection_compare_and_swap_preserves_manual_edit(
    isolated_memory,
):
    target = isolated_memory / "scratch" / "projection-cas.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("known base", encoding="utf-8")
    expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text("manual edit", encoding="utf-8")

    with pytest.raises(RuntimeError, match="compare-and-swap conflict"):
        atomic_write_text(
            target,
            "replacement",
            expected_current_hash=expected_hash,
        )

    assert target.read_text(encoding="utf-8") == "manual edit"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Source_ALPHA.md", "Source_alpha.md"),
        ("Source_Café.md", "Source_Cafe\u0301.md"),
    ],
)
def test_mutation_batch_rejects_case_or_unicode_equivalent_filenames(
    isolated_memory,
    first,
    second,
):
    _write_purpose_contract(isolated_memory)
    wiki_dir = isolated_memory / "wiki"
    wiki_dir.mkdir(exist_ok=True)
    (wiki_dir / first).write_text("legacy projection", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate filenames"):
        execute_mutation_batch(
            [
                {"filename": first, "content": _source_content()},
                {"filename": second, "content": _source_content()},
            ],
            validation_mode="schema",
        )


@pytest.mark.parametrize(
    ("existing", "requested"),
    [
        ("Source_alpha.md", "Source_ALPHA.md"),
        ("Source_Café.md", "Source_Cafe\u0301.md"),
    ],
)
def test_mutation_rejects_case_or_unicode_alias_of_existing_page(
    isolated_memory,
    existing,
    requested,
):
    _write_purpose_contract(isolated_memory)
    wiki_dir = isolated_memory / "wiki"
    wiki_dir.mkdir(exist_ok=True)
    (wiki_dir / existing).write_text("legacy projection", encoding="utf-8")

    with pytest.raises(ValueError, match="alias of existing"):
        execute_mutation_batch(
            [{"filename": requested, "content": _source_content()}],
            validation_mode="schema",
        )


def test_mutation_commits_canonical_and_durable_intent_before_projection(isolated_memory):
    _write_purpose_contract(isolated_memory)

    ok, message = execute_mutation_plan("Source_Test.md", content=_source_content())

    assert ok is True
    assert "committed" in message.lower()
    target = isolated_memory / "wiki" / "Source_Test.md"
    assert target.read_text(encoding="utf-8") == _source_content()
    conn = db_store.get_connection()
    entity = conn.execute(
        "SELECT data_json FROM entities WHERE json_extract(data_json, '$.page_key') = 'Source_Test'"
    ).fetchone()
    assert entity is not None
    outbox = conn.execute(
        "SELECT status, payload_text FROM mutation_outbox WHERE filename = 'Source_Test.md'"
    ).fetchone()
    assert outbox["status"] == "pending"
    assert outbox["payload_text"] == _source_content()


def test_mutation_rolls_back_canonical_when_outbox_enqueue_fails(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(db_store, "enqueue_mutation", fail_enqueue)
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        execute_mutation_plan("Source_Test.md", content=_source_content())

    conn = db_store.get_connection()
    assert conn.execute("SELECT 1 FROM entities").fetchone() is None
    assert not (isolated_memory / "wiki" / "Source_Test.md").exists()


def test_mutation_rejects_interleaved_canonical_update_at_commit_boundary(
    isolated_memory,
    monkeypatch,
):
    _write_purpose_contract(isolated_memory)
    original = _source_content()
    execute_mutation_plan("Source_Test.md", content=original)
    expected = governance_store.canonical_page_versions({"Source_Test"})["Source_Test"]
    outbox_before = db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0]
    real_prepare = governance_store.prepare_change_set_from_content
    injected = False

    def prepare_then_inject_concurrent_update(*args, **kwargs):
        nonlocal injected
        change_set = real_prepare(*args, **kwargs)
        if not injected:
            injected = True
            row = db_store.get_connection().execute(
                "SELECT entity_id, data_json FROM entities "
                "WHERE json_extract(data_json, '$.page_key') = 'Source_Test' LIMIT 1"
            ).fetchone()
            data = json.loads(row["data_json"])
            data["raw_text"] = "Concurrent canonical update."
            governance_store.upsert_entity(row["entity_id"], data)
        return change_set

    monkeypatch.setattr(
        governance_store,
        "prepare_change_set_from_content",
        prepare_then_inject_concurrent_update,
    )
    with pytest.raises(ValueError, match="Canonical version conflict"):
        execute_mutation_batch([
            {
                "filename": "Source_Test.md",
                "content": original.replace("Primary source content.", "Desired update."),
                "expected_version": expected,
            }
        ])

    assert injected is True
    assert (isolated_memory / "wiki" / "Source_Test.md").read_text(encoding="utf-8") == original
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM mutation_outbox"
    ).fetchone()[0] == outbox_before
    current = db_store.get_connection().execute(
        "SELECT data_json FROM entities "
        "WHERE json_extract(data_json, '$.page_key') = 'Source_Test' LIMIT 1"
    ).fetchone()
    assert json.loads(current["data_json"])["raw_text"] == "Concurrent canonical update."


def test_two_sqlite_writers_with_same_expected_version_cannot_both_commit(isolated_memory):
    _write_purpose_contract(isolated_memory)
    original = _source_content()
    execute_mutation_plan("Source_Test.md", content=original)
    expected = governance_store.canonical_page_versions({"Source_Test"})["Source_Test"]
    barrier = threading.Barrier(2)

    def race(body):
        barrier.wait(timeout=5)
        try:
            execute_mutation_batch([{
                "filename": "Source_Test.md",
                "content": original.replace("Primary source content.", body),
                "expected_version": expected,
            }])
            return "committed"
        except ValueError as exc:
            assert "Canonical version conflict" in str(exc)
            return "conflict"
        finally:
            db_store.close_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, ["Writer A.", "Writer B."]))

    assert sorted(results) == ["committed", "conflict"]
    projection = (isolated_memory / "wiki" / "Source_Test.md").read_text(encoding="utf-8")
    assert ("Writer A." in projection) != ("Writer B." in projection)


def test_mutation_batch_rolls_back_all_pages_and_callback(isolated_memory):
    _write_purpose_contract(isolated_memory)
    left_original = _named_source_content("source_left", "Left Source")
    right_original = _named_source_content("source_right", "Right Source")
    execute_mutation_plan("Source_Left.md", content=left_original)
    execute_mutation_plan("Source_Right.md", content=right_original)
    conn = db_store.get_connection()
    conn.execute("DELETE FROM mutation_outbox")
    conn.commit()
    indexer.refresh_claim_graph_projection()

    def fail_callback():
        raise RuntimeError("injected registry failure")

    with pytest.raises(RuntimeError, match="injected registry failure"):
        execute_mutation_batch(
            [
                {
                    "filename": "Source_Left.md",
                    "content": _named_source_content("source_left", "Left Source", "Revised."),
                },
                {"filename": "Source_Right.md", "is_delete": True},
            ],
            canonical_callback=fail_callback,
        )

    assert (isolated_memory / "wiki" / "Source_Left.md").read_text(encoding="utf-8") == left_original
    assert (isolated_memory / "wiki" / "Source_Right.md").read_text(encoding="utf-8") == right_original
    assert conn.execute(
        "SELECT 1 FROM entities WHERE json_extract(data_json, '$.page_key') = 'Source_Right'"
    ).fetchone() is not None
    assert conn.execute("SELECT 1 FROM mutation_outbox").fetchone() is None


def test_mutation_batch_updates_derived_state_without_full_rebuild(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    alias_calls = 0
    memory_calls = 0
    real_alias_rebuild = governance_store.rebuild_alias_registry
    real_memory_rebuild = governance_store.rebuild_operational_memory

    def count_alias_rebuild():
        nonlocal alias_calls
        alias_calls += 1
        return real_alias_rebuild()

    def count_memory_rebuild():
        nonlocal memory_calls
        memory_calls += 1
        return real_memory_rebuild()

    monkeypatch.setattr(governance_store, "rebuild_alias_registry", count_alias_rebuild)
    monkeypatch.setattr(governance_store, "rebuild_operational_memory", count_memory_rebuild)
    execute_mutation_batch(
        [
            {
                "filename": "Source_Left.md",
                "content": _named_source_content("source_left", "Left Source"),
            },
            {
                "filename": "Source_Right.md",
                "content": _named_source_content("source_right", "Right Source"),
            },
        ]
    )

    assert alias_calls == 0
    assert memory_calls == 0
    conn = db_store.get_connection()
    alias_row = conn.execute("SELECT value FROM alias_registry WHERE key = 'Left Source'").fetchone()
    assert alias_row is not None
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id = ?", (alias_row[0],)).fetchone() is not None
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] > 0


def test_page_update_preserves_merge_identity_redirect(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_batch(
        [{"filename": "Source_Left.md", "content": _named_source_content("source_left", "Left Source")}]
    )
    conn = db_store.get_connection()
    target_id = conn.execute(
        "SELECT entity_id FROM entities WHERE json_extract(data_json, '$.page_key') = 'Source_Left'"
    ).fetchone()[0]
    governance_store.upsert_alias("entity_deleted_source", target_id)

    execute_mutation_batch(
        [
            {
                "filename": "Source_Left.md",
                "content": _named_source_content("source_left", "Left Source", "Updated body."),
            }
        ]
    )

    assert governance_store.get_alias("entity_deleted_source") == target_id


def test_page_scoped_mutation_does_not_rewrite_unrelated_canonical_rows(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_batch(
        [
            {"filename": "Source_Left.md", "content": _named_source_content("source_left", "Left Source")},
            {"filename": "Source_Right.md", "content": _named_source_content("source_right", "Right Source")},
        ]
    )
    conn = db_store.get_connection()
    before = {
        table: conn.execute(
            f"SELECT updated_at, data_json FROM {table} WHERE json_extract(data_json, '$.locator.page_key') = 'Source_Right' LIMIT 1"
            if table in {"claims", "evidence"}
            else f"SELECT updated_at, data_json FROM {table} WHERE json_extract(data_json, '$.page_key') = 'Source_Right' LIMIT 1"
        ).fetchone()
        for table in ("entities", "claims", "evidence")
    }

    execute_mutation_plan(
        "Source_Left.md",
        content=_named_source_content("source_left", "Left Source", "Revised left content."),
    )

    for table, old_row in before.items():
        assert old_row is not None
        new_row = conn.execute(
            f"SELECT updated_at, data_json FROM {table} WHERE json_extract(data_json, '$.locator.page_key') = 'Source_Right' LIMIT 1"
            if table in {"claims", "evidence"}
            else f"SELECT updated_at, data_json FROM {table} WHERE json_extract(data_json, '$.page_key') = 'Source_Right' LIMIT 1"
        ).fetchone()
        assert tuple(new_row) == tuple(old_row)


def test_record_prepared_change_sets_does_not_load_full_history(isolated_memory, monkeypatch):
    db_store.init_db()
    change_set = {
        "change_set_id": "changeset_delta",
        "idempotency_key": "delta-key",
        "status": "published",
    }
    monkeypatch.setattr(governance_store, "load_change_sets", lambda: (_ for _ in ()).throw(AssertionError("full history load")))

    assert governance_store.record_prepared_change_sets([change_set]) == 1
    assert governance_store.record_prepared_change_sets([change_set]) == 0
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 1


def test_schema_validation_mode_allows_bounded_legacy_maintenance(isolated_memory):
    _write_purpose_contract(isolated_memory)
    legacy_content = _named_source_content("source_legacy", "Legacy Source").replace(
        "evidence_tier: primary\n",
        "",
    )

    with pytest.raises(Exception, match="evidence_tier"):
        execute_mutation_plan("Source_Legacy.md", content=legacy_content)

    ok, message = execute_mutation_batch(
        [{"filename": "Source_Legacy.md", "content": legacy_content}],
        validation_mode="schema",
    )

    assert ok is True
    assert "committed" in message.lower()
    assert (isolated_memory / "wiki" / "Source_Legacy.md").exists()
    row = db_store.get_connection().execute(
        "SELECT validation_mode FROM mutation_outbox WHERE filename = 'Source_Legacy.md'"
    ).fetchone()
    assert row["validation_mode"] == "schema"


def test_schema_maintenance_preserves_legacy_tag_entity_collision(isolated_memory):
    _write_purpose_contract(isolated_memory)
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text(
        json.dumps({"nodes": {"Concept_Agentic-AI": {"title": "Agentic AI", "aliases": []}}}),
        encoding="utf-8",
    )
    legacy_content = _named_source_content("source_tag_collision", "Tag Collision").replace(
        "categories: [Source]\n",
        "categories: [Source]\ntags: [Agentic AI]\n",
    )

    ok, message = execute_mutation_batch(
        [{"filename": "Source_Tag-Collision.md", "content": legacy_content}],
        validation_mode="schema",
    )

    assert ok is True
    assert "committed" in message.lower()
    assert (isolated_memory / "wiki" / "Source_Tag-Collision.md").read_text(encoding="utf-8") == legacy_content


def test_mutation_batch_rejects_unknown_validation_mode(isolated_memory):
    with pytest.raises(ValueError, match="validation_mode"):
        execute_mutation_batch([], validation_mode="bypass")


def test_schema_mode_updates_existing_legacy_filename_but_cannot_create_one(isolated_memory):
    _write_purpose_contract(isolated_memory)
    legacy_name = "Source_-Legacy.md"
    legacy_path = isolated_memory / "wiki" / legacy_name
    content = _named_source_content("source_legacy_name", "Legacy Name")
    legacy_path.write_text(content, encoding="utf-8")

    updated = content.replace("Primary source content.", "Updated source content.")
    execute_mutation_batch(
        [{"filename": legacy_name, "content": updated}],
        validation_mode="schema",
    )
    assert legacy_path.read_text(encoding="utf-8") == updated

    legacy_path.unlink()
    with pytest.raises(ValueError, match="Naming"):
        execute_mutation_batch(
            [{"filename": legacy_name, "content": updated}],
            validation_mode="schema",
        )
