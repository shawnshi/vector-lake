"""Synthetic official snapshots only; no network or operator MEMORY access."""
import copy
import hashlib
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from tests.test_claim_provenance_repair import _claim, _source
from vector_lake import db_store, governance_store, tool_projection
from vector_lake import tool_claim_provenance as provenance
from vector_lake.governance_metrics import claim_governance_version


def _freeze(path, payload):
    for entry in payload["entries"]:
        entry["review_receipt_sha256"] = provenance._json_sha256({
            key: value for key, value in entry.items() if key != "review_receipt_sha256"
        })
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def official_case(isolated_memory):
    db_store.init_db()
    research = isolated_memory / "raw" / "research"
    research.mkdir()
    selected = [f"claim_reviewed_{i}" for i in range(5)]
    claims = [_claim(cid, f"Research claim {i}", f"Concept_Page_{i % 3}")
              for i, cid in enumerate(selected)]
    claims += [_claim(f"claim_excluded_{i}", f"Other same-page claim {i}", f"Concept_Page_{i % 3}")
               for i in range(8)]
    # Equal text is NOT permission to close an unselected claim.
    claims.append(_claim("claim_equal_extra", claims[0]["claim_text"], "Concept_Extra"))
    claims += [_claim(f"claim_auto_{i}", f"Automatic claim {i}", f"Source_Auto_{i}") for i in range(2)]
    sources, artifacts = [], []
    for i in range(2):
        raw_ref = f"raw/auto_{i}.txt"
        (isolated_memory / raw_ref).write_text(f"Automatic source {i}", encoding="utf-8")
        artifact = provenance.resolve_source_artifact(raw_ref, source_id=f"source_auto_{i}")
        artifacts.append(artifact)
        sources.append(_source(f"source_auto_{i}", raw_ref, f"Source_Auto_{i}", artifact))
    for page in {c["source_page"] for c in claims}:
        (isolated_memory / "wiki" / page).write_text("---\ntitle: Synthetic\n---\nUntouched page\n", encoding="utf-8")
    governance_store.apply_change_set({
        "affected_pages": sorted({c["source_page"] for c in claims}),
        "proposed_entities": [], "proposed_claims": claims, "proposed_evidence": [],
        "proposed_source_updates": sources, "proposed_source_artifacts": artifacts,
        "proposed_extraction_runs": [], "proposed_edges": [],
    })
    canonical = governance_store.load_claims()["items"]
    snapshots = []
    for name in ("index", "specification"):
        data = f"Synthetic official {name} document.\nThe release was published on 2024-01-01.\n".encode()
        raw_ref = f"raw/research/official-{name}.txt"
        (isolated_memory / raw_ref).write_bytes(data)
        snapshots.append({
            "original_url": f"https://standards.example/{name}",
            "retrieved_at": "2024-02-01T00:00:00Z", "raw_ref": raw_ref,
            "representation": "verbatim-text-extraction", "byte_size": len(data),
            "raw_sha256": hashlib.sha256(data).hexdigest(),
            "quoted_excerpt": "The release was published on 2024-01-01.",
            "metadata": {"validator": "operator-verified-text/v1", "consent": "public-research",
                         "classification": "public", "retention_policy": "research-record",
                         "generation_parent_refs": [], "expires_at": None, "revoked_at": None},
            "semantic_review": {"supports_claim": True, "original_source_verified": True,
                                "rationale": "Synthetic operator reviewed the date and specification together."},
        })
    payload = {"contract": "claim-official-evidence-map/v1", "entries": [{
        "claim_id": cid, "expected_claim_version": claim_governance_version(canonical[cid]),
        "expected_claim_sha256": provenance._json_sha256(canonical[cid]),
        "review": {"actor_id": "operator:test", "method_version": "manual-primary-review/v1",
                   "purpose": "Attach reviewed primary passages without fact promotion",
                   "reviewed_at": "2024-02-02T00:00:00Z"},
        "snapshots": copy.deepcopy(snapshots if i == 0 else snapshots[:1]),
    } for i, cid in enumerate(selected)]}
    map_path = isolated_memory / "official-map.json"
    _freeze(map_path, payload)
    return {"memory": isolated_memory, "scope": selected, "payload": payload, "path": map_path}


def _preview(case, **kwargs):
    return provenance.repair_claim_provenance(
        source_claim_ids=case["scope"], official_evidence_map_path=str(case["path"]), **kwargs
    )


def _rows():
    conn = db_store.get_connection()
    # Table names below are fixed test-owned schema identifiers, never user input.
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]  # noqa: S608
            for table in ("claims", "evidence", "sources", "source_artifacts", "extraction_runs",
                          "claim_versions", "evidence_versions", "operational_memory", "governance_queue",
                          "canonical_identities", "claim_assessments")}


def _publish():
    tool_projection.rebuild_index_projection(dry_run=False)


def test_exact_five_official_evidence_isolation_and_history(official_case):
    case = official_case
    assert set(provenance.build_claim_provenance_repair_plan()["candidate_claim_ids"]) == {"claim_auto_0", "claim_auto_1"}
    _publish()
    before = _rows()
    files = {p: p.read_bytes() for folder in ("wiki", "raw")
             for p in (case["memory"] / folder).rglob("*") if p.is_file() and ".meta" not in p.parts}
    preview = _preview(case)
    assert preview["repairable_claims"] == 5
    assert preview["repairable_pages"] == 3
    assert preview["source_claim_ids"] == sorted(case["scope"])
    assert "quoted_excerpt" not in json.dumps(preview)
    assert _rows() == before
    result = _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert result["repaired_claims"] == 5
    assert result["created_evidence"] == 6
    assert all(path.read_bytes() == data for path, data in files.items())
    after = _rows()
    current = governance_store.load_claims()["items"]
    conn = db_store.get_connection()
    columns = [row[1] for row in conn.execute("PRAGMA table_info(claims)")]
    old_claims = {row[0]: json.loads(row[columns.index("data_json")]) for row in before["claims"]}
    for cid, claim in current.items():
        assert claim["claim_text"] == old_claims[cid]["claim_text"]
        assert claim["status"] == old_claims[cid]["status"]
        if cid not in case["scope"]:
            assert claim == old_claims[cid]
        else:
            assert claim["source_ids"] and claim["evidence_ids"]
    memory_columns = [row[1] for row in conn.execute("PRAGMA table_info(operational_memory)")]
    data_index = memory_columns.index("data_json")
    excluded = {row[0]: row for row in before["operational_memory"]
                if json.loads(row[data_index])["source_claim_id"] not in case["scope"]}
    assert {row[0]: row for row in after["operational_memory"] if row[0] in excluded} == excluded
    evidence = governance_store.load_evidence()["items"]
    assert len(evidence) == 6
    for record in evidence.values():
        assert len(record["supports_claim_ids"]) == 1
        assert set(record["supports_claim_ids"]) <= set(case["scope"])
        assert record["evidence_text"] == "The release was published on 2024-01-01."
        assert record["evidence_text"] != current[record["supports_claim_ids"][0]]["claim_text"]
        assert "evidence_tier" not in record
        assert record["official_review"]["review"]["actor_id"] == "operator:test"
    assert set(before["claim_versions"]) <= set(after["claim_versions"])
    assert len(after["claim_versions"]) >= len(before["claim_versions"]) + 5
    assert after["claim_assessments"] == before["claim_assessments"]
    assert len(current[case["scope"][0]]["evidence_ids"]) == 2


@pytest.mark.parametrize("scope", [[], ["x", "x"], [""], [" x"], [False], "x", ["x"] * 257,
                                  [f"c{i}" for i in range(257)], ["claim_missing"]])
def test_scope_rejects_invalid_or_missing(official_case, scope):
    before = _rows()
    with pytest.raises(ValueError):
        provenance.build_claim_provenance_repair_plan(source_claim_ids=scope)
    assert _rows() == before


def test_scope_rejects_ineligible_and_partial(official_case):
    with pytest.raises(ValueError, match="completely repairable"):
        provenance.build_claim_provenance_repair_plan(source_claim_ids=["claim_auto_0", "claim_reviewed_0"])
    conn = db_store.get_connection()
    conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.evidence_ids', json('[\"already\"]'), '$.source_ids', json('[\"source\"]')) WHERE claim_id='claim_reviewed_0'")
    with pytest.raises(ValueError, match="ineligible"):
        _preview(official_case)


@pytest.mark.parametrize("mutation", ["partial", "extra", "mixed", "unscoped", "limit"])
def test_official_map_rejects_mixed_or_partial_scope(official_case, mutation):
    case = official_case
    kwargs = {}
    if mutation == "partial":
        case["payload"]["entries"].pop()
    elif mutation == "extra":
        case["payload"]["entries"].append(copy.deepcopy(case["payload"]["entries"][0]))
    elif mutation == "mixed":
        kwargs["source_map_path"] = str(case["path"])
    elif mutation == "unscoped":
        case["scope"] = None
    else:
        case["payload"]["limit"] = 1
    _freeze(case["path"], case["payload"])
    before = _rows()
    with pytest.raises(ValueError):
        _preview(case, **kwargs)
    assert _rows() == before


@pytest.mark.parametrize("field,value", [
    ("raw_ref", "raw/research/../../outside.txt"), ("raw_ref", "raw/auto_0.txt"),
    ("raw_sha256", "0" * 64), ("byte_size", 1), ("byte_size", True),
    ("quoted_excerpt", "Invented claim wording"), ("original_url", "file:///secret"),
    ("original_url", "https://user:password@standards.example/spec"),
    ("representation", "research-summary"), ("retrieved_at", "2024-02-01"),
    ("semantic_review", {"supports_claim": True}),
])
def test_official_snapshot_validation_zero_writes(official_case, field, value):
    case = official_case
    case["payload"]["entries"][0]["snapshots"][0][field] = value
    _freeze(case["path"], case["payload"])
    before = _rows()
    with pytest.raises(ValueError):
        _preview(case)
    assert _rows() == before


@pytest.mark.parametrize("field,value", [("revoked_at", "2024-02-01T00:00:00Z"),
                                         ("expires_at", "2024-02-01T00:00:00Z"),
                                         ("consent", ""), ("generation_parent_refs", None)])
def test_official_trust_metadata_validation(official_case, field, value):
    case = official_case
    case["payload"]["entries"][0]["snapshots"][0]["metadata"][field] = value
    _freeze(case["path"], case["payload"])
    with pytest.raises(ValueError):
        _preview(case)


@pytest.mark.parametrize("field", ["original_url", "quoted_excerpt", "review", "review_receipt_sha256"])
def test_review_receipt_binds_claim_source_and_operator(official_case, field):
    case = official_case
    entry = case["payload"]["entries"][0]
    if field == "review":
        entry[field]["actor_id"] = "different-operator"
    elif field == "review_receipt_sha256":
        entry[field] = "0" * 64
    else:
        entry["snapshots"][0][field] += "changed"
    case["path"].write_text(json.dumps(case["payload"]), encoding="utf-8")
    before = _rows()
    with pytest.raises(ValueError, match="receipt"):
        _preview(case)
    assert _rows() == before


@pytest.mark.parametrize("drift", ["claim", "memory", "snapshot", "url-refrozen"])
def test_preview_fingerprint_rejects_drift(official_case, drift):
    case = official_case
    preview = _preview(case)
    conn = db_store.get_connection()
    if drift == "claim":
        conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.claim_text', 'changed') WHERE claim_id='claim_reviewed_0'")
    elif drift == "memory":
        conn.execute("UPDATE operational_memory SET data_json=json_set(data_json, '$.note', 'changed')")
    elif drift == "snapshot":
        (case["memory"] / "raw/research/official-index.txt").write_text("changed", encoding="utf-8")
    else:
        case["payload"]["entries"][0]["snapshots"][0]["original_url"] += "/new"
        _freeze(case["path"], case["payload"])
    before = _rows()
    with pytest.raises(ValueError):
        _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _rows() == before


@pytest.mark.parametrize("race, error_type, message", [
    ("claim", ValueError, r"^Official evidence expected claim version/hash changed\.$"),
    ("snapshot", ValueError, r"^Official snapshot hash/byte size changed after freeze\.$"),
    *[(race, RuntimeError,
       r"^Claim provenance repair candidate set changed after preview; "
       r"run a new dry-run and review its fingerprint\.$")
      for race in ("memory", "contradiction_peer", "same_key_peer")],
])
def test_transaction_time_race_causes_zero_repair_writes(official_case, monkeypatch, race, error_type, message):
    case = official_case
    _publish()
    preview = _preview(case)
    original = db_store.transaction
    observed = {"injected": False, "yielded": False}

    @contextmanager
    def racing_transaction(*args, **kwargs):
        assert not observed["injected"]
        assert not observed["yielded"]
        with original(*args, **kwargs) as conn:
            changes_before_injection = conn.total_changes
            if race in {"contradiction_peer", "same_key_peer"}:
                _set_runtime_peer(case, "contradiction" if race == "contradiction_peer" else "same_key",
                                  "claim_excluded_0", conn=conn)
            elif race == "claim":
                conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.claim_text', 'race') WHERE claim_id='claim_reviewed_0'")
            elif race == "memory":
                conn.execute("UPDATE operational_memory SET data_json=json_set(data_json, '$.note', 'race')")
            else:
                (case["memory"] / "raw/research/official-index.txt").write_text("race", encoding="utf-8")
            observed["injected"] = True
            if race != "snapshot":
                assert conn.total_changes > changes_before_injection
            changes_after_injection = conn.total_changes
            try:
                observed["yielded"] = True
                yield conn
            finally:
                assert conn.total_changes == changes_after_injection

    # Build the backup before patching transaction to isolate the race seam.
    backup = tool_projection.require_maintenance_backup("race-test")
    monkeypatch.setattr(tool_projection, "require_maintenance_backup", lambda _label: backup)
    monkeypatch.setattr(db_store, "transaction", racing_transaction)
    before = _rows()
    with pytest.raises(error_type, match=message) as rejection:
        _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert type(rejection.value) is error_type
    assert observed["injected"] is True
    assert observed["yielded"] is True
    assert not db_store.get_connection().in_transaction
    assert _rows() == before


def test_scoped_apply_rejects_inconsistent_backup(official_case):
    preview = _preview(official_case)
    before = _rows()
    with pytest.raises(ValueError, match="consistent maintenance backup"):
        _preview(official_case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _rows() == before


def test_scoped_apply_rejects_stale_consistent_backup(official_case, monkeypatch):
    _publish()
    backup = tool_projection.require_maintenance_backup("stale-test")
    conn = db_store.get_connection()
    conn.execute("UPDATE operational_memory SET data_json=json_set(data_json, '$.note', 'new basis')")
    conn.commit()
    preview = _preview(official_case)
    before = _rows()
    monkeypatch.setattr(tool_projection, "require_maintenance_backup", lambda _label: backup)
    with pytest.raises(ValueError, match="confirmed claim/memory basis"):
        _preview(official_case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _rows() == before


def test_scoped_apply_rejects_tampered_backup(official_case, monkeypatch):
    _publish()
    backup = Path(tool_projection.require_maintenance_backup("tamper-test"))
    preview = _preview(official_case)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_sha256"]["vector_lake.db"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(tool_projection, "require_maintenance_backup", lambda _label: str(backup))
    before = _rows()
    with pytest.raises(ValueError, match="artifact_mismatch"):
        _preview(official_case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _rows() == before


def test_scoped_legacy_automatic_selection_excludes_equal_text_and_other_auto(official_case):
    conn = db_store.get_connection()
    conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.claim_text', 'Automatic claim 0') WHERE claim_id='claim_equal_extra'")
    plan = provenance.build_claim_provenance_repair_plan(source_claim_ids=["claim_auto_0"])
    assert plan["candidate_claim_ids"] == ["claim_auto_0"]
    assert len(plan["candidates"]) == 1


def test_mcp_official_scope_arguments_are_forwarded(monkeypatch):
    from vector_lake import mcp_server

    captured = {}

    def repair(**kwargs):
        captured.update(kwargs)
        return {"dry_run": True}

    monkeypatch.setattr(mcp_server.tools, "repair_claim_provenance", repair)
    assert json.loads(mcp_server.claim_provenance_repair(
        source_claim_ids=["claim_synthetic"], official_evidence_map_path="synthetic-map.json"
    )) == {"dry_run": True}
    assert captured["source_claim_ids"] == ["claim_synthetic"]
    assert captured["official_evidence_map_path"] == "synthetic-map.json"
    assert captured["dry_run"] is True


def test_scope_accepts_full_bound_and_normalizes_order():
    scope = [f"synthetic_{i}" for i in range(256)]
    assert provenance._validate_claim_scope(scope) == sorted(scope)


def test_byte_containment_without_semantic_support_is_rejected(official_case):
    case = official_case
    case["payload"]["entries"][0]["snapshots"][0]["semantic_review"]["supports_claim"] = False
    _freeze(case["path"], case["payload"])
    before = _rows()
    with pytest.raises(ValueError, match="semantic support"):
        _preview(case)
    assert _rows() == before


def test_snapshot_resolved_path_escape_is_rejected(official_case, monkeypatch):
    case = official_case
    original = Path.resolve
    target = case["memory"] / "raw/research/official-index.txt"

    def escaped_resolve(path, *args, **kwargs):
        if path == target:
            return case["memory"] / "raw/auto_0.txt"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped_resolve)
    with pytest.raises(ValueError, match="MEMORY/raw/research"):
        _preview(case)


def test_snapshot_race_during_backup_validation_is_rechecked(official_case, monkeypatch):
    case = official_case
    _publish()
    preview = _preview(case)
    original = provenance._validate_scoped_backup

    def racing_backup(backup, basis):
        original(backup, basis)
        (case["memory"] / "raw/research/official-index.txt").write_text("late drift", encoding="utf-8")

    monkeypatch.setattr(provenance, "_validate_scoped_backup", racing_backup)
    before = _rows()
    with pytest.raises(ValueError, match="hash/byte size"):
        _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _rows() == before


def test_receipt_preimage_and_trust_metadata_are_retained(official_case):
    case = official_case
    for entry in case["payload"]["entries"]:
        for snapshot in entry["snapshots"]:
            snapshot["metadata"]["generation_parent_refs"] = [snapshot["original_url"]]
            snapshot["metadata"]["consent_receipt"] = "synthetic-consent-receipt"
    _freeze(case["path"], case["payload"])
    _publish()
    preview = _preview(case)
    _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    conn = db_store.get_connection()
    runs = provenance._canonical_records(conn, "extraction_runs", "run_id")
    for run in runs.values():
        entry = run["review_receipt"]
        assert run["review_receipt_sha256"] == provenance._json_sha256({
            key: value for key, value in entry.items() if key != "review_receipt_sha256"
        })
    for source in governance_store.load_sources()["items"].values():
        if source.get("source_type") == "official-snapshot":
            metadata = source["official_snapshot"]["metadata"]
            assert metadata["consent_receipt"] == "synthetic-consent-receipt"
            assert source["generation_parent_refs"] == [source["original_url"]]
    for evidence in governance_store.load_evidence()["items"].values():
        assert evidence["independence_status"] == "derived_source"


@pytest.mark.parametrize("value", ["claim" + chr(code) + "x" for code in (32, 9, 10, 160, 8195)])
def test_scope_rejects_embedded_whitespace(value):
    with pytest.raises(ValueError, match="source_claim_ids"):
        provenance._validate_claim_scope([value])


def _set_runtime_peer(case, kind, peer, *, conn=None):
    selected = case["scope"][0]
    # An injected race must use its caller's transaction, not re-enter the patched seam.
    with db_store.transaction() if conn is None else nullcontext(conn) as conn:
        if kind == "contradiction":
            conn.execute(
                "UPDATE operational_memory SET data_json=json_set(data_json, "
                "'$.contradicts_claim_ids', json(?)) "
                "WHERE json_extract(data_json, '$.source_claim_id')=?",
                (json.dumps([peer]), selected),
            )
        else:
            conn.execute(
                "UPDATE operational_memory SET memory_type='preference', "
                "data_json=json_set(data_json, '$.memory_type', 'preference', "
                "'$.memory_key', 'shared_test_key') "
                "WHERE json_extract(data_json, '$.source_claim_id') IN (?, ?)",
                (selected, peer),
            )


@pytest.mark.parametrize("kind", ["contradiction", "same_key"])
def test_scoped_memory_refresh_rejects_unselected_peer_without_writes(official_case, kind, monkeypatch):
    case = official_case
    _set_runtime_peer(case, kind, "claim_excluded_0")
    _freeze(case["path"], case["payload"])
    _publish()
    preview = _preview(case)
    assert preview["repairable_claims"] == 5
    before = _rows()
    # No persistent mutation may precede the dependency guard.
    def forbidden_write(*args, **kwargs):
        pytest.fail("canonical mutation before memory scope rejection")
    monkeypatch.setattr(governance_store, "_register_locator_id_ownership", forbidden_write)
    with pytest.raises(ValueError, match="memory.*scope"):
        _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert _rows() == before


@pytest.mark.parametrize("kind", ["contradiction", "same_key"])
def test_scoped_memory_refresh_allows_in_scope_peers(official_case, kind):
    case = official_case
    _set_runtime_peer(case, kind, case["scope"][1])
    _publish()
    preview = _preview(case)
    result = _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert result["repaired_claims"] == 5


@pytest.mark.parametrize("kind", ["contradiction", "same_key"])
def test_new_memory_dependencies_guard_and_legacy_refresh(official_case, kind, monkeypatch):
    case = official_case
    conn = db_store.get_connection()
    claim = copy.deepcopy(governance_store.load_claims()["items"][case["scope"][0]])
    peer = "claim_excluded_0"
    if kind == "contradiction":
        claim["contradicts"] = [peer]
        claim["authority_score"] = 1.0
    else:
        _set_runtime_peer(case, kind, peer)
        # A preference must satisfy the governed source contract, not merely
        # carry a legacy memory_type label.
        claim.update(memory_type="preference", memory_key="shared_test_key",
                     source_page="Concept_UserPreferences.md",
                     operational_memory_provenance=True,
                     updated_at="2099-01-01T00:00:00Z")
    before = _rows()
    with db_store.transaction(), pytest.raises(ValueError, match="memory.*scope"):
        governance_store._validate_operational_memory_delta_scope(
            {claim["claim_id"]}, [claim], set(case["scope"])
        )
    assert _rows() == before
    # Authorized peers retain the full resolver, as does the unchanged legacy helper.
    monkeypatch.setattr(governance_store, "_utc_now", lambda: "2099-02-01T00:00:00Z")
    with db_store.transaction():
        governance_store._validate_operational_memory_delta_scope(
            {claim["claim_id"]}, [claim], {*case["scope"], peer}
        )
        governance_store._refresh_operational_memory_delta({claim["claim_id"]}, [claim])
    row = conn.execute(
        "SELECT data_json, updated_at FROM operational_memory "
        "WHERE json_extract(data_json, '$.source_claim_id')=?", (peer,),
    ).fetchone()
    assert json.loads(row["data_json"])["validity_state"] == "superseded"
    assert row["updated_at"] == "2099-02-01T00:00:00Z"


@pytest.mark.parametrize("source_type,has_page", [
    (None, True), (None, False),
    ("official-snapshot", False), ("official-snapshot", True),
])
def test_build_claim_graph_projection_source_pages(official_case, source_type, has_page):
    conn = db_store.get_connection()
    source = governance_store.load_sources()["items"]["source_auto_0"]
    if source_type is not None:
        source["source_type"] = source_type
    source["original_url"] = "https://standards.example/index"
    if not has_page:
        del source["canonical_source_page"]
    conn.execute("UPDATE sources SET data_json=? WHERE source_id=?",
                 (json.dumps(source), source["source_id"]))
    conn.execute("UPDATE claims SET data_json=json_set(data_json, '$.source_ids', "
                 "json('[\"source_auto_0\"]')) WHERE claim_id='claim_auto_0'")
    if source_type is None and not has_page:
        with pytest.raises(KeyError) as rejected:
            governance_store.build_claim_graph_projection(connection=conn)
        assert rejected.value.args == ("canonical_source_page",)
    else:
        graph = governance_store.build_claim_graph_projection(connection=conn)
        node = next(node for node in graph["nodes"] if node["id"] == "claim_auto_0")
        assert node["source_pages"] == (["Source_Auto_0.md"] if has_page else [])


def test_official_evidence_repair_then_projection_rebuild(official_case):
    from vector_lake import indexer

    case = official_case
    governance_store.upsert_entity("entity_projection_fixture", {
        "entity_id": "entity_projection_fixture", "page_key": "Concept_Page_0",
        "canonical_name": "Synthetic", "type": "concept",
        "summary": "Synthetic projection fixture", "raw_text": "Untouched page",
    })
    _publish()
    preview = _preview(case)
    result = _preview(case, dry_run=False, confirmation=preview["candidate_fingerprint"])
    assert result["repaired_claims"] == 5
    sources = governance_store.load_sources()["items"]
    official = [source for source in sources.values()
                if source.get("source_type") == "official-snapshot"]
    assert official
    assert all("canonical_source_page" not in source for source in official)
    assert all(source["original_url"] and source["raw_ref"] for source in official)
    before = _rows()
    wiki_files = {p: p.read_bytes() for p in (case["memory"] / "wiki").glob("*.md")}
    output = tool_projection.rebuild_index_projection(dry_run=False)
    assert output.startswith("Rebuilt index projection at ")
    assert "missing_index=0; extra_index=0" in output
    assert indexer.projection_pair_matches_current_generation()
    index = indexer.read_committed_index_snapshot(_mutable=True)
    graph = indexer._read_claim_graph_snapshot(str(indexer.get_claim_graph_path()))
    assert indexer.validate_projection_pair(index, graph)
    assert "Concept_Page_0" in index["nodes"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert set(nodes) == set(governance_store.load_claims()["items"])
    assert all(nodes[cid]["source_pages"] == [] for cid in case["scope"])
    assert _rows() == before
    assert {p: p.read_bytes() for p in (case["memory"] / "wiki").glob("*.md")} == wiki_files
    assert not db_store.get_connection().in_transaction
