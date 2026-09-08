import json

import pytest

from vector_lake import db_store, governance_store
from vector_lake import tool_claim_provenance as provenance
from vector_lake import tool_governance_maintenance as maintenance
from vector_lake.evidence_foundation import build_extraction_run, resolve_source_artifact
from vector_lake.governance_metrics import infer_claim_validity


def _claim(claim_id: str, text: str, page_key: str) -> dict:
    return {
        "claim_id": claim_id,
        "claim_text": text,
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
        "locator": {"page_key": page_key, "heading": "Facts", "block_index": 1},
        "source_page": f"{page_key}.md",
    }


def _source(source_id: str, raw_ref: str, page_key: str, artifact: dict) -> dict:
    return {
        "source_id": source_id,
        "raw_ref": raw_ref,
        "canonical_source_page": f"{page_key}.md",
        "artifact_id": artifact["artifact_id"],
        "content_hash": artifact["content_hash"],
        "hash_algorithm": artifact["hash_algorithm"],
        "byte_size": artifact["byte_size"],
        "mime_type": artifact["mime_type"],
        "storage_uri": artifact["storage_uri"],
        "integrity_status": artifact["integrity_status"],
        "classification": artifact["classification"],
        "retention_policy": artifact["retention_policy"],
        "legal_hold": artifact["legal_hold"],
        "lineage_id": artifact["lineage_id"],
        "generation_parent_refs": artifact["generation_parent_refs"],
    }


def test_claim_provenance_repair_is_preview_first_and_closes_exact_debt(
    isolated_memory,
):
    db_store.init_db()
    raw = isolated_memory / "raw"
    wiki = isolated_memory / "wiki"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    (raw / "self.md").write_text("Self source material", encoding="utf-8")
    (raw / "exact.md").write_text("Exact source material", encoding="utf-8")
    source_page = "Source_Self"
    (wiki / f"{source_page}.md").write_text(
        "---\ntitle: Self\ntype: source\nsources: []\n---\nSelf claim text\n",
        encoding="utf-8",
    )

    self_artifact = resolve_source_artifact(
        "raw/self.md", source_id="source_self"
    )
    exact_artifact = resolve_source_artifact(
        "raw/exact.md", source_id="source_exact"
    )
    claims = [
        _claim("claim_self", "Self claim text", source_page),
        _claim("claim_self_copy", "Self claim text", "Concept_Self-Copy"),
        _claim("claim_exact", "Exact claim text", "Concept_Exact"),
    ]
    evidence = {
        "evidence_id": "evidence_exact",
        "evidence_family_id": "evidencefamily_exact",
        "source_id": "source_exact",
        "artifact_id": exact_artifact["artifact_id"],
        "locator": {
            "page_key": "Concept_Exact",
            "heading": "Facts",
            "block_index": 1,
        },
        "projection_locator": {
            "page_key": "Concept_Exact",
            "heading": "Facts",
            "block_index": 1,
        },
        "source_locator": {"kind": "file", "raw_ref": "raw/exact.md"},
        "evidence_text": "Exact claim text",
        "evidence_type": "block-paragraph",
        "lineage_safe": True,
        "supports_claim_ids": [],
        "contradicts_claim_ids": [],
    }
    governance_store.apply_change_set(
        {
            "affected_pages": [
                f"{source_page}.md",
                "Concept_Self-Copy.md",
                "Concept_Exact.md",
            ],
            "proposed_entities": [],
            "proposed_claims": claims,
            "proposed_evidence": [evidence],
            "proposed_source_updates": [
                _source("source_self", "raw/self.md", source_page, self_artifact),
                _source(
                    "source_exact",
                    "raw/exact.md",
                    "Source_Exact",
                    exact_artifact,
                ),
            ],
            "proposed_source_artifacts": [self_artifact, exact_artifact],
            "proposed_extraction_runs": [],
            "proposed_edges": [],
        }
    )
    debt_preview = maintenance.register_unsupported_claim_debt(dry_run=True)
    maintenance.register_unsupported_claim_debt(
        dry_run=False,
        confirmation=debt_preview["candidate_fingerprint"],
    )

    preview = provenance.repair_claim_provenance(dry_run=True)
    assert preview["unsupported_claims"] == 3
    assert preview["repairable_claims"] == 3
    assert preview["unresolved_claims"] == 0
    assert preview["candidate_fingerprint"].startswith("sha256:")
    assert "claim_text" not in json.dumps(preview)
    with pytest.raises(ValueError, match="exact preview fingerprint"):
        provenance.repair_claim_provenance(dry_run=False)

    result = provenance.repair_claim_provenance(
        dry_run=False,
        confirmation=preview["candidate_fingerprint"],
    )
    repaired = governance_store.load_claims()["items"]
    assert result["repaired_claims"] == 3
    assert result["resolved_governance_items"] == 3
    assert result["backup"]
    assert result["projection_rebuild_required"] is True
    assert infer_claim_validity(repaired["claim_self"])["validity_state"] == "active"
    assert infer_claim_validity(repaired["claim_self_copy"])["validity_state"] == "active"
    assert infer_claim_validity(repaired["claim_exact"])["validity_state"] == "active"
    assert repaired["claim_self"]["provenance_repair"]["contract"] == (
        "claim-provenance-repair-plan-v1"
    )
    exact = governance_store.load_evidence()["items"]["evidence_exact"]
    assert "claim_exact" in exact["supports_claim_ids"]
    source_page_evidence = next(
        item
        for item in governance_store.load_evidence()["items"].values()
        if item.get("evidence_type") == "provenance-reconstruction"
    )
    assert set(source_page_evidence["supports_claim_ids"]) == {
        "claim_self",
        "claim_self_copy",
    }
    resolved = [
        item
        for item in governance_store.load_governance_queue()["items"]
        if item.get("type") == "evidence-gap"
    ]
    assert {item["status"] for item in resolved} == {"resolved"}
    assert provenance.repair_claim_provenance(dry_run=True)["unsupported_claims"] == 0


def test_claim_provenance_repair_accepts_frozen_raw_source_map(isolated_memory):
    db_store.init_db()
    raw = isolated_memory / "raw"
    wiki = isolated_memory / "wiki"
    scratch = isolated_memory / "scratch"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    raw_path = raw / "mapped.md"
    raw_path.write_text("Mapped source material", encoding="utf-8")
    page_key = "Source_Mapped"
    (wiki / f"{page_key}.md").write_text(
        "---\ntitle: Mapped\ntype: source\nsources: []\n---\nMapped claim text\n",
        encoding="utf-8",
    )
    governance_store.apply_change_set(
        {
            "affected_pages": [f"{page_key}.md"],
            "proposed_entities": [],
            "proposed_claims": [
                _claim("claim_mapped", "Mapped claim text", page_key)
            ],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    debt_preview = maintenance.register_unsupported_claim_debt(dry_run=True)
    maintenance.register_unsupported_claim_debt(
        dry_run=False,
        confirmation=debt_preview["candidate_fingerprint"],
    )
    artifact = resolve_source_artifact(
        "raw/mapped.md", source_id=provenance._claim_stable_id("source", "raw/mapped.md")
    )
    source_map = scratch / "source-map.json"
    source_map.write_text(
        json.dumps(
            {
                "contract": "claim-provenance-source-map/v1",
                "entries": [
                    {
                        "page_key": page_key,
                        "raw_ref": "raw/mapped.md",
                        "raw_sha256": artifact["content_hash"],
                        "byte_size": artifact["byte_size"],
                        "expected_unsupported_claims": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    preview = provenance.repair_claim_provenance(
        dry_run=True, source_map_path=str(source_map)
    )
    assert preview["repairable_claims"] == 1
    assert preview["unresolved_claims"] == 0
    assert preview["source_map_entries"] == 1
    assert preview["source_map_sha256"].startswith("sha256:")
    assert "claim_text" not in json.dumps(preview)

    result = provenance.repair_claim_provenance(
        dry_run=False,
        confirmation=preview["candidate_fingerprint"],
        source_map_path=str(source_map),
    )
    assert result["repaired_claims"] == 1
    repaired = governance_store.load_claims()["items"]["claim_mapped"]
    assert len(repaired["source_ids"]) == 1
    assert len(repaired["evidence_ids"]) == 1
    source = next(iter(governance_store.load_sources()["items"].values()))
    assert source["raw_ref"] == "raw/mapped.md"
    assert source["integrity_status"] == "verified"
    evidence = governance_store.load_evidence()["items"][repaired["evidence_ids"][0]]
    assert evidence["source_id"] == source["source_id"]
    assert evidence["supports_claim_ids"] == ["claim_mapped"]


def test_claim_provenance_repair_accepts_frozen_extraction_lineage_map(
    isolated_memory,
):
    db_store.init_db()
    raw = isolated_memory / "raw"
    wiki = isolated_memory / "wiki"
    scratch = isolated_memory / "scratch"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    raw_path = raw / "lineage.md"
    raw_path.write_text("Lineage source material", encoding="utf-8")
    source_ref = "Source_Virtual"
    (wiki / f"{source_ref}.md").write_text(
        "---\ntitle: Virtual\ntype: source\nsources: []\n---\nVirtual source\n",
        encoding="utf-8",
    )
    (wiki / "Concept_Lineage.md").write_text(
        "---\ntitle: Lineage\ntype: concept\nsources: []\n---\nLineage claim text\n",
        encoding="utf-8",
    )
    virtual_artifact = resolve_source_artifact(
        source_ref, source_id="source_virtual"
    )
    run = build_extraction_run(
        page_key="Concept_Lineage",
        body="Lineage claim text",
        artifact_ids=[virtual_artifact["artifact_id"]],
        frontmatter={},
    )
    claim = _claim("claim_lineage", "Lineage claim text", "Concept_Lineage")
    claim["extraction_run_id"] = run["run_id"]
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Lineage.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_source_artifacts": [virtual_artifact],
            "proposed_extraction_runs": [run],
            "proposed_edges": [],
        }
    )
    artifact = resolve_source_artifact(
        "raw/lineage.md",
        source_id=provenance._claim_stable_id("source", "raw/lineage.md"),
    )
    source_map = scratch / "lineage-source-map.json"
    source_map.write_text(
        json.dumps(
            {
                "contract": "claim-provenance-source-map/v1",
                "lineage_entries": [
                    {
                        "source_ref": source_ref,
                        "raw_ref": "raw/lineage.md",
                        "raw_sha256": artifact["content_hash"],
                        "byte_size": artifact["byte_size"],
                        "expected_unsupported_claims": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    preview = provenance.repair_claim_provenance(
        dry_run=True, source_map_path=str(source_map)
    )
    assert preview["repairable_claims"] == 1
    assert preview["source_map_lineage_entries"] == 1
    assert preview["source_map_page_entries"] == 0
    result = provenance.repair_claim_provenance(
        dry_run=False,
        confirmation=preview["candidate_fingerprint"],
        source_map_path=str(source_map),
    )
    assert result["repaired_claims"] == 1
    repaired = governance_store.load_claims()["items"]["claim_lineage"]
    source = governance_store.load_sources()["items"][repaired["source_ids"][0]]
    assert source["raw_ref"] == "raw/lineage.md"
    evidence = governance_store.load_evidence()["items"][repaired["evidence_ids"][0]]
    assert evidence["source_id"] == source["source_id"]
    assert evidence["supports_claim_ids"] == ["claim_lineage"]


def test_claim_provenance_repair_accepts_frozen_linked_page_map(isolated_memory):
    db_store.init_db()
    raw = isolated_memory / "raw"
    wiki = isolated_memory / "wiki"
    scratch = isolated_memory / "scratch"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    (raw / "linked.md").write_text("Linked source material", encoding="utf-8")
    source_ref = "Source_Linked"
    (wiki / f"{source_ref}.md").write_text(
        "---\ntitle: Linked Source\ntype: source\nsources: []\n---\nSource body\n",
        encoding="utf-8",
    )
    page_key = "Concept_Linked"
    (wiki / f"{page_key}.md").write_text(
        "---\ntitle: Linked\ntype: concept\nsources: []\n---\n"
        "Linked claim text\n\n## Source\n[[Source_Linked]]\n",
        encoding="utf-8",
    )
    governance_store.apply_change_set(
        {
            "affected_pages": [f"{page_key}.md"],
            "proposed_entities": [],
            "proposed_claims": [
                _claim("claim_linked", "Linked claim text", page_key)
            ],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    artifact = resolve_source_artifact(
        "raw/linked.md",
        source_id=provenance._claim_stable_id("source", "raw/linked.md"),
    )
    source_map = scratch / "linked-page-source-map.json"
    source_map.write_text(
        json.dumps(
            {
                "contract": "claim-provenance-source-map/v1",
                "linked_page_entries": [
                    {
                        "page_key": page_key,
                        "source_ref": source_ref,
                        "raw_ref": "raw/linked.md",
                        "raw_sha256": artifact["content_hash"],
                        "byte_size": artifact["byte_size"],
                        "expected_unsupported_claims": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    preview = provenance.repair_claim_provenance(
        dry_run=True, source_map_path=str(source_map)
    )
    assert preview["repairable_claims"] == 1
    assert preview["source_map_linked_page_entries"] == 1
    result = provenance.repair_claim_provenance(
        dry_run=False,
        confirmation=preview["candidate_fingerprint"],
        source_map_path=str(source_map),
    )
    assert result["repaired_claims"] == 1
    repaired = governance_store.load_claims()["items"]["claim_linked"]
    source = governance_store.load_sources()["items"][repaired["source_ids"][0]]
    assert source["raw_ref"] == "raw/linked.md"
    evidence = governance_store.load_evidence()["items"][repaired["evidence_ids"][0]]
    assert evidence["source_id"] == source["source_id"]
    assert evidence["supports_claim_ids"] == ["claim_linked"]


def _write_linked_page_map_fixture(
    isolated_memory,
    *,
    page_key: str,
    claim_id: str,
    claim_text: str,
    page_body: str,
    source_ref: str,
    raw_name: str,
    source_page_type: str = "source",
):
    db_store.init_db()
    raw = isolated_memory / "raw"
    wiki = isolated_memory / "wiki"
    scratch = isolated_memory / "scratch"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    raw_ref = f"raw/{raw_name}"
    (raw / raw_name).write_text(f"{source_ref} source material", encoding="utf-8")
    (wiki / f"{source_ref}.md").write_text(
        f"---\ntitle: {source_ref}\ntype: {source_page_type}\nsources: []\n---\n"
        "Source body\n",
        encoding="utf-8",
    )
    (wiki / f"{page_key}.md").write_text(
        f"---\ntitle: {page_key}\ntype: concept\nsources: []\n---\n"
        f"{page_body}",
        encoding="utf-8",
    )
    governance_store.apply_change_set(
        {
            "affected_pages": [f"{page_key}.md"],
            "proposed_entities": [],
            "proposed_claims": [_claim(claim_id, claim_text, page_key)],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )
    artifact = resolve_source_artifact(
        raw_ref,
        source_id=provenance._claim_stable_id("source", raw_ref),
    )
    source_map = scratch / f"{page_key}-source-map.json"
    source_map.write_text(
        json.dumps(
            {
                "contract": "claim-provenance-source-map/v1",
                "linked_page_entries": [
                    {
                        "page_key": page_key,
                        "source_ref": source_ref,
                        "raw_ref": raw_ref,
                        "raw_sha256": artifact["content_hash"],
                        "byte_size": artifact["byte_size"],
                        "expected_unsupported_claims": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return source_map


def test_claim_provenance_repair_accepts_frozen_linked_page_map_footnote_source_ref(
    isolated_memory,
):
    source_ref = "Source_Linked_Footnote"
    claim_text = "Footnote linked claim text"
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Footnote",
        claim_id="claim_linked_footnote",
        claim_text=claim_text,
        page_body=(
            f"{claim_text}[^Source_Linked_Footnote]\n\n"
            "[^Source_Linked_Footnote]: Archived source citation.\n"
        ),
        source_ref=source_ref,
        raw_name="linked-footnote.md",
    )

    preview = provenance.repair_claim_provenance(
        dry_run=True, source_map_path=str(source_map)
    )

    assert preview["repairable_claims"] == 1
    assert preview["unresolved_claims"] == 0
    assert preview["source_map_linked_page_entries"] == 1
    assert "claim_text" not in json.dumps(preview)


def test_claim_provenance_repair_rejects_linked_page_map_undefined_footnote_source_ref(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Undefined",
        claim_id="claim_linked_undefined",
        claim_text="Undefined footnote linked claim text",
        page_body="Undefined footnote linked claim text[^Source_Linked_Undefined]\n",
        source_ref="Source_Linked_Undefined",
        raw_name="linked-undefined.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_mixed_wikilink_and_footnote_sources(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Mixed",
        claim_id="claim_linked_mixed",
        claim_text="Mixed linked claim text",
        page_body=(
            "Mixed linked claim text[^Source_Linked_Mixed_A]\n\n"
            "[[Source_Linked_Mixed_B]]\n\n"
            "[^Source_Linked_Mixed_A]: Archived source citation.\n"
        ),
        source_ref="Source_Linked_Mixed_A",
        raw_name="linked-mixed.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_malformed_source_wikilink_target(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Malformed_Wikilink",
        claim_id="claim_linked_malformed_wikilink",
        claim_text="Malformed wikilink linked claim text",
        page_body=(
            "Malformed wikilink linked claim text[^Source_Linked_Malformed_A]\n\n"
            "[[Source_Linked_Malformed_B#fragment]]\n\n"
            "[^Source_Linked_Malformed_A]: Archived source citation.\n"
        ),
        source_ref="Source_Linked_Malformed_A",
        raw_name="linked-malformed-wikilink.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_malformed_definition_wikilink_target(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Malformed_Definition_Wikilink",
        claim_id="claim_linked_malformed_definition_wikilink",
        claim_text="Malformed definition wikilink linked claim text",
        page_body=(
            "Malformed definition wikilink linked claim text[^Source_Linked_Def_A]\n\n"
            "[^Source_Linked_Def_A]: See [[Source_Linked_Def_B#fragment]].\n"
        ),
        source_ref="Source_Linked_Def_A",
        raw_name="linked-malformed-definition-wikilink.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_footnote_definition_contradiction(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Contradiction",
        claim_id="claim_linked_contradiction",
        claim_text="Contradictory linked claim text",
        page_body=(
            "Contradictory linked claim text[^Source_Linked_Contradiction_A]\n\n"
            "[^Source_Linked_Contradiction_A]: See Source_Linked_Contradiction_B.md.\n"
        ),
        source_ref="Source_Linked_Contradiction_A",
        raw_name="linked-contradiction.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_footnote_continuation_contradiction(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Continuation",
        claim_id="claim_linked_continuation",
        claim_text="Continuation linked claim text",
        page_body=(
            "Continuation linked claim text[^Source_Linked_Continuation_A]\n\n"
            "[^Source_Linked_Continuation_A]: Archived source citation.\n"
            "    See Source_Linked_Continuation_B.md.\n"
        ),
        source_ref="Source_Linked_Continuation_A",
        raw_name="linked-continuation.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_blank_line_footnote_continuation(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Blank_Continuation",
        claim_id="claim_linked_blank_continuation",
        claim_text="Blank continuation linked claim text",
        page_body=(
            "Blank continuation linked claim text[^Source_Linked_Blank_Continuation_A]\n\n"
            "[^Source_Linked_Blank_Continuation_A]: Archived source citation.\n\n"
            "    See Source_Linked_Blank_Continuation_B.md.\n"
        ),
        source_ref="Source_Linked_Blank_Continuation_A",
        raw_name="linked-blank-continuation.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_ignores_source_refs_inside_code_for_linked_page_map(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Code",
        claim_id="claim_linked_code",
        claim_text="Code-only linked claim text",
        page_body=(
            "Code-only linked claim text\n\n"
            "```markdown\n[^Source_Linked_Code]\n[[Source_Linked_Code]]\n```\n\n"
            "`[^Source_Linked_Code]`\n"
        ),
        source_ref="Source_Linked_Code",
        raw_name="linked-code.md",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_rejects_linked_page_map_non_source_page(
    isolated_memory,
):
    source_map = _write_linked_page_map_fixture(
        isolated_memory,
        page_key="Concept_Linked_Non_Source",
        claim_id="claim_linked_non_source",
        claim_text="Non-source linked claim text",
        page_body=(
            "Non-source linked claim text[^Source_Linked_Non_Source]\n\n"
            "[^Source_Linked_Non_Source]: Archived source citation.\n"
        ),
        source_ref="Source_Linked_Non_Source",
        raw_name="linked-non-source.md",
        source_page_type="concept",
    )

    with pytest.raises(RuntimeError, match="could not produce every linked-page candidate"):
        provenance.repair_claim_provenance(dry_run=True, source_map_path=str(source_map))


def test_claim_provenance_repair_leaves_unresolved_claim_untouched(isolated_memory):
    db_store.init_db()
    claim = _claim("claim_unresolved", "No evidence exists", "Concept_Unresolved")
    governance_store.apply_change_set(
        {
            "affected_pages": ["Concept_Unresolved.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }
    )

    preview = provenance.repair_claim_provenance(dry_run=True)

    assert preview["unsupported_claims"] == 1
    assert preview["repairable_claims"] == 0
    assert preview["unresolved_claims"] == 1
    assert preview["confirmation_required"] is False
    current = governance_store.load_claims()["items"]["claim_unresolved"]
    assert current["source_ids"] == []
    assert current["evidence_ids"] == []
