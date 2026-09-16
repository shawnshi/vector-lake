"""Regression guards for the canonical change-set entry points.

``create_change_set`` referenced an undefined ``existing_change_sets`` name, so
every call raised ``NameError`` inside its transaction.  That made the V8
bootstrap, ``migrate_existing_wiki(dry_run=False)`` and
``sync_pages_to_canonical`` unreachable at exactly the moment they are needed:
a knowledge base with wiki pages but an empty canonical store.
"""

import pytest

from vector_lake import db_store, governance_store

from tests.test_mutation_coordinator import _write_purpose_contract


def _concept(name: str, sources: list[str] = None) -> str:
    source_lines = "\n".join(f"  - {s}" for s in (sources or ["raw/original.md"]))
    return f"""---
id: {name.lower()}
title: {name}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
strategic_scope: core
evidence_tier: primary
topic_cluster: Test
updated: 2026-01-01
sources:
{source_lines}
---
## 1. 编译事实 (Compiled Truth - READ MODEL)
### 物理机制 (Mechanism)
A fact about {name}.

## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)
- [2026-01-01] [Observation] observed. (Source: [[Source_Original]])
"""


@pytest.fixture
def wiki_page(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    path = isolated_memory / "wiki" / "Concept_Dry.md"
    path.write_text(_concept("Concept_Dry"), encoding="utf-8")
    return path


def test_create_change_set_does_not_raise_name_error(wiki_page):
    change_set = governance_store.create_change_set([str(wiki_page)], origin="probe", auto_approve=True)

    assert change_set["change_set_id"].startswith("changeset_")
    assert len(governance_store.load_change_sets()["items"]) == 1


def test_create_change_set_dry_run_persists_nothing(wiki_page):
    preview = governance_store.create_change_set(
        [str(wiki_page)], origin="probe", auto_approve=True, dry_run=True
    )

    assert preview["dry_run"] is True
    assert preview["status"] == "dry-run"
    assert governance_store.load_change_sets()["items"] == []
    assert governance_store.load_entities()["items"] == {}


def test_ensure_canonical_store_populated_bootstraps_once(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    (isolated_memory / "wiki" / "Concept_Boot.md").write_text(
        _concept("Concept_Boot"), encoding="utf-8"
    )

    first = governance_store.ensure_canonical_store_populated()

    assert first.get("bootstrapped") is True
    assert "change_set_id" in first
    assert len(governance_store.load_entities()["items"]) == 1

    second = governance_store.ensure_canonical_store_populated()
    assert second.get("bootstrapped") is False


def test_migrate_existing_wiki_non_dry_run_completes(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    (isolated_memory / "wiki" / "Concept_Migrate.md").write_text(
        _concept("Concept_Migrate"), encoding="utf-8"
    )

    result = governance_store.migrate_existing_wiki(dry_run=False)

    assert result["pages_scanned"] == 1
    assert result["entities"] == 1


def test_sync_pages_to_canonical_accepts_a_bare_basename(isolated_memory, monkeypatch):
    """A relative basename used to be read as "file deleted" and dropped the entity."""
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    (isolated_memory / "wiki" / "Concept_Name.md").write_text(_concept("Concept_Name"), encoding="utf-8")

    governance_store.ensure_canonical_store_populated()
    assert len(governance_store.load_entities()["items"]) == 1

    governance_store.sync_pages_to_canonical(["Concept_Name.md"], origin="probe")

    assert len(governance_store.load_entities()["items"]) == 1, (
        "a relative basename was treated as a deleted page"
    )
    assert (isolated_memory / "wiki" / "Concept_Name.md").exists()


def test_sync_pages_to_canonical_still_handles_a_real_deletion(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    path = isolated_memory / "wiki" / "Concept_Gone.md"
    path.write_text(_concept("Concept_Gone"), encoding="utf-8")

    governance_store.ensure_canonical_store_populated()
    assert len(governance_store.load_entities()["items"]) == 1

    path.unlink()
    governance_store.sync_pages_to_canonical([str(path)], origin="probe")

    assert governance_store.load_entities()["items"] == {}


def test_change_set_apply_deletes_both_edge_directions(isolated_memory, monkeypatch):
    """Editing a page must not leave claim edges pointing at its removed claims."""
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    page = isolated_memory / "wiki" / "Concept_Edges.md"
    page.write_text(_concept("Concept_Edges"), encoding="utf-8")
    governance_store.ensure_canonical_store_populated()

    conn = db_store.get_connection()
    peer_claim = conn.execute("SELECT claim_id FROM claims LIMIT 1").fetchone()["claim_id"]
    with db_store.transaction():
        # An edge owned by another page pointing *at* this page's claim.
        conn.execute(
            "INSERT OR REPLACE INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("page:OTHER#1", peer_claim, "depends-on", 1.0, governance_store._utc_now()),
        )
    # Rewrite the page so its claim ids change, then re-apply the change set.
    page.write_text(_concept("Concept_Edges").replace("A fact about", "An updated fact about"), encoding="utf-8")
    governance_store.sync_pages_to_canonical([str(page)], origin="probe", auto_approve=True)

    dangling = conn.execute(
        "SELECT COUNT(*) FROM claim_graph_edges WHERE target_id = ?", (peer_claim,)
    ).fetchone()[0]
    assert dangling == 0, "a claim edge survived the removal of its target claim"
