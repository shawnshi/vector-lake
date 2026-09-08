"""Focused regression coverage for claim provenance tool export and MCP routing."""

import json

from vector_lake import (
    db_store,
    mcp_server,
    tool_claim_provenance,
    tool_registry,
    tools,
)


def test_tool_registry_and_facade_export_callable_identity():
    """Verify registry exports repair_claim_provenance with identical callable identity."""
    assert "repair_claim_provenance" in tool_registry.__all__
    assert "repair_claim_provenance" in dir(tools)

    registry_callable = tool_registry.repair_claim_provenance
    facade_callable = tools.repair_claim_provenance

    assert registry_callable is tool_claim_provenance.repair_claim_provenance
    assert facade_callable is tool_claim_provenance.repair_claim_provenance
    assert callable(facade_callable)


def test_mcp_wrapper_routes_unchanged_default_repair_parameters(monkeypatch):
    """Verify MCP wrapper routes defaults including runtime_only=True without alteration."""
    captured: dict = {}

    def fake_repair(**kwargs):
        captured.update(kwargs)
        return {
            "candidate_fingerprint": "fake_fingerprint",
            "confirmation_required": False,
            "dry_run": kwargs.get("dry_run", True),
            "repairable_claims": 0,
            "runtime_only": kwargs.get("runtime_only", True),
            "unsupported_claims": 0,
        }

    monkeypatch.setattr(tools, "repair_claim_provenance", fake_repair)

    raw_result = mcp_server.claim_provenance_repair()
    result = json.loads(raw_result)

    assert result["dry_run"] is True
    assert result["runtime_only"] is True
    assert captured == {
        "confirmation": "",
        "dry_run": True,
        "official_evidence_map_path": "",
        "runtime_only": True,
        "source_claim_ids": None,
        "source_map_path": "",
    }


def test_mcp_wrapper_routes_unchanged_custom_repair_parameters(monkeypatch):
    """Verify MCP wrapper forwards custom parameters and enforces runtime_only=True."""
    captured: dict = {}

    def fake_repair(**kwargs):
        captured.update(kwargs)
        return {
            "candidate_fingerprint": "custom_fp",
            "dry_run": kwargs.get("dry_run", True),
            "repaired_claims": 1,
            "runtime_only": kwargs.get("runtime_only", True),
        }

    monkeypatch.setattr(tools, "repair_claim_provenance", fake_repair)

    raw_result = mcp_server.claim_provenance_repair(
        dry_run=False,
        confirmation="expected_fp",
        source_map_path="source_map.json",
        source_claim_ids=["claim_1", "claim_2"],
        official_evidence_map_path="official_map.json",
    )
    result = json.loads(raw_result)

    assert result["dry_run"] is False
    assert result["repaired_claims"] == 1
    assert captured == {
        "confirmation": "expected_fp",
        "dry_run": False,
        "official_evidence_map_path": "official_map.json",
        "runtime_only": True,
        "source_claim_ids": ["claim_1", "claim_2"],
        "source_map_path": "source_map.json",
    }


def test_mcp_wrapper_live_facade_invocation_isolated(isolated_memory):
    """Verify MCP wrapper invokes live facade and registry without mocks in isolated DB."""
    db_store.init_db()

    raw_result = mcp_server.claim_provenance_repair(dry_run=True)
    result = json.loads(raw_result)

    assert result["dry_run"] is True
    assert result["runtime_only"] is True
    assert result["repairable_claims"] == 0
    assert result["unsupported_claims"] == 0
    assert result["unresolved_claims"] == 0
    assert result["confirmation_required"] is False
    assert result["candidate_sample"] == []
    assert result["unresolved_sample"] == []
    assert isinstance(result["candidate_fingerprint"], str)
    assert result["candidate_fingerprint"].startswith("sha256:")
