import json

from vector_lake import db_store, governance_store
from vector_lake.governance_metrics import claim_governance_version, compute_debt_metrics
from vector_lake import tool_governance_maintenance as maintenance


def _page(title: str, body: str = "") -> str:
    return f"""---
id: {title.lower().replace(' ', '-')}
title: {title}
type: concept
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-07-19T00:00:00+00:00
sources: []
strategic_scope: edge
evidence_tier: derived
---
## 1. 编译事实

### 物理机制 (Mechanism)

{body or title + ' body.'}

## 2. 证据时间线

- [2026-07-19] [Observation] {title} observed.
"""


def _materializing_commit(monkeypatch):
    def commit(mutations, origin, batch_size=200):
        for mutation in mutations:
            (maintenance.get_wiki_dir() / mutation["filename"]).write_text(
                mutation["content"], encoding="utf-8"
            )
        return {
            "committed": len(mutations),
            "outbox_completed": len(mutations),
            "outbox_failed": 0,
        }

    monkeypatch.setattr(maintenance, "_commit_page_mutations", commit)


def test_broken_link_repair_maps_existing_or_records_plain_text(
    isolated_memory, monkeypatch, tmp_path
):
    db_store.init_db()
    wiki = isolated_memory / "wiki"
    (wiki / "Concept_Target-Page.md").write_text(_page("Target Page"), encoding="utf-8")
    (wiki / "Concept_Source-Page.md").write_text(
        _page("Source Page", "[[Target Pagee]] and [[Missing Topic|visible label]]."),
        encoding="utf-8",
    )
    _materializing_commit(monkeypatch)

    preview = maintenance.repair_broken_link_governance(dry_run=True)

    assert preview["mapped_occurrences"] == 1
    assert preview["missing_occurrences"] == 1

    result = maintenance.repair_broken_link_governance(
        dry_run=False,
        backup_dir=str(tmp_path / "link-backup"),
    )
    content = (wiki / "Concept_Source-Page.md").read_text(encoding="utf-8")
    assert "[[Concept_Target-Page]]" in content
    assert "visible label" in content
    assert "[[Missing Topic" not in content
    assert result["governance_items_created"] == 1
    queue = governance_store.load_governance_queue()["items"]
    item = next(item for item in queue if item.get("type") == "missing-link-target")
    assert item["status"] == "acknowledged"
    assert item["resolution"] == "converted-to-text"
    assert item["owner"] == "vector-lake-governance"
    assert item["due_at"]


def test_missing_link_registration_refreshes_expired_debt(isolated_memory):
    db_store.init_db()
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_expired_missing_link",
            "type": "missing-link-target",
            "status": "acknowledged",
            "target_label": "Expired target",
            "owner": "old-owner",
            "due_at": "2000-01-01T00:00:00+00:00",
        }
    )

    preview = maintenance.register_missing_link_debt(dry_run=True)
    result = maintenance.register_missing_link_debt(dry_run=False)
    item = governance_store.load_governance_queue()["items"][0]

    assert preview["to_register"] == 1
    assert result["registered"] == 1
    assert item["owner"] == "vector-lake-governance"
    assert item["due_at"] > "2000-01-01T00:00:00+00:00"


def test_legacy_topology_queue_cleanup_preserves_human_and_decision_scoped_items(
    isolated_memory,
):
    db_store.init_db()
    for item in (
        {
            "item_id": "gov_legacy_topology",
            "type": "community_naming",
            "status": "pending",
            "source": "indexer",
            "affected_pages": ["System_Community_L0_1.md"],
        },
        {
            "item_id": "gov_decision_topology",
            "type": "community_naming",
            "status": "pending",
            "source": "indexer",
            "affected_pages": ["System_Community_L0_2.md"],
            "critical_decision_refs": ["CD-001"],
        },
        {
            "item_id": "gov_human_suggestion",
            "type": "suggestion",
            "status": "pending",
            "source": "human",
        },
    ):
        governance_store.upsert_governance_item(item)

    preview = maintenance.retire_legacy_topology_queue(dry_run=True)
    result = maintenance.retire_legacy_topology_queue(dry_run=False)
    items = {
        item["item_id"]: item
        for item in governance_store.load_governance_queue()["items"]
    }

    assert preview["candidate_count"] == 1
    assert preview["protected_count"] == 1
    assert result["retired_count"] == 1
    assert items["gov_legacy_topology"]["status"] == "superseded"
    assert items["gov_decision_topology"]["status"] == "pending"
    assert items["gov_human_suggestion"]["status"] == "pending"


def test_broken_link_repair_preserves_fenced_source_payload(
    isolated_memory, monkeypatch, tmp_path
):
    db_store.init_db()
    wiki = isolated_memory / "wiki"
    fenced = "```markdown\n[[Missing Code Target]]\n```"
    (wiki / "Concept_Source-Payload.md").write_text(
        _page("Source Payload", fenced + "\n\n[[Missing Body Target]]"),
        encoding="utf-8",
    )
    _materializing_commit(monkeypatch)

    preview = maintenance.repair_broken_link_governance(dry_run=True)
    assert preview["missing_occurrences"] == 1

    maintenance.repair_broken_link_governance(
        dry_run=False,
        backup_dir=str(tmp_path / "payload-backup"),
    )
    content = (wiki / "Concept_Source-Payload.md").read_text(encoding="utf-8")
    assert fenced in content
    assert "[[Missing Body Target]]" not in content


def test_restore_fenced_code_uses_verified_backup(
    isolated_memory, monkeypatch, tmp_path
):
    db_store.init_db()
    wiki = isolated_memory / "wiki"
    original = _page("Raw Payload", "```markdown\n[[Original Link]]\n```")
    target = wiki / "Concept_Raw-Payload.md"
    target.write_text(original.replace("[[Original Link]]", "Original Link"), encoding="utf-8")
    source_backup = tmp_path / "source-backup"
    source_backup.mkdir()
    (source_backup / target.name).write_text(original, encoding="utf-8")
    report = tmp_path / "repair-report.json"
    report.write_text(
        json.dumps(
            {
                "mapped_targets": [],
                "missing_targets": [{"target": "Original Link"}],
            }
        ),
        encoding="utf-8",
    )
    _materializing_commit(monkeypatch)

    preview = maintenance.restore_fenced_code_from_backup(
        str(source_backup), str(report), dry_run=True
    )
    assert preview == {"dry_run": True, "changed_pages": 1, "restored_blocks": 1}

    result = maintenance.restore_fenced_code_from_backup(
        str(source_backup),
        str(report),
        dry_run=False,
        backup_dir=str(tmp_path / "pre-restore"),
    )
    assert result["changed_pages"] == 1
    assert target.read_text(encoding="utf-8") == original


def test_restore_fenced_code_refuses_unexplained_live_edit(
    isolated_memory, monkeypatch, tmp_path
):
    db_store.init_db()
    wiki = isolated_memory / "wiki"
    original = _page("Concurrent Payload", "```markdown\n[[Original Link]]\n```")
    target = wiki / "Concept_Concurrent-Payload.md"
    target.write_text(
        original.replace("[[Original Link]]", "Legitimate concurrent edit"),
        encoding="utf-8",
    )
    source_backup = tmp_path / "source-backup"
    source_backup.mkdir()
    (source_backup / target.name).write_text(original, encoding="utf-8")
    report = tmp_path / "repair-report.json"
    report.write_text(
        json.dumps(
            {
                "mapped_targets": [],
                "missing_targets": [{"target": "Original Link"}],
            }
        ),
        encoding="utf-8",
    )
    _materializing_commit(monkeypatch)

    import pytest

    with pytest.raises(RuntimeError, match="unexplained edits"):
        maintenance.restore_fenced_code_from_backup(
            str(source_backup),
            str(report),
            dry_run=True,
        )


def test_missing_link_item_reconciliation_removes_code_only_target(
    isolated_memory, tmp_path
):
    db_store.init_db()
    source_backup = tmp_path / "source-backup"
    source_backup.mkdir()
    (source_backup / "Concept_Source.md").write_text(
        _page("Source", "```markdown\n[[Code Only Target]]\n```"),
        encoding="utf-8",
    )
    governance_store.upsert_governance_item(
        {
            "item_id": "gov_code_only",
            "type": "missing-link-target",
            "source": "broken-link-governance",
            "status": "acknowledged",
            "target_label": "Code Only Target",
            "occurrences": 1,
            "affected_pages": ["Concept_Source.md"],
        }
    )

    preview = maintenance.reconcile_missing_link_items_from_backup(
        str(source_backup), dry_run=True
    )
    assert preview["remove_code_only_targets"] == 1

    applied = maintenance.reconcile_missing_link_items_from_backup(
        str(source_backup), dry_run=False
    )
    assert applied["removed_code_only_targets"] == 1
    assert governance_store.get_governance_item("gov_code_only") is None


def test_orphan_review_marks_entry_point_without_inventing_link(
    isolated_memory, monkeypatch, tmp_path
):
    db_store.init_db()
    wiki = isolated_memory / "wiki"
    target = wiki / "Concept_Entry-Point.md"
    target.write_text(_page("Entry Point"), encoding="utf-8")
    _materializing_commit(monkeypatch)

    result = maintenance.review_orphan_entry_points(
        dry_run=False,
        backup_dir=str(tmp_path / "orphan-backup"),
    )

    content = target.read_text(encoding="utf-8")
    assert result["acknowledged_pages"] == 1
    assert "topology_status: acknowledged-orphan" in content
    assert "topology_review_basis: no-resolvable-inbound-links" in content
    assert "topology_review_owner: vector-lake-governance" in content
    assert "topology_review_due:" in content
    assert "[[" not in content
    assert maintenance.review_orphan_entry_points(dry_run=True)["orphan_pages"] == 0


def test_unsupported_claim_registration_closes_unmanaged_debt(isolated_memory):
    db_store.init_db()
    claim = {
        "claim_id": "claim_needs_evidence",
        "claim_text": "Claim needs evidence",
        "status": "Active",
        "confidence": 0.8,
        "source_ids": [],
        "evidence_ids": [],
        "subject_entity_ids": [],
        "locator": {"page_key": "Concept_Evidence-Debt"},
    }
    with db_store.transaction():
        db_store.get_connection().execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                claim["claim_id"],
                claim["claim_text"],
                claim["status"],
                json.dumps(claim),
                "2026-07-19T00:00:00+00:00",
            ),
        )

    result = maintenance.register_unsupported_claim_debt(dry_run=False)
    metrics = compute_debt_metrics(skip_heavy=True)

    assert result["registered"] == 1
    item = next(
        item
        for item in governance_store.load_governance_queue()["items"]
        if item.get("type") == "evidence-gap"
    )
    assert item["claim_version"] == claim_governance_version(claim)
    assert item["owner"] == "vector-lake-governance"
    assert item["due_at"]
    assert metrics["unsupported_claim_count"] == 1
    assert metrics["managed_unsupported_claim_count"] == 1
    assert metrics["unmanaged_unsupported_claim_count"] == 0


def test_orphan_source_classification_is_non_destructive_and_resumable(isolated_memory):
    db_store.init_db()
    (isolated_memory / "raw" / "recoverable.md").write_text("raw", encoding="utf-8")
    (isolated_memory / "raw" / "raw-only.md").write_text("raw", encoding="utf-8")
    (isolated_memory / "wiki" / "Source_Recoverable.md").write_text("projection", encoding="utf-8")
    (isolated_memory / "wiki" / "Source_Projection-Only.md").write_text("projection", encoding="utf-8")
    sources = [
        {
            "source_id": "source_recoverable",
            "raw_ref": "raw/recoverable.md",
            "canonical_source_page": "Source_Recoverable.md",
        },
        {
            "source_id": "source_raw_only",
            "raw_ref": "raw/raw-only.md",
            "canonical_source_page": "Source_Raw-Only.md",
        },
        {
            "source_id": "source_projection_only",
            "raw_ref": "raw/missing.md",
            "canonical_source_page": "Source_Projection-Only.md",
        },
        {
            "source_id": "source_unresolved",
            "raw_ref": "raw/missing-too.md",
            "canonical_source_page": "Source_Missing.md",
        },
        {
            "source_id": "source_referenced",
            "raw_ref": "raw/referenced.md",
            "canonical_source_page": "Source_Referenced.md",
        },
    ]
    with db_store.transaction():
        for source in sources:
            db_store.get_connection().execute(
                "INSERT INTO sources (source_id, data_json, updated_at) VALUES (?, ?, ?)",
                (source["source_id"], json.dumps(source), "2026-07-21T00:00:00+00:00"),
            )
        evidence = {
            "evidence_id": "evidence_referenced",
            "source_id": "source_referenced",
            "locator": {"page_key": "Concept_Test"},
        }
        db_store.get_connection().execute(
            "INSERT INTO evidence (evidence_id, data_json, updated_at) VALUES (?, ?, ?)",
            (evidence["evidence_id"], json.dumps(evidence), "2026-07-21T00:00:00+00:00"),
        )

    preview = maintenance.classify_orphan_source_debt(dry_run=True)
    assert preview["orphan_sources"] == 4
    assert preview["buckets"] == {
        "unreferenced_but_recoverable": 1,
        "raw_only": 1,
        "projection_only_missing_raw": 1,
        "unresolved_missing_raw_and_page": 1,
    }
    applied = maintenance.classify_orphan_source_debt(dry_run=False)
    assert applied["registered"] == 4
    assert db_store.get_connection().execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 5
    second = maintenance.classify_orphan_source_debt(dry_run=True)
    assert second["already_managed"] == 4
    assert second["to_register"] == 0
