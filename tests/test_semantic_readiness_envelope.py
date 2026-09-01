import json
import xml.etree.ElementTree as ET

import pytest

from vector_lake import (
    db_store,
    governance_store,
    memory_protocol,
    runtime_health,
    tool_search,
)


def _binding(token: str) -> dict:
    return {
        "canonical": {"claims": 1, "evidence": 2, "sources": 3},
        "governance": {"governance_queue": 4},
        "projection": {
            "generation": f"projection-{token}",
            "fingerprint": f"sha256:projection-{token}",
        },
        "database_fingerprint": f"sha256:database-{token}",
        "fingerprint": f"sha256:{token}",
    }


def _assessment(*, status: str, issues=None, warnings=None) -> dict:
    issues = list(issues or [])
    warnings = list(warnings or [])
    return {
        "ready": status == "ready",
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "detail": {
            "pending_governance_total": 7,
            "critical_pending_governance": 2,
            "runtime_validity_state_counts": {
                "active": 10,
                "unsupported": 3,
            },
            "awaiting_subagent_jobs": 5,
        },
    }


def _read_text_envelope(payload: str) -> tuple[dict, str]:
    prefix = "<SemanticReadinessEnvelope>\n"
    suffix = "\n</SemanticReadinessEnvelope>\n"
    assert payload.startswith(prefix)
    envelope_text, result = payload[len(prefix) :].split(suffix, 1)
    return json.loads(envelope_text), result


def test_projection_v2_generation_binding_does_not_materialize_index(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import indexer, projection_format_v2

    db_store.init_db()
    indexer.generate_index()

    def reject_materialization(*_args, **_kwargs):
        raise AssertionError("projection materialization entered readiness hot path")

    monkeypatch.setattr(
        indexer,
        "read_committed_index_snapshot",
        reject_materialization,
    )
    monkeypatch.setattr(
        projection_format_v2,
        "materialize_index",
        reject_materialization,
    )

    binding = runtime_health._semantic_readiness_generation_binding()

    assert binding["projection"]["generation"]
    assert binding["projection"]["fingerprint"].startswith("sha256:")
    assert "database_fingerprint" not in binding


def test_nonblocking_readiness_schedules_refresh_without_assessment(monkeypatch):
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    scheduled = []
    assessments = []
    monkeypatch.setattr(
        runtime_health,
        "_semantic_readiness_generation_binding",
        lambda _index_data=None: _binding("cold"),
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_semantic_readiness",
        lambda **_kwargs: assessments.append(True),
    )
    monkeypatch.setattr(
        runtime_health,
        "_schedule_semantic_readiness_refresh",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )

    envelope = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60,
        nonblocking=True,
    )

    assert assessments == []
    assert len(scheduled) == 1
    assert envelope["status"] == "unknown"
    assert envelope["issues"] == ["semantic_readiness_refresh_pending"]
    assert envelope["captured_fingerprint"] == "sha256:cold"


def test_not_ready_envelope_is_bounded_and_does_not_suppress_results(
    monkeypatch,
):
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    monkeypatch.setattr(
        runtime_health,
        "_semantic_readiness_generation_binding",
        lambda _index_data=None: _binding("not-ready"),
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_semantic_readiness",
        lambda **_kwargs: _assessment(
            status="not_ready",
            issues=[f"issue-{index}" for index in range(20)],
            warnings=[f"warning-{index}" for index in range(20)],
        ),
    )
    monkeypatch.setattr(
        tool_search,
        "format_operational_memory_results",
        lambda *_args, **_kwargs: "BASE RETRIEVAL RESULT",
    )
    runtime_health.get_semantic_readiness_envelope(cache_ttl_seconds=60)

    payload = tool_search.search_vector_lake("query", mode="memory")
    envelope, result = _read_text_envelope(payload)

    assert result == "BASE RETRIEVAL RESULT"
    assert envelope["ready"] is False
    assert envelope["status"] == "not_ready"
    assert envelope["issue_count"] == 20
    assert envelope["warning_count"] == 20
    assert len(envelope["issues"]) <= 8
    assert len(envelope["warnings"]) <= 8
    assert envelope["issues_omitted"] == 12
    assert envelope["warnings_omitted"] == 12
    assert envelope["debt_summary"]["critical_pending_governance"] == 2
    assert envelope["results_are_not_accepted_facts"] is True
    assert envelope["captured_fingerprint"] == "sha256:not-ready"


def test_ready_snapshot_has_no_false_alarm_and_reuses_full_assessment(monkeypatch):
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    calls = {"assessment": 0}
    monkeypatch.setattr(
        runtime_health,
        "_semantic_readiness_generation_binding",
        lambda _index_data=None: _binding("ready"),
    )

    def assess(**_kwargs):
        calls["assessment"] += 1
        return _assessment(status="ready")

    monkeypatch.setattr(runtime_health, "assess_semantic_readiness", assess)

    first = runtime_health.get_semantic_readiness_envelope(cache_ttl_seconds=60)
    second = runtime_health.get_semantic_readiness_envelope(cache_ttl_seconds=60)

    assert first == second
    assert calls["assessment"] == 1
    assert first["ready"] is True
    assert first["status"] == "ready"
    assert first["issues"] == []
    assert first["warnings"] == []
    assert first["issue_count"] == 0
    assert first["warning_count"] == 0
    assert first["results_are_not_accepted_facts"] is True


def test_generation_drift_returns_unknown_instead_of_caching_false_ready(
    monkeypatch,
):
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    bindings = iter([_binding("before"), _binding("after")])
    monkeypatch.setattr(
        runtime_health,
        "_semantic_readiness_generation_binding",
        lambda _index_data=None: next(bindings),
    )
    monkeypatch.setattr(
        runtime_health,
        "assess_semantic_readiness",
        lambda **_kwargs: _assessment(status="ready"),
    )

    envelope = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60
    )

    assert envelope["ready"] is False
    assert envelope["status"] == "unknown"
    assert "semantic_readiness_generation_changed" in envelope["issues"]
    assert envelope["captured_fingerprint"] == "sha256:after"


def test_unprovable_generation_is_unknown_without_running_full_assessment(
    monkeypatch,
):
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    calls = {"assessment": 0}

    def unavailable(_index_data=None):
        raise RuntimeError("unprovable generation")

    def assess(**_kwargs):
        calls["assessment"] += 1
        return _assessment(status="ready")

    monkeypatch.setattr(
        runtime_health,
        "_semantic_readiness_generation_binding",
        unavailable,
    )
    monkeypatch.setattr(runtime_health, "assess_semantic_readiness", assess)

    envelope = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60
    )

    assert calls["assessment"] == 0
    assert envelope["ready"] is False
    assert envelope["status"] == "unknown"
    assert envelope["captured_fingerprint"] is None
    assert envelope["results_are_not_accepted_facts"] is True


def test_generation_change_invalidates_hot_snapshot_immediately(monkeypatch):
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    state = {"token": "one", "assessments": 0}
    monkeypatch.setattr(
        runtime_health,
        "_semantic_readiness_generation_binding",
        lambda _index_data=None: _binding(state["token"]),
    )

    def assess(**_kwargs):
        state["assessments"] += 1
        return _assessment(status="ready")

    monkeypatch.setattr(runtime_health, "assess_semantic_readiness", assess)

    runtime_health.get_semantic_readiness_envelope(cache_ttl_seconds=60)
    runtime_health.get_semantic_readiness_envelope(cache_ttl_seconds=60)
    assert state["assessments"] == 1

    state["token"] = "two"
    changed = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60
    )

    assert state["assessments"] == 2
    assert changed["captured_fingerprint"] == "sha256:two"


def test_real_governance_generation_invalidates_cached_assessment(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.indexer import generate_index, read_committed_index_snapshot

    db_store.init_db()
    generate_index()
    index_data = read_committed_index_snapshot(_acquire_lock=False)
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    calls = {"assessment": 0}

    def assess(**_kwargs):
        calls["assessment"] += 1
        return _assessment(status="ready")

    monkeypatch.setattr(runtime_health, "assess_semantic_readiness", assess)
    first = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60,
        index_data=index_data,
    )

    governance_store.upsert_governance_item(
        {
            "item_id": "semantic-readiness-generation-test",
            "type": "suggestion",
            "status": "pending",
            "title": "Readiness generation test",
        }
    )
    second = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60,
        index_data=index_data,
    )

    assert calls["assessment"] == 2
    assert first["captured_fingerprint"] != second["captured_fingerprint"]
    assert str(isolated_memory) not in json.dumps(first, ensure_ascii=False)
    assert (
        first["captured_generation"]["governance"]["governance_queue"] + 1
        == second["captured_generation"]["governance"]["governance_queue"]
    )


def test_projection_commit_tamper_returns_unknown_without_reusing_ready(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.indexer import generate_index, read_committed_index_snapshot
    from vector_lake.wiki_utils import get_projection_manifest_path

    db_store.init_db()
    generate_index()
    index_data = read_committed_index_snapshot(_acquire_lock=False)
    runtime_health._clear_semantic_readiness_envelope_cache_for_tests()
    calls = {"assessment": 0}

    def assess(**_kwargs):
        calls["assessment"] += 1
        return _assessment(status="ready")

    monkeypatch.setattr(runtime_health, "assess_semantic_readiness", assess)
    ready = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60,
        index_data=index_data,
    )
    get_projection_manifest_path().write_text("{}", encoding="utf-8")

    unknown = runtime_health.get_semantic_readiness_envelope(
        cache_ttl_seconds=60,
        index_data=index_data,
    )

    assert ready["status"] == "ready"
    assert calls["assessment"] == 1
    assert unknown["ready"] is False
    assert unknown["status"] == "unknown"
    assert unknown["captured_fingerprint"] is None
    assert unknown["issues"] == [
        "semantic_readiness_binding_unavailable:ProjectionPairContractError"
    ]


@pytest.mark.parametrize("mode", ["memory", "fact"])
def test_direct_memory_search_modes_include_readiness(mode, monkeypatch):
    readiness = {
        "ready": False,
        "status": "not_ready",
        "issues": ["governance debt"],
        "results_are_not_accepted_facts": True,
    }
    monkeypatch.setattr(
        runtime_health,
        "get_semantic_readiness_envelope",
        lambda **_kwargs: readiness,
    )
    monkeypatch.setattr(
        tool_search,
        "format_operational_memory_results",
        lambda *_args, **_kwargs: f"{mode} base result",
    )

    envelope, result = _read_text_envelope(
        tool_search.search_vector_lake("query", mode=mode)
    )

    assert envelope == readiness
    assert result == f"{mode} base result"


def test_direct_page_search_includes_readiness_without_failing_base_retrieval(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.indexer import generate_index

    db_store.init_db()
    generate_index()
    monkeypatch.setattr(
        runtime_health,
        "get_semantic_readiness_envelope",
        lambda **_kwargs: {
            "ready": False,
            "status": "not_ready",
            "issues": ["semantic debt"],
            "results_are_not_accepted_facts": True,
        },
    )

    envelope, result = _read_text_envelope(
        tool_search.search_vector_lake("absent", mode="page")
    )

    assert envelope["status"] == "not_ready"
    assert result == "No matching evidence found.\n"


def test_xml_search_envelope_remains_well_formed(monkeypatch):
    monkeypatch.setattr(
        runtime_health,
        "get_semantic_readiness_envelope",
        lambda **_kwargs: {
            "ready": True,
            "status": "ready",
            "issues": [],
            "warnings": [],
            "results_are_not_accepted_facts": True,
        },
    )
    monkeypatch.setattr(
        tool_search,
        "format_operational_memory_results",
        lambda *_args, **_kwargs: "<MemoryResults />",
    )

    root = ET.fromstring(
        tool_search.search_vector_lake("query", mode="fact", as_xml=True)
    )

    assert root.tag == "VectorLakeSearchResponse"
    envelope = json.loads(root.findtext("SemanticReadinessEnvelope", ""))
    assert envelope["status"] == "ready"
    assert root.find("MemoryResults") is not None


def test_page_xml_search_envelope_preserves_evidence_root(
    isolated_memory,
    monkeypatch,
):
    from vector_lake.indexer import generate_index

    db_store.init_db()
    generate_index()
    monkeypatch.setattr(
        runtime_health,
        "get_semantic_readiness_envelope",
        lambda **_kwargs: {
            "ready": True,
            "status": "ready",
            "issues": [],
            "warnings": [],
            "results_are_not_accepted_facts": True,
        },
    )

    root = ET.fromstring(
        tool_search.search_vector_lake("absent", mode="page", as_xml=True)
    )

    assert root.tag == "VectorLakeSearchResponse"
    assert root.find("EvidenceResults") is not None
    assert root.find("EvidenceResults/NoEvidence") is not None


def test_memory_protocol_read_verbs_expose_the_same_readiness(monkeypatch):
    readiness = {
        "ready": False,
        "status": "degraded",
        "issues": [],
        "warnings": ["coverage"],
        "results_are_not_accepted_facts": True,
    }
    monkeypatch.setattr(
        memory_protocol,
        "get_semantic_readiness_envelope",
        lambda **_kwargs: readiness,
        raising=False,
    )
    monkeypatch.setattr(
        memory_protocol,
        "search_vector_lake",
        lambda *_args, **_kwargs: "recall result",
    )
    monkeypatch.setattr(
        memory_protocol,
        "prepare_query_context",
        lambda *_args, **_kwargs: "synthesis context",
    )
    monkeypatch.setattr(
        memory_protocol,
        "assemble_context",
        lambda *_args, **_kwargs: {"context": "packed"},
    )

    responses = (
        memory_protocol.recall("query"),
        memory_protocol.synthesize("query"),
        memory_protocol.context_pack("query"),
    )

    assert all(item["semantic_readiness"] == readiness for item in responses)
