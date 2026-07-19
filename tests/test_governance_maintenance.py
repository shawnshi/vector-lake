import json
from pathlib import Path

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
