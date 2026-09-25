"""Pointers that name a claim nobody holds.

Both surfaces here were silent: ``evidence.supports_claim_ids`` named claims a bulk pass had
retired (25 220 live pointers, all but 6 from one 2026-07-14 event), and the
``claim_graph_edges`` delete filtered a page-key table by claim ids, so it matched nothing.
These tests pin the report and the repair, not the sizes.
"""

import json

from vector_lake import db_store
from vector_lake.tool_maintenance import claim_projection_drift_report, repair_claim_pointers


def _evidence(conn, evidence_id: str, supports: list[str], updated_at: str = "2026-07-14T00:00:00+00:00"):
    with db_store.transaction():
        conn.execute(
            "INSERT INTO evidence (evidence_id, data_json, updated_at) VALUES (?, ?, ?)",
            (
                evidence_id,
                json.dumps({
                    "evidence_id": evidence_id,
                    "evidence_text": "text",
                    "supports_claim_ids": supports,
                    "contradicts_claim_ids": [],
                }),
                updated_at,
            ),
        )


def _claim(conn, claim_id: str):
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                claim_id,
                "a claim",
                "active",
                json.dumps({
                    "claim_id": claim_id,
                    "claim_text": "a claim",
                    "claim_type": "assertion",
                    "status": "active",
                    "locator": {"page_key": "Concept_Alpha"},
                }),
                "2026-07-14T00:00:00+00:00",
            ),
        )


def _node(conn, node_key: str, title: str, aliases: list[str] | None = None):
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO page_index_nodes (node_key, title, node_json) VALUES (?, ?, ?)",
            (node_key, title, json.dumps({"title": title, "aliases": aliases or []})),
        )


def _edge(conn, source: str, target: str, relation: str = "related-to"):
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO claim_graph_edges "
            "(source_id, target_id, relation, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
            (source, target, relation, 1.0, "2026-07-14T00:00:00+00:00"),
        )


def test_drift_report_counts_pointers_naming_a_missing_claim(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _claim(conn, "claim_alive")
    _evidence(conn, "evidence_ok", ["claim_alive"])
    _evidence(conn, "evidence_dead", ["claim_alive", "claim_retired"])
    _node(conn, "Concept_Alpha", "Alpha")
    _edge(conn, "Concept_Alpha", "Concept_Alpha")

    report = claim_projection_drift_report()

    assert "evidence rows naming a missing claim: 1" in report
    assert "dead pointer(s): 1" in report
    assert "rows with an unresolved endpoint: 0" in report


def test_repair_prunes_the_dead_pointer_and_leaves_the_live_one(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _claim(conn, "claim_alive")
    _evidence(conn, "evidence_dead", ["claim_alive", "claim_retired"])

    assert "Would prune 1 dead claim pointer" in repair_claim_pointers(dry_run=True)
    assert repair_claim_pointers(dry_run=False).startswith("Pruned 1 dead claim pointer")

    row = conn.execute(
        "SELECT data_json, updated_at FROM evidence WHERE evidence_id = 'evidence_dead'"
    ).fetchone()
    assert json.loads(row["data_json"])["supports_claim_ids"] == ["claim_alive"]
    # Dropping a pointer is not new evidence: the freshness clock must not move.
    assert row["updated_at"] == "2026-07-14T00:00:00+00:00"
    assert "evidence rows naming a missing claim: 0" in claim_projection_drift_report()

    from vector_lake.tool_maintenance import _rollback_path

    lines = [line for line in _rollback_path().read_text(encoding="utf-8").splitlines() if line.strip()]
    assert json.loads(lines[-1])["supports_claim_ids"] == ["claim_retired"]


def test_repair_rekeys_a_resolvable_edge_and_leaves_the_rest(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _node(conn, "Product_CoMET", "CoMET")
    _node(conn, "Concept_Alpha", "Alpha")
    _edge(conn, "Concept_Alpha", "Concept_CoMET")          # core name -> Product_CoMET
    _edge(conn, "Concept_Alpha", "Nothing_Declares_This")   # no page answers it
    _edge(conn, "Source_Retired", "Product_CoMET")          # the *source* is a retired page

    report = claim_projection_drift_report()
    assert "rows with an unresolved endpoint: 3" in report
    assert "of which re-keyable by target: 1" in report

    result = repair_claim_pointers(dry_run=False, edges=True)
    assert "re-keyed 1 edge row(s)" in result
    rows = {
        (row["source_id"], row["target_id"])
        for row in conn.execute("SELECT source_id, target_id FROM claim_graph_edges")
    }
    assert ("Concept_Alpha", "Product_CoMET") in rows
    assert ("Concept_Alpha", "Concept_CoMET") not in rows
    # An endpoint no page answers, and a retired *source*, are both left alone: the target is
    # the only side the link rules are allowed to re-key.
    assert ("Concept_Alpha", "Nothing_Declares_This") in rows
    assert ("Source_Retired", "Product_CoMET") in rows


_EDGE_FRONTMATTER = {
    "id": "source_edge",
    "title": "Edge source",
    "type": "concept",
    "domain": "General",
    "status": "Active",
    "epistemic-status": "seed",
    "categories": ["System_Architecture"],
    "strategic_scope": "core",
    "evidence_tier": "primary",
    "topic_cluster": "Test",
    "updated": "2026-01-01",
    "sources": ["raw/a.md"],
}
_EDGE_BODY = (
    "## 1. 编译事实\n[validates:: [[Concept_CoMET]]]\n"
    "\n## 2. 证据时间线\n- [2026-01-01] [Observation] observed. (Source: [[Source_A]])\n"
)


def test_edge_target_is_stored_as_the_page_key_the_index_answers(isolated_memory):
    """The producer stored the link's literal text while the indexer resolved it.

    ``[[Concept_CoMET]]`` where the page is ``Product_CoMET`` is the case the resolution module
    names in its own docstring, and the reason 230 live edge rows were re-keyable.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    _node(conn, "Product_CoMET", "CoMET")
    from vector_lake.claim_extractor import extract_page_objects

    extracted = extract_page_objects("C:/wiki/Concept_Edge.md", dict(_EDGE_FRONTMATTER), _EDGE_BODY)

    assert [edge["target_id"] for edge in extracted["edges"]] == ["Product_CoMET"]
    # What the page *declared* is still what the page said: only the key field is resolved.
    entity = extracted["entities"][0]
    assert entity["links"] == ["Concept_CoMET"]
    assert entity["triples"] == [{"predicate": "validates", "target": "Concept_CoMET"}]


def test_edge_target_keeps_the_literal_text_without_an_index(isolated_memory):
    db_store.init_db()
    from vector_lake.claim_extractor import extract_page_objects

    extracted = extract_page_objects("C:/wiki/Concept_Edge.md", dict(_EDGE_FRONTMATTER), _EDGE_BODY)

    assert [edge["target_id"] for edge in extracted["edges"]] == ["Concept_CoMET"]
