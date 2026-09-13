"""Canonical re-extraction retention for native reviewed official evidence."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.test_exact_claim_evidence import official_case, _preview, _publish, _rows
from vector_lake import db_store, governance_store
from vector_lake.claim_assessment import record_claim_assessment
from vector_lake.governance_metrics import claim_governance_version
from vector_lake.provenance_retention import ReviewedProvenanceRetentionError
from vector_lake import provenance_retention as retention

# ``official_case`` is a cross-module pytest fixture: importing it is what makes the
# name resolvable for the ``official_case`` parameters below, so every such
# parameter shadows the import and ruff reports the import as unused/redefined.
# Declaring it as an exported name records the intent without deleting a binding
# that 19 tests depend on.
__all__ = ["official_case"]


def test_same_batch_source_revocation_cannot_preserve_binding(official_case):
    case = official_case
    _repair(case)
    claim = governance_store.load_claims()["items"][case["scope"][0]]
    source = copy.deepcopy(
        governance_store.load_sources()["items"][claim["source_ids"][0]]
    )
    source["official_snapshot"]["metadata"]["revoked_at"] = (
        "2024-02-03T00:00:00Z"
    )
    _reextract(case, sources=[source])
    current = governance_store.load_claims()["items"][case["scope"][0]]
    assert source["source_id"] not in current.get("source_ids", [])


def _repair(case):
    _publish()
    preview = _preview(case)
    _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])


def _reextract(case, mutate=None, evidence=None, sources=None):
    current = governance_store.load_claims()["items"]
    selected = set(case["scope"])
    pages = {current[cid]["locator"]["page_key"] for cid in selected}
    proposed = []
    for claim in current.values():
        if claim["locator"]["page_key"] not in pages:
            continue
        item = copy.deepcopy(claim)
        if item["claim_id"] in selected:
            item.pop("provenance_repair", None)
            item["source_ids"] = []
            item["evidence_ids"] = []
            if mutate:
                mutate(item)
        proposed.append(item)
    return governance_store.apply_change_set({
        "affected_pages": sorted(f"{page}.md" for page in pages),
        "proposed_entities": [], "proposed_claims": proposed,
        "proposed_evidence": list(evidence or []),
        "proposed_source_updates": list(sources or []),
        "proposed_source_artifacts": [], "proposed_extraction_runs": [],
        "proposed_edges": [],
    })


def test_native_official_reextract_retains_ids_and_is_repeat_stable(official_case):
    case = official_case
    _repair(case)
    before = governance_store.load_claims()["items"]
    ids = {cid: (before[cid]["source_ids"], before[cid]["evidence_ids"])
           for cid in case["scope"]}
    excluded = copy.deepcopy(before["claim_excluded_0"])
    _reextract(case)
    once = governance_store.load_claims()["items"]
    _reextract(case)
    twice = governance_store.load_claims()["items"]
    for cid in case["scope"]:
        assert (once[cid]["source_ids"], once[cid]["evidence_ids"]) == ids[cid]
        assert twice[cid] == once[cid]
    assert twice["claim_excluded_0"] == excluded


def test_heading_change_keeps_reviewed_evidence_locator_frozen(official_case):
    case = official_case
    _repair(case)
    evidence_before = copy.deepcopy(governance_store.load_evidence()["items"])
    _reextract(case, lambda claim: claim["locator"].update(heading="New heading"))
    evidence_after = governance_store.load_evidence()["items"]
    assert evidence_after == evidence_before
    assert all(governance_store.load_claims()["items"][cid]["locator"]["heading"] == "New heading"
               for cid in case["scope"])


@pytest.mark.parametrize("field,value", [
    ("claim_text", "changed"), ("subject_entity_ids", ["different"]),
    ("claim_type", "different"), ("claim_scope", "different"),
    ("status", "withdrawn"),
])
def test_semantic_or_withdrawal_change_does_not_resurrect(official_case, field, value):
    case = official_case
    _repair(case)
    _reextract(case, lambda claim: claim.__setitem__(field, value))
    current = governance_store.load_claims()["items"]
    assert all(not current[cid].get("evidence_ids") for cid in case["scope"])
    assert all("provenance_repair" not in current[cid] for cid in case["scope"])


def test_missing_current_evidence_is_not_restored_from_history(official_case):
    case = official_case
    _repair(case)
    conn = db_store.get_connection()
    eid = governance_store.load_claims()["items"][case["scope"][0]]["evidence_ids"][0]
    conn.execute("DELETE FROM evidence WHERE evidence_id=?", (eid,))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="missing canonical reviewed evidence"):
        _reextract(case)
    assert _rows() == before


@pytest.mark.parametrize("failure", ["raw-missing", "receipt", "preimage", "io"])
def test_unverifiable_reviewed_proof_rolls_back_full_batch(official_case, monkeypatch, failure):
    case = official_case
    _repair(case)
    conn = db_store.get_connection()
    evidence = next(iter(governance_store.load_evidence()["items"].values()))
    run_id = evidence["extraction_run_id"]
    if failure == "raw-missing":
        (case["memory"] / "raw/research/official-index.txt").unlink()
    elif failure == "receipt":
        conn.execute("UPDATE extraction_runs SET data_json=json_set(data_json, '$.review_receipt_sha256', ?) WHERE run_id=?",
                     ("0" * 64, run_id))
    elif failure == "preimage":
        run = json.loads(conn.execute("SELECT data_json FROM extraction_runs WHERE run_id=?", (run_id,)).fetchone()[0])
        claim_hash = run["review_receipt"]["expected_claim_sha256"]
        conn.execute("DELETE FROM claim_versions WHERE record_hash=?", (claim_hash,))
    else:
        from pathlib import Path
        original = Path.open
        monkeypatch.setattr(Path, "open", lambda path, *a, **k: (_ for _ in ()).throw(OSError("denied"))
                            if "official-" in path.name else original(path, *a, **k))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError):
        _reextract(case)
    assert _rows() == before


def test_conflicting_incoming_evidence_and_same_batch_revocation(official_case):
    case = official_case
    _repair(case)
    record = copy.deepcopy(next(iter(governance_store.load_evidence()["items"].values())))
    record["evidence_text"] = "conflict"
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="conflicts"):
        _reextract(case, evidence=[record])
    assert _rows() == before


def test_current_negative_assessment_prevents_retention(official_case):
    case = official_case
    _repair(case)
    cid = case["scope"][0]
    claim = governance_store.load_claims()["items"][cid]
    record_claim_assessment(
        cid, assessment_type="review", outcome="contradicted", actor_id="tester",
        method_version="v1", reason="negative", expected_claim_version=claim_governance_version(claim),
    )
    _reextract(case)
    current = governance_store.load_claims()["items"][cid]
    assert not current.get("evidence_ids")
    assert "provenance_repair" not in current


def test_stale_negative_assessment_is_not_current_authority(official_case):
    case = official_case
    cid = case["scope"][0]
    claim = governance_store.load_claims()["items"][cid]
    record_claim_assessment(
        cid, assessment_type="review", outcome="unsupported", actor_id="tester",
        method_version="v1", reason="old", expected_claim_version=claim_governance_version(claim),
    )
    # The governed repair creates a new version; the old assessment is stale.
    _repair(case)
    _reextract(case, lambda item: item.__setitem__("locator", {
        **item["locator"], "heading": "new heading"
    }))
    assert governance_store.load_claims()["items"][cid]["evidence_ids"]


@pytest.mark.parametrize("field", ["claim_scope", "source_page"])
def test_reviewed_identity_change_does_not_retain(official_case, field):
    case = official_case
    _repair(case)
    _reextract(case, lambda claim: claim.__setitem__(field, "different"))
    assert not governance_store.load_claims()["items"][case["scope"][0]].get("evidence_ids")


def test_physical_artifact_source_pair_tamper_rolls_back(official_case):
    case = official_case
    _repair(case)
    conn = db_store.get_connection()
    artifact = next(iter(governance_store.load_evidence()["items"].values()))["artifact_id"]
    conn.execute("UPDATE source_artifacts SET source_id='foreign' WHERE artifact_id=?", (artifact,))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="physical binding"):
        _reextract(case)
    assert _rows() == before


def test_consistent_foreign_artifact_pair_rolls_back(official_case):
    case = official_case
    _repair(case)
    evidence = next(iter(governance_store.load_evidence()["items"].values()))
    conn = db_store.get_connection()
    conn.execute("UPDATE source_artifacts SET source_id=?, data_json=json_set(data_json, '$.source_id', ?) WHERE artifact_id=?",
                 ("foreign", "foreign", evidence["artifact_id"]))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="cross-source artifact"):
        _reextract(case)
    assert _rows() == before


def test_source_artifact_pointer_must_match_evidence(official_case):
    case = official_case
    _repair(case)
    evidence = next(iter(governance_store.load_evidence()["items"].values()))
    conn = db_store.get_connection()
    conn.execute("UPDATE sources SET data_json=json_set(data_json, '$.artifact_id', 'foreign') WHERE source_id=?", (evidence["source_id"],))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="cross-source artifact"):
        _reextract(case)
    assert _rows() == before


@pytest.mark.parametrize("field", ["input_fingerprint", "parser_name", "parser_version"])
def test_producer_descriptor_tamper_rolls_back(official_case, field):
    case = official_case
    _repair(case)
    evidence = next(iter(governance_store.load_evidence()["items"].values()))
    conn = db_store.get_connection()
    conn.execute("UPDATE extraction_runs SET data_json=json_set(data_json, ?, 'foreign') WHERE run_id=?", ("$." + field, evidence["extraction_run_id"]))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="producer descriptor"):
        _reextract(case)
    assert _rows() == before


def test_rekeyed_foreign_page_run_does_not_validate(official_case):
    from vector_lake.evidence_foundation import build_extraction_run
    from vector_lake import tool_claim_provenance as producer
    case = official_case
    _repair(case)
    evidence = next(iter(governance_store.load_evidence()["items"].values()))
    conn = db_store.get_connection()
    old_id = evidence["extraction_run_id"]
    run = json.loads(conn.execute("SELECT data_json FROM extraction_runs WHERE run_id=?", (old_id,)).fetchone()[0])
    base = build_extraction_run(page_key="Concept_Foreign", body=evidence["evidence_text"], artifact_ids=[evidence["artifact_id"]], frontmatter={}, extractor_name=producer._EXTRACTOR_NAME, extractor_version=producer._EXTRACTOR_VERSION)
    new_id = producer._claim_stable_id("extractrun", base["run_id"] + run["review_receipt_sha256"] + evidence["official_review"]["claim_id"])
    run.update(base)
    run["run_id"] = new_id
    conn.execute("UPDATE extraction_runs SET run_id=?, data_json=? WHERE run_id=?", (new_id, json.dumps(run), old_id))
    conn.execute("UPDATE evidence SET data_json=json_set(data_json, '$.extraction_run_id', ?) WHERE evidence_id=?", (new_id, evidence["evidence_id"]))
    conn.commit()
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="producer page"):
        _reextract(case)
    assert _rows() == before


@pytest.mark.parametrize("field,value", [
    ("locator.page_key", "Concept_Page_1"),
    ("locator.heading", "Forged reviewed heading"),
    ("projection_locator.page_key", "Concept_Page_1"),
    ("projection_locator.block_index", 999),
    ("source_locator.raw_ref", "raw/research/foreign.txt"),
    ("source_locator.sha256", "0" * 64),
    ("source_locator.original_url", "https://foreign.example/source"),
    ("source_locator.scope", "forged-segment"),
    ("evidence_family_id", "foreign-family"),
    ("independence_status", "forged-independent"),
    ("lineage_safe", False),
    ("official_review.accepted_fact", True),
])
def test_reviewed_evidence_descriptor_tamper_rolls_back(official_case, field, value):
    case = official_case
    _repair(case)
    claim = governance_store.load_claims()["items"][case["scope"][0]]
    eid = claim["evidence_ids"][0]
    evidence = copy.deepcopy(governance_store.load_evidence()["items"][eid])
    target = evidence
    path = field.split(".")
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    conn = db_store.get_connection()
    conn.execute("UPDATE evidence SET data_json=? WHERE evidence_id=?", (json.dumps(evidence), eid))
    conn.commit()
    before = _rows()
    # A moved canonical owner is already rejected by the upstream identity
    # registry guard; the retained-descriptor gate protects the other fields.
    if field == "locator.page_key":
        with pytest.raises((RuntimeError, ReviewedProvenanceRetentionError), match="owned by registry page|evidence descriptor"):
            _reextract(case)
    else:
        with pytest.raises(ReviewedProvenanceRetentionError, match="evidence descriptor"):
            _reextract(case)
    assert _rows() == before


def test_malformed_lifecycle_fails_closed_and_rolls_back(official_case):
    case = official_case
    _repair(case)
    claim = governance_store.load_claims()["items"][case["scope"][0]]
    source = copy.deepcopy(governance_store.load_sources()["items"][claim["source_ids"][0]])
    source["valid_to"] = []
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="lifecycle metadata"):
        _reextract(case, sources=[source])
    assert _rows() == before


def test_same_batch_forged_run_cannot_replace_current_receipt(official_case):
    case = official_case
    _repair(case)
    evidence = next(iter(governance_store.load_evidence()["items"].values()))
    conn = db_store.get_connection()
    run = json.loads(conn.execute(
        "SELECT data_json FROM extraction_runs WHERE run_id=?",
        (evidence["extraction_run_id"],),
    ).fetchone()[0])
    run["review_receipt_sha256"] = "0" * 64
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="conflicts"):
        governance_store.apply_change_set({
            "affected_pages": sorted({f"{governance_store.load_claims()['items'][cid]['locator']['page_key']}.md"
                                      for cid in case["scope"]}),
            "proposed_entities": [],
            "proposed_claims": [copy.deepcopy(governance_store.load_claims()["items"][cid])
                                for cid in case["scope"]],
            "proposed_evidence": [], "proposed_source_updates": [],
            "proposed_source_artifacts": [], "proposed_extraction_runs": [run],
            "proposed_edges": [],
        })
    assert _rows() == before


def test_stream_verify_different_excerpts_share_budget_and_return_current_digest(tmp_path):
    path = tmp_path / "snapshot.txt"
    first = b"alpha and beta"
    path.write_bytes(first)
    total = [0]
    digest, found, _ = retention._stream_verify(path, b"alpha", len(first), total)
    assert (digest, found, total[0]) == (hashlib.sha256(first).hexdigest(), True, len(first))
    second = b"alpha and gamma"
    path.write_bytes(second)
    digest, found, _ = retention._stream_verify(path, b"gamma", len(second), total)
    assert digest == hashlib.sha256(second).hexdigest()
    assert found is True
    assert total[0] == len(first) + len(second)


def test_stream_verify_len_one_excerpt_has_bounded_overlap(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.bin"
    body = b"x" * 200_000 + b"z"
    path.write_bytes(body)
    monkeypatch.setattr(retention, "_MAX_FILE_BYTES", len(body) + 1)
    digest, found, _ = retention._stream_verify(path, b"z", len(body), [0])
    assert digest == hashlib.sha256(body).hexdigest()
    assert found is True


def test_stream_verify_rejects_empty_excerpt_without_reading(tmp_path):
    path = tmp_path / "snapshot.txt"
    path.write_bytes(b"content")
    with pytest.raises(ReviewedProvenanceRetentionError, match="non-empty"):
        retention._stream_verify(path, b"", 7, [0])


def test_stream_verify_growth_over_budget_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.txt"
    path.write_bytes(b"0123456789")
    monkeypatch.setattr(retention, "_MAX_FILE_BYTES", 5)
    with pytest.raises(ReviewedProvenanceRetentionError, match="byte budget"):
        retention._stream_verify(path, b"1", 10, [0])


def test_windows_reparse_attribute_is_rejected_cross_platform():
    assert retention._unsafe_node(SimpleNamespace(st_mode=0, st_file_attributes=0x400))


def test_intermediate_reparse_node_is_rejected(tmp_path, monkeypatch):
    memory = tmp_path / "MEMORY"
    target = memory / "raw" / "research" / "snapshot.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"content")
    original = retention.Path.lstat

    def marked(node):
        info = original(node)
        if node == memory / "raw":
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(retention.Path, "lstat", marked)
    with pytest.raises(ReviewedProvenanceRetentionError, match="reparse"):
        retention._validated_raw_path(memory, "raw/research/snapshot.txt")


def test_assessment_history_cap_fails_closed():
    class Connection:
        def execute(self, _sql, _params):
            return [{"data_json": "{}"}] * (retention._MAX_ASSESSMENTS_PER_CLAIM + 1)

    with pytest.raises(ReviewedProvenanceRetentionError, match="history budget"):
        retention._negative_assessment(Connection(), {"claim_id": "c"})


def test_same_file_change_between_cached_checks_rolls_back(official_case, monkeypatch):
    case = official_case
    _repair(case)
    original = retention._stream_verify
    changed = {"done": False}

    def changing_verify(path, excerpt, expected_size, total_read):
        result = original(path, excerpt, expected_size, total_read)
        if not changed["done"]:
            path.write_bytes(path.read_bytes()[:-1] + b"!")
            changed["done"] = True
        return result

    monkeypatch.setattr(retention, "_stream_verify", changing_verify)
    before = _rows()
    with pytest.raises(ReviewedProvenanceRetentionError, match="changed after verification"):
        _reextract(case)
    assert changed["done"] is True
    assert _rows() == before
